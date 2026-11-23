"""
sync_engine.py
──────────────
Giai đoạn 3 của KE_HOACH_DONG_BO_VOICE_PHU_DE.md

Điều phối vòng lặp: lập kế hoạch thời gian, tạo giọng, đo thật, tăng tốc nhẹ,
nhờ AI viết gọn, đo lại, rồi chốt timeline neo cứng vào mốc nguồn.

Mọi thao tác ra bên ngoài (gọi TTS, đo file, ép tốc độ, gọi AI viết gọn) đều
được truyền vào dưới dạng hàm, nên toàn bộ logic khó của bài toán đồng bộ kiểm
thử được đầy đủ mà không cần mạng, không cần ffmpeg, không cần render.

Nguyên tắc bất di bất dịch: KHÔNG câu nào được đẩy câu kế tiếp ra sau.
Câu đọc không kịp thì tự xử lý trong khung của mình, hết cách thì báo cho người
dùng, chứ không làm trôi cả phần còn lại của video.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor

import os
import re
import unicodedata
from typing import Callable, Dict, List, Optional

import timing_planner as tp

# Mức độ nghiêm trọng, quyết định màu hiển thị và có chặn xuất bản hay không
BLOCK = "block"      # đỏ, chặn xuất bản
WARN = "warn"        # cam, nghi mất dữ kiện, nên xem trước
INFO = "info"        # vàng / xanh, chỉ để biết

# Loại vấn đề
OVERFLOW = "overflow"              # đọc xong vẫn lấn sang câu sau
OUTSIDE_VIDEO = "outside_video"    # câu nằm ngoài thời lượng video
WRONG_VOICE = "wrong_voice"        # câu dùng giọng khác giọng đã chọn
FACT_MISSING = "fact_missing"      # rút gọn làm mất số / tên riêng / từ phủ định
SHORTENED = "shortened"            # đã rút gọn, còn nguyên dữ kiện
FAST = "fast"                      # bị ép nhanh tới mức tai nghe ra
TTS_FAILED = "tts_failed"          # không tạo được giọng cho câu này
ACCEPTED_OVERFLOW = "accepted_overflow"  # người dùng chủ động cho phép lố
OK = "ok"                          # đọc vừa khung, không cần đụng tới

# Trên mức này thì tai người bắt đầu nhận ra giọng bị ép nhanh
AUDIBLE_SPEEDUP = 1.10

# Bảng Bác sĩ đồng bộ. Ngưỡng chấp nhận lố mềm tự động và trần ép nhanh riêng cho
# nút "ép nhanh hơn" của từng câu (đã chốt với người dùng 2026-09-10).
SOFT_OVERFLOW_MS = 120
HARD_SPEED_CAP = 1.30


# ─────────────────────────────────────────────────────────────────────────────
# KIỂM TRA DỮ KIỆN CÒN NGUYÊN SAU KHI RÚT GỌN
# ─────────────────────────────────────────────────────────────────────────────

# Từ phủ định tiếng Việt. Mất một chữ trong nhóm này là câu đảo ngược nghĩa,
# đây là kiểu hỏng nguy hiểm nhất khi rút gọn.
_NEGATION_WORDS = ("không", "chẳng", "chưa", "đừng", "chớ", "khỏi", "chả", "kẻo")

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _fold(text: str) -> str:
    """Bỏ dấu và hạ chữ thường để so sánh không phụ thuộc cách viết."""
    stripped = "".join(c for c in unicodedata.normalize("NFD", text)
                       if unicodedata.category(c) != "Mn")
    return stripped.lower()


def _proper_nouns(text: str) -> set:
    """
    Tên riêng, xấp xỉ bằng từ viết hoa KHÔNG đứng đầu câu.

    Cách này thô nhưng đủ dùng: mục đích là gợi ý chỗ cần xem, không phải kết luận.
    """
    found = set()
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        words = _TOKEN_RE.findall(sentence)
        for w in words[1:]:
            if w[:1].isupper():
                found.add(_fold(w))
    return found


def check_facts_preserved(original: str, shortened: str) -> List[str]:
    """
    So sánh câu trước và sau khi rút gọn, trả về các dữ kiện đã biến mất.

    Không chứng minh được câu rút gọn đúng nghĩa, chỉ bắt được kiểu hỏng dễ gây
    hậu quả nhất: mất con số, mất tên riêng, và nhất là mất từ phủ định.
    """
    missing: List[str] = []

    goc_nums = _NUMBER_RE.findall(original)
    moi_nums = _NUMBER_RE.findall(shortened)
    for num in goc_nums:
        if num in moi_nums:
            moi_nums.remove(num)
        else:
            missing.append(f"số {num}")

    goc_words = [_fold(w) for w in _TOKEN_RE.findall(original)]
    moi_words = [_fold(w) for w in _TOKEN_RE.findall(shortened)]
    # So sánh trên dạng đã bỏ dấu, nên bảng từ phủ định cũng phải bỏ dấu,
    # nếu không thì "khong" sẽ không bao giờ khớp với "không".
    for neg in _NEGATION_WORDS:
        if goc_words.count(_fold(neg)) > moi_words.count(_fold(neg)):
            missing.append(f"từ phủ định '{neg}'")

    for name in _proper_nouns(original):
        if name not in moi_words:
            missing.append(f"tên riêng '{name}'")

    return missing


# ─────────────────────────────────────────────────────────────────────────────
# VÒNG LẶP ĐỒNG BỘ
# ─────────────────────────────────────────────────────────────────────────────

# Số câu tạo giọng cùng lúc. Đo ngày 13/09 với Edge TTS: tuần tự 4,2 giây cho
# tám câu, hai luồng còn 2,2 giây, bốn luồng còn 1,0 giây và không lượt nào
# hỏng. Không lấy cao hơn: bắn hai mươi tư lượt một lúc thì bị chặn cả phút.
TTS_LANES = 4


def synthesize_anchored(
    cues: List[Dict],
    video_duration_ms: int,
    tts_fn: Callable[[str, str, str, str, str], bool],
    measure_fn: Callable[[str], int],
    speed_fn: Optional[Callable[[str, str, float], bool]] = None,
    rewrite_fn: Optional[Callable[[List[Dict]], Dict[str, str]]] = None,
    path_fn: Optional[Callable[[str, int], str]] = None,
    voice: str = "vi-VN-HoaiMyNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    max_speedup: float = tp.DEFAULT_MAX_SPEEDUP,
    max_rewrite_rounds: int = 2,
    overrides: Optional[Dict[str, Dict]] = None,
    silences: Optional[List] = None,
    scene_cuts: Optional[List] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict:
    """
    Chạy trọn vòng đồng bộ cho một video.

    tts_fn(text, voice, rate, pitch, out_path) -> bool
    measure_fn(path) -> milliseconds
    speed_fn(in_path, out_path, factor) -> bool
    rewrite_fn(list_yeu_cau) -> {cue_id: cau_moi}
    path_fn(cue_id, revision) -> đường dẫn file audio

    overrides: bản đồ theo cue_id, mỗi mục có thể có:
      text          chữ người dùng sửa tay, GHIM cứng, không cho AI rút gọn nữa
      max_speedup   trần ép nhanh riêng cho câu này (nút "ép nhanh hơn")
      allow_overflow  cho phép câu này lố sang cảnh sau, không chặn xuất bản
      locked        không cho autofix đụng tới (chỉ dùng ở tầng gọi)

    Trả về dict gồm `cues` đã chốt timeline và `report` để hiển thị lên giao diện.
    """
    overrides = overrides or {}
    def say(msg: str):
        if progress:
            progress(msg)

    def audio_path(cue_id: str, revision: int, text: str) -> str:
        # Truyền cả nội dung câu để bên gọi có thể đặt tên theo mã băm nội dung,
        # nhờ đó lần chạy sau dùng lại được file cũ thay vì tạo giọng lại từ đầu.
        if path_fn:
            return path_fn(cue_id, revision, text)
        return f"{cue_id}_r{revision}.mp3"

    plan = tp.plan_timeline(cues, video_duration_ms, rate=rate, max_speedup=max_speedup,
                            silences=silences)
    state: Dict[str, Dict] = {}
    for item in plan:
        ov = overrides.get(item["cue_id"], {})
        pinned_text = (ov.get("text") or "").strip()
        state[item["cue_id"]] = {
            **item,
            "original_text": item["text"],
            # Chữ ghim tay được coi là chữ cuối luôn và không cho AI đụng vào.
            "final_text": pinned_text or item["text"],
            "pinned": bool(pinned_text),
            "revision": 0,
            "shortened": False,
            "fact_warnings": [],
            "voice_used": voice,
            "audio_path": None,
            "measured_ms": None,
            "speed_factor": 1.0,
            "final_ms": None,
        }

    def _one(item: Dict) -> None:
        """Tạo giọng và đo độ dài thật của đúng một câu."""
        item["revision"] += 1
        path = audio_path(item["cue_id"], item["revision"], item["final_text"])
        ok = False
        try:
            ok = bool(tts_fn(item["final_text"], voice, rate, pitch, path))
        except Exception as exc:
            say(f"Câu {item['cue_id']} lỗi tạo giọng: {exc}")
        if not ok:
            item["audio_path"] = None
            item["measured_ms"] = None
            return
        item["audio_path"] = path
        item["measured_ms"] = int(measure_fn(path))

    def _speak_all(items: List[Dict]) -> None:
        """
        Tạo giọng cho cả nhóm câu.

        Mỗi câu là một lượt gọi mạng cộng hai lượt gọi ffmpeg, và các câu độc
        lập nhau. Chạy tuần tự thì hai mươi lăm câu xếp hàng chờ nhau. Đo ngày
        13/09: bốn câu một lúc nhanh hơn tuần tự khoảng bốn lần và không câu nào
        bị nhà cung cấp giọng từ chối.
        """
        if len(items) > 1 and TTS_LANES > 1:
            with ThreadPoolExecutor(max_workers=min(TTS_LANES, len(items))) as pool:
                for fut in [pool.submit(_one, item) for item in items]:
                    fut.result()
        else:
            for item in items:
                _one(item)

    # ── Vòng 0: nhờ AI viết gọn TRƯỚC khi tạo giọng ──────────────────────────
    # Bộ ước lượng đã biết trước câu nào chắc chắn tràn, nên rút gọn ngay từ đầu
    # thay vì tạo giọng xong mới phát hiện rồi phải tạo lại. Câu ghim tay bỏ qua.
    #
    # Chỉ những câu bị nghi tràn mới phải chờ AI. Các câu còn lại đã chốt chữ,
    # nên cho chúng đi tạo giọng ngay trong lúc AI đang viết. Trước đây cả video
    # đứng chờ lượt AI xong mới bắt đầu đọc, mất không gần một phút mỗi lần.
    waiting_ids: set = set()
    rewrite_pool = None
    rewrite_job = None
    if rewrite_fn:
        candidates = [s for s in state.values() if s["status"] == tp.REWRITE and not s["pinned"]]
        if candidates:
            say(f"Ước lượng thấy {len(candidates)} câu sẽ tràn khung, đang nhờ AI viết gọn trước...")
            waiting_ids = {s["cue_id"] for s in candidates}
            rewrite_pool = ThreadPoolExecutor(max_workers=1)
            rewrite_job = rewrite_pool.submit(_do_rewrite, state, candidates, rewrite_fn)

    # ── Tạo giọng và đo thật ────────────────────────────────────────────────
    if waiting_ids:
        head = [s for s in state.values() if s["cue_id"] not in waiting_ids]
        if head:
            say(f"Đang tạo giọng cho {len(head)} câu đã chốt chữ trong lúc chờ AI...")
            _speak_all(head)
        try:
            rewrite_job.result()
        finally:
            rewrite_pool.shutdown()
        pending = [s for s in state.values() if s["cue_id"] in waiting_ids]
    else:
        pending = list(state.values())

    for rnd in range(max_rewrite_rounds + 1):
        if not pending:
            break
        say(f"Đang tạo giọng cho {len(pending)} câu (vòng {rnd + 1})...")
        _speak_all(pending)

        # Câu nào đo xong vẫn vượt cả sau khi tăng tốc tối đa thì mới cần vòng sau.
        # Câu ghim tay không bao giờ nhờ AI viết lại.
        still_long = [
            s for s in state.values()
            if not s["pinned"] and s["measured_ms"] is not None and s["budget_ms"] > 0
            and s["measured_ms"] / max_speedup > s["budget_ms"]
        ]
        if not still_long or rnd >= max_rewrite_rounds or not rewrite_fn:
            break
        say(f"Còn {len(still_long)} câu vẫn dài hơn khung, nhờ AI viết gọn lần {rnd + 1}...")
        pending = _do_rewrite(state, still_long, rewrite_fn, use_measured=True)

    # ── Chốt: tăng tốc trong ngưỡng, đặt vào đúng mốc nguồn ─────────────────
    for s in state.values():
        if s["measured_ms"] is None:
            s["final_ms"] = 0
            s["audio_start_ms"] = s["start_ms"]
            s["audio_end_ms"] = s["start_ms"]
            continue

        # Trần ép nhanh riêng cho câu này nếu người dùng đã bấm "ép nhanh hơn",
        # ngược lại dùng trần chung.
        cue_cap = float(overrides.get(s["cue_id"], {}).get("max_speedup") or max_speedup)
        factor = 1.0
        final_ms = s["measured_ms"]
        if s["budget_ms"] > 0 and s["measured_ms"] > s["budget_ms"]:
            need = s["measured_ms"] / s["budget_ms"]
            factor = min(need, cue_cap)
            if factor > 1.001 and speed_fn:
                # File đã ép nhanh phải có tên khác file gốc, nếu không sẽ đè lên
                # chính nó khi tên đặt theo mã băm nội dung.
                sped = _with_suffix(audio_path(s["cue_id"], s["revision"], s["final_text"]),
                                    f"_x{factor:.3f}")
                try:
                    if speed_fn(s["audio_path"], sped, factor):
                        s["audio_path"] = sped
                        final_ms = int(measure_fn(sped))
                    else:
                        final_ms = int(round(s["measured_ms"] / factor))
                except Exception:
                    final_ms = int(round(s["measured_ms"] / factor))
            else:
                final_ms = int(round(s["measured_ms"] / factor))

        s["speed_factor"] = round(factor, 3)
        s["final_ms"] = final_ms
        s["audio_start_ms"] = s["start_ms"]
        s["audio_end_ms"] = s["start_ms"] + final_ms

    ordered = [state[item["cue_id"]] for item in plan]
    return {"cues": ordered,
            "report": build_report(ordered, video_duration_ms, voice, max_speedup,
                                   overrides, scene_cuts)}


def _with_suffix(path: str, suffix: str) -> str:
    """Chèn hậu tố vào trước phần đuôi file."""
    base, ext = os.path.splitext(path)
    return f"{base}{suffix}{ext}"


def _do_rewrite(state: Dict[str, Dict], targets: List[Dict],
                rewrite_fn: Callable, use_measured: bool = False) -> List[Dict]:
    """Gửi yêu cầu viết gọn, nhận lại câu mới, kiểm tra dữ kiện còn nguyên."""
    requests = []
    for s in targets:
        item = dict(s)
        item["text"] = s["final_text"]
        if use_measured and s.get("measured_ms"):
            item["measured_ms"] = s["measured_ms"]
            item["target_syllables"] = tp.max_syllables_for(
                s["budget_ms"], speedup=tp.DEFAULT_MAX_SPEEDUP)
        requests.append(tp.build_shorten_request(item))

    try:
        results = rewrite_fn(requests) or {}
    except Exception:
        results = {}

    changed = []
    for s in targets:
        new_text = (results.get(s["cue_id"]) or "").strip()
        if not new_text or new_text == s["final_text"]:
            continue
        # Chỉ nhận nếu thật sự ngắn hơn, tránh AI trả về câu dài hơn cũ.
        if tp.count_syllables(new_text) >= tp.count_syllables(s["final_text"]):
            continue
        s["fact_warnings"] = check_facts_preserved(s["original_text"], new_text)
        s["final_text"] = new_text
        s["shortened"] = True
        changed.append(s)
    return changed


# ─────────────────────────────────────────────────────────────────────────────
# BÁO CÁO CHO GIAO DIỆN
# ─────────────────────────────────────────────────────────────────────────────

def overflow_ms(s: Dict, video_duration_ms: int) -> int:
    """Phần một câu lố sang cảnh sau hoặc ra ngoài video, tính bằng ms (0 nếu vừa)."""
    over = 0
    if s.get("budget_ms", 0) > 0 and s.get("final_ms") and s["final_ms"] > s["budget_ms"]:
        over = s["final_ms"] - s["budget_ms"]
    if video_duration_ms > 0 and s.get("audio_end_ms", 0) > video_duration_ms:
        over = max(over, s["audio_end_ms"] - video_duration_ms)
    return over


def evaluate_cue(s: Dict, index: int, video_duration_ms: int,
                 chosen_voice: str, overrides: Dict[str, Dict]) -> Dict:
    """
    Chấm một câu ra đúng một trạng thái kèm mức độ, dữ liệu đủ để bảng vẽ và sửa.

    Thứ tự ưu tiên: hỏng cứng (không tạo được giọng, nằm ngoài video) trước, rồi
    tới lố (đã được cho phép hay chưa), sai giọng, nghi mất dữ kiện, rút gọn, ép
    nhanh, cuối cùng là vừa khung.
    """
    ov = overrides.get(s["cue_id"], {})
    over = overflow_ms(s, video_duration_ms)
    info = {
        "cue_id": s["cue_id"],
        "index": index,
        "start_ms": s["start_ms"],
        "start_label": tp.format_timestamp_ms(s["start_ms"]),
        "end_ms": s.get("end_ms"),
        "budget_ms": s.get("budget_ms", 0),
        "measured_ms": s.get("measured_ms"),
        "final_ms": s.get("final_ms"),
        "speed_factor": s.get("speed_factor", 1.0),
        "over_ms": over,
        "shortened": bool(s.get("shortened")),
        "pinned": bool(s.get("pinned")),
        "original_text": s["original_text"],
        "final_text": s["final_text"],
        "audio_path": s.get("audio_path"),
        "max_speedup_override": ov.get("max_speedup"),
        "allow_overflow": bool(ov.get("allow_overflow")),
    }

    def out(kind, severity, detail):
        return {**info, "kind": kind, "severity": severity, "detail": detail}

    if s.get("measured_ms") is None:
        return out(TTS_FAILED, BLOCK, "Không tạo được giọng đọc cho câu này.")
    if video_duration_ms > 0 and s["start_ms"] >= video_duration_ms:
        return out(OUTSIDE_VIDEO, BLOCK,
                   f"Câu bắt đầu ở {s['start_ms'] / 1000:.1f}s, "
                   f"sau khi video đã kết thúc ({video_duration_ms / 1000:.1f}s).")
    if over > 0:
        if ov.get("allow_overflow"):
            return out(ACCEPTED_OVERFLOW, WARN,
                       f"Bạn đã cho phép câu này lố {over} ms sang cảnh sau.")
        return out(OVERFLOW, BLOCK,
                   f"Đọc xong vẫn tràn {over} ms sang cảnh sau, dù đã ép nhanh {s['speed_factor']}x.")
    if s.get("voice_used") and s["voice_used"] != chosen_voice:
        return out(WRONG_VOICE, BLOCK,
                   f"Câu này dùng giọng '{s['voice_used']}' chứ không phải giọng bạn chọn.")
    if s.get("fact_warnings"):
        return out(FACT_MISSING, WARN,
                   "Sau khi rút gọn có vẻ mất: " + ", ".join(s["fact_warnings"]) + ". Bạn đọc lại giúp.")
    if s.get("shortened"):
        return out(SHORTENED, INFO,
                   f"Đã rút gọn từ {tp.count_syllables(s['original_text'])} "
                   f"xuống {tp.count_syllables(s['final_text'])} chữ để vừa khung.")
    if s.get("speed_factor", 1.0) > AUDIBLE_SPEEDUP:
        return out(FAST, INFO, f"Ép nhanh {s['speed_factor']}x, nghe thử xem có gấp quá không.")
    return out(OK, INFO, "")


def _crosses_cut(audio_start_ms: int, audio_end_ms: int, scene_cuts: Optional[List]) -> int:
    """Mốc cắt cảnh hình rơi hẳn vào GIỮA giọng của câu, hay 0 nếu không có."""
    if not scene_cuts or audio_end_ms <= audio_start_ms:
        return 0
    for cut in scene_cuts:
        try:
            c = int(cut)
        except (TypeError, ValueError):
            continue
        # Chừa 120ms hai đầu để không báo khi giọng chỉ chạm sát mép cắt.
        if audio_start_ms + 120 < c < audio_end_ms - 120:
            return c
    return 0


def build_report(cues: List[Dict], video_duration_ms: int,
                 chosen_voice: str, max_speedup: float,
                 overrides: Optional[Dict[str, Dict]] = None,
                 scene_cuts: Optional[List] = None) -> Dict:
    """
    Dựng báo cáo đồng bộ: một mảng `cues` đủ trạng thái để bảng Bác sĩ vẽ và sửa,
    cùng `issues` (các câu không phải OK) để giữ tương thích giao diện cũ.

    Chỉ khẳng định những gì máy chứng minh được bằng số đo. Nhóm cam là nghi ngờ
    dựa trên so sánh dữ kiện, vẫn cần người dùng đọc lại. `scene_cuts` (mốc cắt
    hình) chỉ gắn CỜ nhắc, không chặn xuất bản.
    """
    overrides = overrides or {}
    cue_infos = [evaluate_cue(s, i + 1, video_duration_ms, chosen_voice, overrides)
                 for i, s in enumerate(cues)]
    # Gắn cờ giọng vắt qua một cú cắt hình. Đây là cảnh báo mềm: câu vẫn vừa khung
    # câu kế nhưng người xem sẽ nghe một câu bắc qua hai cảnh khác nhau.
    cut_crossed = 0
    for info, s in zip(cue_infos, cues):
        c = _crosses_cut(s.get("audio_start_ms", 0), s.get("audio_end_ms", 0), scene_cuts)
        info["crosses_cut"] = bool(c)
        info["cut_ms"] = c
        if c:
            cut_crossed += 1
    issues = [c for c in cue_infos if c["kind"] != OK]
    blocking = [c for c in cue_infos if c["severity"] == BLOCK]
    return {
        "total": len(cues),
        "clean": sum(1 for c in cue_infos if c["kind"] == OK),
        "shortened": sum(1 for c in cue_infos if c["kind"] == SHORTENED),
        "sped_up": sum(1 for c in cue_infos if c["kind"] == FAST),
        "accepted_overflow": sum(1 for c in cue_infos if c["kind"] == ACCEPTED_OVERFLOW),
        "crosses_cut": cut_crossed,
        "blocking": len(blocking),
        "warnings": sum(1 for c in cue_infos if c["severity"] == WARN),
        "can_export": not blocking,
        "cues": cue_infos,
        "issues": issues,
        "summary_text": _summary_text(cue_infos),
    }


def _summary_text(cue_infos: List[Dict]) -> str:
    total = len(cue_infos)
    clean = sum(1 for c in cue_infos if c["kind"] == OK)
    shortened = sum(1 for c in cue_infos if c["kind"] == SHORTENED)
    facts = sum(1 for c in cue_infos if c["kind"] == FACT_MISSING)
    fast = sum(1 for c in cue_infos if c["kind"] == FAST)
    accepted = sum(1 for c in cue_infos if c["kind"] == ACCEPTED_OVERFLOW)
    blocking = sum(1 for c in cue_infos if c["severity"] == BLOCK)
    parts = [f"{clean}/{total} câu khớp khung, không cần đụng tới"]
    if shortened:
        parts.append(f"{shortened} câu đã rút gọn")
    if facts:
        parts.append(f"{facts} câu nghi mất dữ kiện, xem trước")
    if fast:
        parts.append(f"{fast} câu ép nhanh nghe ra được")
    if accepted:
        parts.append(f"{accepted} câu đã cho phép lố")
    if blocking:
        parts.append(f"{blocking} câu CHẶN xuất bản")
    return ". ".join(parts) + "."


# ─────────────────────────────────────────────────────────────────────────────
# TỰ SỬA HẾT (auto-fix)
# ─────────────────────────────────────────────────────────────────────────────

def compute_autofix_overrides(cue_infos: List[Dict],
                              overrides: Optional[Dict[str, Dict]] = None,
                              soft_overflow_ms: int = SOFT_OVERFLOW_MS,
                              hard_speed_cap: float = HARD_SPEED_CAP) -> Dict[str, Dict]:
    """
    Tính bộ ghi đè để sửa các câu còn CHẶN, tính THUẦN từ báo cáo đã đo.

    Sau khi engine đã thử ép nhanh trong trần chung và nhờ AI rút gọn hai vòng mà
    một câu vẫn lố, chỉ còn hai đường: nâng trần ép nhanh riêng cho câu đó tới
    tối đa hard_speed_cap, hoặc nếu lố ít hơn ngưỡng mềm thì chấp nhận cho lố.

    Trả về bản đồ ghi đè MỚI (gộp với cái đang có), để tầng gọi chạy lại kế hoạch
    một lượt rồi báo kết quả. Không đụng câu đã bị khoá.
    """
    result: Dict[str, Dict] = {cid: dict(v) for cid, v in (overrides or {}).items()}

    for c in cue_infos:
        if c["kind"] != OVERFLOW:
            continue
        cid = c["cue_id"]
        existing = result.get(cid, {})
        if existing.get("locked"):
            continue

        budget = c.get("budget_ms") or 0
        measured = c.get("measured_ms") or 0
        over = c.get("over_ms") or 0

        # Nếu chỉ cần nâng trần ép nhanh trong mức nghe được là vừa, làm vậy.
        if budget > 0 and measured > 0:
            need = measured / budget
            if need <= hard_speed_cap + 1e-6:
                bump = min(hard_speed_cap, round(need + 0.005, 3))
                result[cid] = {**existing, "max_speedup": bump}
                continue
            # Ép tới trần cứng mà phần lố còn lại đủ nhỏ thì ép hết cỡ rồi cho lố nốt.
            residual = int(round(measured / hard_speed_cap)) - budget
            if residual <= soft_overflow_ms:
                result[cid] = {**existing, "max_speedup": hard_speed_cap, "allow_overflow": True}
                continue

        # Không nâng tốc được nữa: lố ít thì cho phép, nhiều thì để người dùng xử tay.
        if over <= soft_overflow_ms:
            result[cid] = {**existing, "allow_overflow": True}

    return result


def build_mix_plan(cues: List[Dict]) -> List[Dict]:
    """Danh sách file audio kèm mốc chèn, để ghép thành một luồng tiếng duy nhất."""
    return [
        {"path": s["audio_path"], "delay_ms": s["audio_start_ms"], "cue_id": s["cue_id"]}
        for s in cues if s.get("audio_path") and s.get("final_ms")
    ]


def build_display_subtitles(cues: List[Dict]) -> List[Dict]:
    """
    Sinh phụ đề hiển thị TỪ timeline giọng đọc đã chốt.

    Đây là điểm mấu chốt ở mục 8 của kế hoạch: dòng hiển thị là con của câu nói,
    được cắt ra sau khi giọng đã cố định, chứ không phải nguồn để tạo giọng.
    """
    out = []
    for s in cues:
        if not s.get("final_ms"):
            continue
        out.append({
            "index": len(out) + 1,
            "cue_id": s["cue_id"],
            "start": tp.format_timestamp_ms(s["audio_start_ms"]),
            "end": tp.format_timestamp_ms(s["audio_end_ms"]),
            "translated_text": s["final_text"],
            "source_text": s.get("source_text", ""),
        })
    return out
