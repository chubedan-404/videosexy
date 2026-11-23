"""
timing_planner.py
─────────────────
Giai đoạn 2 của KE_HOACH_DONG_BO_VOICE_PHU_DE.md

Toàn bộ phép tính thời gian cho luồng Studio Dịch & Ép Sub, viết dưới dạng hàm
thuần: không đọc ghi file, không gọi mạng, không phụ thuộc ffmpeg. Nhờ vậy phần
khó nhất của bài toán đồng bộ có thể kiểm thử được đầy đủ mà không cần render.

Nguyên tắc cốt lõi, khác hẳn cơ chế hiện hành:

    Mỗi câu được NEO CỨNG vào mốc thời gian gốc của nó.
    Câu đọc không kịp thì tự xử lý trong phần thời gian của chính mình
    (tăng tốc nhẹ, hoặc viết gọn lại), TUYỆT ĐỐI không đẩy câu kế tiếp ra sau.

Cơ chế cũ đẩy câu sau mỗi khi câu trước đọc quá giờ, nên cả lời lẫn phụ đề trôi
dần khỏi hình và người xem nghe một câu trong khi màn hình đã sang cảnh khác.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# HẰNG SỐ HIỆU CHỈNH TỪ ĐO THỰC TẾ
#
# Đo trên Edge TTS giọng vi-VN-HoaiMyNeural, rate +0%, đã cắt khoảng lặng hai
# đầu đúng như pipeline thật, trên 10 câu tiếng Việt dài từ 3 đến 36 âm tiết:
#     thời_lượng_ms ≈ 275 + 199.3 × số_âm_tiết
# Sai số trung bình 98 ms, lớn nhất 178 ms.
#
# Tiếng Việt là ngôn ngữ đơn âm nên đếm âm tiết (tách theo khoảng trắng) cho kết
# quả ổn định hơn hẳn đếm ký tự (sai số trung bình 124 ms).
# ─────────────────────────────────────────────────────────────────────────────
VI_BASE_MS = 275.0
VI_MS_PER_SYLLABLE = 199.3

# Sai số của bộ ước lượng. Ước lượng chỉ dùng để quyết định CÓ CẦN viết gọn hay
# không; kết quả cuối vẫn phải đo trên file audio thật sau khi tạo giọng.
ESTIMATE_ERROR_MS = 200

# Giá trị khởi điểm theo mục 5.2 và 7.1 của kế hoạch, cần hiệu chỉnh sau nghe thử.
DEFAULT_GUARD_MS = 60
DEFAULT_BORROW_CAP_MS = 500
# Khi đo được KHOẢNG LẶNG THẬT sau một câu, cho phép mượn tới ngần này thay cho
# trần cứng 500ms, nhờ đó nhiều câu dài lọt vào đúng quãng nghỉ có thật của video
# thay vì bị coi là lố. Vẫn không bao giờ lấn sang mốc bắt đầu câu kế.
SILENCE_BORROW_CAP_MS = 2000
DEFAULT_MAX_SPEEDUP = 1.15
DEFAULT_MERGE_GAP_MS = 400

# Trạng thái của một câu sau khi lập kế hoạch thời gian
FITS = "fits"              # đọc vừa khung, giữ tốc độ tự nhiên
SPEEDUP = "speedup"        # hơi dài, tăng tốc nhẹ trong ngưỡng cho phép
REWRITE = "rewrite"        # dài quá, cần AI viết gọn lại rồi đo lại
REVIEW = "review"          # không thể tự xử lý, phải để người dùng quyết


# ─────────────────────────────────────────────────────────────────────────────
# MỐC THỜI GIAN
# ─────────────────────────────────────────────────────────────────────────────

_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})$")


def parse_timestamp_ms(value) -> int:
    """Đọc mốc thời gian SRT (chấp nhận cả dấu phẩy và dấu chấm) ra milliseconds."""
    if isinstance(value, (int, float)):
        # Số giây dạng thực, như output thô của Whisper
        return max(0, int(round(float(value) * 1000)))
    text = str(value or "").strip()
    if not text:
        return 0
    match = _TS_RE.match(text)
    if not match:
        try:
            return max(0, int(round(float(text) * 1000)))
        except ValueError:
            return 0
    hours, minutes, seconds, frac = match.groups()
    ms = int(frac.ljust(3, "0")[:3])
    return (int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + ms


def format_timestamp_ms(ms: int) -> str:
    """Ghi milliseconds ra chuẩn SubRip."""
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


# ─────────────────────────────────────────────────────────────────────────────
# ƯỚC LƯỢNG THỜI GIAN ĐỌC
# ─────────────────────────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[.,!?;:…\"'“”‘’()\[\]{}\-–—/\\]+")


def count_syllables(text: str) -> int:
    """Đếm âm tiết tiếng Việt, xấp xỉ bằng số tiếng tách theo khoảng trắng."""
    cleaned = _PUNCT_RE.sub(" ", str(text or ""))
    return len([w for w in cleaned.split() if w])


def rate_to_factor(rate: str) -> float:
    """Đổi chuỗi rate của Edge TTS ('+10%') thành hệ số tốc độ (1.10)."""
    text = str(rate or "").strip().replace("%", "")
    if not text:
        return 1.0
    try:
        return max(0.5, min(3.0, 1.0 + float(text) / 100.0))
    except ValueError:
        return 1.0


def estimate_speech_ms(text: str, rate: str = "+0%") -> int:
    """
    Ước lượng thời gian đọc một câu tiếng Việt, tính bằng milliseconds.

    Đây chỉ là ước lượng để biết trước câu nào sẽ tràn khung, nhờ đó gửi luôn
    yêu cầu viết gọn ngay trong lượt dịch thay vì tạo giọng xong mới phát hiện.
    Kết quả cuối cùng luôn phải đo trên file audio thật.
    """
    syllables = count_syllables(text)
    if syllables <= 0:
        return 0
    raw = VI_BASE_MS + VI_MS_PER_SYLLABLE * syllables
    return int(round(raw / rate_to_factor(rate)))


def max_syllables_for(budget_ms: int, rate: str = "+0%", speedup: float = 1.0) -> int:
    """Số âm tiết tối đa đọc lọt trong ngân sách thời gian cho trước."""
    usable = budget_ms * max(1.0, speedup) * rate_to_factor(rate)
    return max(0, int(math.floor((usable - VI_BASE_MS) / VI_MS_PER_SYLLABLE)))


# ─────────────────────────────────────────────────────────────────────────────
# GOM MẢNH THÀNH CÂU NÓI
# ─────────────────────────────────────────────────────────────────────────────

_SENTENCE_END_RE = re.compile(r"[.!?…]['\"”’)]*\s*$")


def group_into_cues(segments: List[Dict], merge_gap_ms: int = DEFAULT_MERGE_GAP_MS,
                    max_syllables: int = 40) -> List[Dict]:
    """
    Gom các mảnh phụ đề liền nhau thành ĐƠN VỊ CÂU NÓI dùng để tạo giọng.

    Đây là mấu chốt của kế hoạch: câu để đọc và dòng để hiển thị là hai loại dữ
    liệu khác nhau. Trước đây mỗi dòng hiển thị ngắn bị đem đi tạo một file TTS
    riêng, làm giọng đọc cụt lủn và mỗi lần lệch lại đẩy sang dòng sau.

    Nối hai mảnh khi cả ba điều kiện đều đúng: khoảng cách nhỏ hơn ngưỡng, mảnh
    trước không kết thúc bằng dấu câu, và cùng một người nói.
    """
    cues: List[Dict] = []
    for seg in segments or []:
        start = parse_timestamp_ms(seg.get("start"))
        end = parse_timestamp_ms(seg.get("end"))
        text = str(seg.get("translated_text") or seg.get("text") or seg.get("source_text") or "").strip()
        speaker = seg.get("speaker_id")
        if not text:
            continue

        prev = cues[-1] if cues else None
        joinable = (
            prev is not None
            and start - prev["end_ms"] <= merge_gap_ms
            and not _SENTENCE_END_RE.search(prev["text"])
            and prev.get("speaker_id") == speaker
            and count_syllables(prev["text"] + " " + text) <= max_syllables
        )
        if joinable:
            prev["end_ms"] = max(prev["end_ms"], end)
            prev["text"] = f"{prev['text']} {text}".strip()
            prev["source_segment_ids"].append(seg.get("index"))
        else:
            cues.append({
                "cue_id": f"c{len(cues) + 1:04d}",
                "source_segment_ids": [seg.get("index")],
                "start_ms": start,
                "end_ms": max(end, start),
                "text": text,
                "speaker_id": speaker,
            })
    return cues


# ─────────────────────────────────────────────────────────────────────────────
# LẬP KẾ HOẠCH THỜI GIAN
# ─────────────────────────────────────────────────────────────────────────────

def _silent_room_after(end_ms: int, silences: Optional[List]) -> int:
    """
    Bao nhiêu mili giây IM LẶNG THẬT ngay sau khi một câu kết thúc.

    Nhận danh sách quãng lặng (start_ms, end_ms) đo bằng silencedetect. Nếu có một
    quãng lặng phủ ngay sau mốc `end` thì trả về phần lặng còn lại tính từ `end`.
    Không có dữ liệu hoặc chỗ đó không lặng thì trả 0, để giữ cách tính thận trọng cũ.
    """
    if not silences:
        return 0
    room = 0
    for span in silences:
        try:
            s0, s1 = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        # Quãng lặng bắt đầu tại hoặc ngay sát sau lúc câu kết thúc (dung sai 60ms).
        if s0 <= end_ms + 60 and s1 > end_ms:
            room = max(room, s1 - end_ms)
    return room


def plan_timeline(
    cues: List[Dict],
    video_duration_ms: int,
    rate: str = "+0%",
    guard_ms: int = DEFAULT_GUARD_MS,
    borrow_cap_ms: int = DEFAULT_BORROW_CAP_MS,
    max_speedup: float = DEFAULT_MAX_SPEEDUP,
    silences: Optional[List] = None,
) -> List[Dict]:
    """
    Tính hạn chót và ngân sách thời gian cho từng câu, theo công thức mục 5.2.

    Mỗi câu chỉ được mượn thêm phần khoảng nghỉ có sẵn phía sau nó, không bao
    giờ được lấn vào thời gian của câu kế tiếp. Nếu có `silences` (quãng lặng thật
    đo từ audio), câu được mượn tới hết quãng lặng đó thay cho trần cứng 500ms.
    """
    planned: List[Dict] = []
    total = len(cues)

    for i, cue in enumerate(cues):
        start = int(cue["start_ms"])
        end = int(cue["end_ms"])
        next_start = int(cues[i + 1]["start_ms"]) if i + 1 < total else video_duration_ms

        gap = max(0, next_start - end)
        is_last = (i + 1 >= total)

        # Mục 5.2: khoảng đệm giảm về 0 khi hai câu nguồn nối sát nhau. Nếu vẫn
        # trừ 60ms trong trường hợp đó thì hạn chót rơi vào TRƯỚC lúc câu này
        # kết thúc, tức là câu bị cắt ngắn hơn cả khung gốc của chính nó.
        guard = guard_ms if gap >= guard_ms else 0

        if is_last:
            # Câu cuối không có câu nào phía sau để bảo vệ, nên được dùng trọn
            # phần video còn lại thay vì bị chặn bởi trần mượn 500ms.
            borrow = max(0, gap)
            deadline = end + borrow
        else:
            # Trần mượn = 500ms như cũ, nhưng nếu ngay sau câu có khoảng lặng
            # THẬT dài hơn thì nới trần tới hết quãng lặng (tối đa 2s).
            silent_room = _silent_room_after(end, silences)
            cap = borrow_cap_ms
            if silent_room > cap:
                cap = min(silent_room, SILENCE_BORROW_CAP_MS)
            borrow = min(cap, max(0, gap - guard))
            deadline = min(end + borrow, next_start - guard)

        if video_duration_ms > 0:
            deadline = min(deadline, video_duration_ms)

        # Không cho hạn chót ngắn hơn chính khung gốc của câu, NHƯNG cũng không
        # được lấn sang câu sau. Nếu chỉ kẹp theo `end` thì trường hợp mốc nguồn
        # chồng nhau sẽ bị che mất và câu hỏng lại được coi là đạt.
        floor = end if is_last else min(end, next_start)
        if video_duration_ms > 0:
            floor = min(floor, video_duration_ms)
        deadline = max(deadline, floor)
        budget = deadline - start

        item = dict(cue)
        item.update({
            "index_in_run": i + 1,
            "deadline_ms": deadline,
            "budget_ms": budget,
            "gap_ms": gap,
            "borrow_ms": borrow,
            "estimated_ms": estimate_speech_ms(cue["text"], rate),
        })
        item.update(_classify(item, rate, max_speedup, video_duration_ms))
        planned.append(item)

    return planned


def _classify(item: Dict, rate: str, max_speedup: float, video_duration_ms: int) -> Dict:
    """Quyết định câu này tự xử lý được hay phải nhờ người dùng."""
    budget = item["budget_ms"]
    estimated = item["estimated_ms"]

    if video_duration_ms > 0 and item["start_ms"] >= video_duration_ms:
        return {"status": REVIEW,
                "reason": "Câu bắt đầu sau khi video đã kết thúc nên sẽ không bao giờ hiện lên.",
                "required_speedup": None, "target_syllables": None}

    if budget <= 0:
        return {"status": REVIEW,
                "reason": "Mốc thời gian nguồn bị chồng lấn nên câu này không còn khoảng trống nào.",
                "required_speedup": None, "target_syllables": None}

    if estimated <= budget:
        return {"status": FITS, "reason": "", "required_speedup": 1.0, "target_syllables": None}

    required = estimated / budget
    if required <= max_speedup:
        return {"status": SPEEDUP,
                "reason": f"Dài hơn khung {estimated - budget} ms, tăng tốc {required:.2f}x là vừa.",
                "required_speedup": round(required, 3), "target_syllables": None}

    target = max_syllables_for(budget, rate, max_speedup)
    if target < 3:
        return {"status": REVIEW,
                "reason": (f"Khung chỉ có {budget} ms, không đủ để đọc trọn vẹn dù chỉ vài chữ. "
                           "Cần bạn xem lại mốc thời gian hoặc gộp câu."),
                "required_speedup": round(required, 3), "target_syllables": target}

    return {"status": REWRITE,
            "reason": (f"Dài hơn khung {estimated - budget} ms, vượt mức tăng tốc cho phép "
                       f"({required:.2f}x > {max_speedup:.2f}x). Cần viết gọn còn khoảng {target} chữ."),
            "required_speedup": round(required, 3), "target_syllables": target}


# ─────────────────────────────────────────────────────────────────────────────
# ÁP THỜI LƯỢNG ĐO ĐƯỢC SAU KHI TẠO GIỌNG
# ─────────────────────────────────────────────────────────────────────────────

def apply_measured_durations(planned: List[Dict], measured_ms_by_cue: Dict[str, int],
                             max_speedup: float = DEFAULT_MAX_SPEEDUP) -> List[Dict]:
    """
    Chốt timeline bằng thời lượng ĐO THẬT của từng file giọng đọc.

    Ước lượng chỉ để chuẩn bị; đây mới là bước quyết định. Câu nào vẫn không vừa
    thì đánh dấu cần xử lý, KHÔNG đẩy câu kế tiếp ra sau.
    """
    out: List[Dict] = []
    for item in planned:
        item = dict(item)
        measured = measured_ms_by_cue.get(item["cue_id"])
        if measured is None:
            item["audio_start_ms"] = item["start_ms"]
            item["audio_end_ms"] = item["start_ms"]
            out.append(item)
            continue

        budget = item["budget_ms"]
        speed = 1.0
        final = measured
        if budget > 0 and measured > budget:
            needed = measured / budget
            speed = min(needed, max_speedup)
            final = int(round(measured / speed))

        item["measured_ms"] = measured
        item["speed_factor"] = round(speed, 3)
        item["final_ms"] = final
        item["audio_start_ms"] = item["start_ms"]
        item["audio_end_ms"] = item["start_ms"] + final

        if budget > 0 and final > budget:
            item["status"] = REVIEW
            item["reason"] = (f"Đã tăng tốc tối đa {max_speedup:.2f}x mà vẫn dài hơn khung "
                              f"{final - budget} ms. Cần rút gọn câu này.")
        elif item["status"] in (SPEEDUP, REWRITE):
            item["status"] = FITS
            item["reason"] = ""
        out.append(item)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# KIỂM TRA TIMELINE
# ─────────────────────────────────────────────────────────────────────────────

def validate_timeline(planned: List[Dict], video_duration_ms: int) -> List[Dict]:
    """
    Soát lại timeline trước khi cho render. Trả về danh sách vấn đề, rỗng là đạt.

    Đây là phần chặn ở mục 12 của kế hoạch: không bao giờ để lời hoặc phụ đề nằm
    ngoài video, và không để hai câu chồng tiếng lên nhau.
    """
    problems: List[Dict] = []
    last_end = -1
    last_id = None

    for item in planned:
        cue_id = item.get("cue_id")
        start = item.get("audio_start_ms", item["start_ms"])
        end = item.get("audio_end_ms", item["start_ms"] + item.get("estimated_ms", 0))

        if video_duration_ms > 0 and start >= video_duration_ms:
            problems.append({"cue_id": cue_id, "kind": "starts_after_video",
                             "detail": f"bắt đầu ở {start} ms, video chỉ dài {video_duration_ms} ms"})
        elif video_duration_ms > 0 and end > video_duration_ms:
            problems.append({"cue_id": cue_id, "kind": "ends_after_video",
                             "detail": f"kết thúc ở {end} ms, vượt cuối video {end - video_duration_ms} ms"})

        if last_end >= 0 and start < last_end:
            problems.append({"cue_id": cue_id, "kind": "overlaps_previous",
                             "detail": f"chồng lên {last_id} mất {last_end - start} ms"})

        if item.get("status") == REVIEW:
            problems.append({"cue_id": cue_id, "kind": "needs_review",
                             "detail": item.get("reason", "")})

        last_end, last_id = end, cue_id

    return problems


def summarize(planned: List[Dict]) -> Dict:
    """Tóm tắt nhanh một kế hoạch, dùng cho log và cho giao diện."""
    counts = {FITS: 0, SPEEDUP: 0, REWRITE: 0, REVIEW: 0}
    for item in planned:
        counts[item.get("status", REVIEW)] = counts.get(item.get("status", REVIEW), 0) + 1
    total = len(planned) or 1
    return {
        "total": len(planned),
        "counts": counts,
        "fits_percent": round(counts[FITS] * 100.0 / total, 1),
        "needs_ai_rewrite": counts[REWRITE],
        "needs_human": counts[REVIEW],
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT VIẾT GỌN
# ─────────────────────────────────────────────────────────────────────────────

def build_shorten_request(item: Dict) -> Dict:
    """
    Dựng yêu cầu viết gọn cho một câu, đúng tinh thần mục 6.5 của kế hoạch.

    Gửi kèm số chữ mục tiêu để AI có đích cụ thể thay vì đoán mò, và nói rõ
    không được bỏ phủ định, con số hay điều kiện.
    """
    return {
        "cue_id": item["cue_id"],
        "current_text": item["text"],
        "measured_ms": item.get("measured_ms", item.get("estimated_ms")),
        "budget_ms": item["budget_ms"],
        "target_syllables": item.get("target_syllables"),
        "instruction": (
            f"Câu tiếng Việt hiện tại đọc mất khoảng "
            f"{item.get('measured_ms', item.get('estimated_ms'))} ms, "
            f"trong khi chỉ có {item['budget_ms']} ms. "
            f"Viết lại ngắn hơn, còn khoảng {item.get('target_syllables')} chữ, "
            "giữ nguyên ý câu gốc cùng mọi tên riêng, con số, đơn vị, từ phủ định và điều kiện. "
            "Không đổi mốc thời gian. Nếu không thể rút gọn mà vẫn giữ đủ ý, "
            "trả về needs_review thay vì làm sai nghĩa."
        ),
    }
