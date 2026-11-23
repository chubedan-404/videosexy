"""
remotion_generator.py
─────────────────────
Pipeline: 9Router / Gemini (kịch bản JSON) → Edge-TTS (giọng đọc) → Remotion (MP4 1080x1920)

Nguyên tắc:
- Không bao giờ âm thầm trả về kịch bản mẫu khi AI lỗi. Lỗi phải nổi lên giao diện.
- Không bao giờ lấy chữ của kịch bản mẫu để vá vào cảnh mà AI trả thiếu.
- Diện mạo video do bộ token trong remotion_templates.py quyết định, không hardcode trong React.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from typing import Callable, Dict, List, Optional, Tuple

import requests

from remotion_templates import (
    DEFAULT_TEMPLATE_ID,
    SCENE_GRADIENTS,
    resolve_tokens,
)

REMOTION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remotion")
REMOTION_PUBLIC_DIR = os.path.join(REMOTION_DIR, "public")
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
FPS = 30
SCENE_COUNT = 3

# Giá trị đặc biệt của ô chọn model trên giao diện: bỏ qua 9Router, gọi thẳng Gemini.
FORCE_GEMINI = "__gemini__"

# Kịch bản mẫu: CHỈ dùng khi người dùng mở tool lần đầu hoặc bấm "Mẫu mặc định".
# Tuyệt đối không dùng để vá vào kết quả AI. Chủ đề mẫu để trung tính, vì tool
# làm được video cho mọi ngành, không riêng kênh mẹ và bé.
DEFAULT_SCENES = [
    {
        "id": 1,
        "kind": "hook",
        "icon": "🔥",
        "tag": "",
        "title": "Bạn đang mất ba tiếng mỗi ngày mà không hề biết",
        "desc": "Ba tiếng bốc hơi mỗi ngày",
        "highlightWord": "",
        "durationInFrames": 100,
        "bgGradient": SCENE_GRADIENTS[0],
    },
    {
        "id": 2,
        "kind": "body",
        "icon": "⚠️",
        "tag": "VẤN ĐỀ THẬT",
        "title": "Ba Tiếng Trôi Qua Trong Vô Thức",
        "desc": "Mỗi lần bị ngắt quãng bạn mất thêm hai ba phút để quay lại đúng mạch làm việc cũ, một ngày cộng dồn lại là gần ba tiếng.",
        "highlightWord": "ngắt quãng",
        "durationInFrames": 320,
        "bgGradient": SCENE_GRADIENTS[1],
    },
    {
        "id": 3,
        "kind": "body",
        "icon": "❌",
        "tag": "SAI Ở ĐÂU",
        "title": "Làm Nhiều Việc Cùng Lúc",
        "desc": "Não không chạy song song được, nó chỉ nhảy qua nhảy lại, và mỗi lần nhảy như vậy bạn trả giá bằng sự tập trung vừa mới gom được.",
        "highlightWord": "nhảy qua nhảy lại",
        "durationInFrames": 340,
        "bgGradient": SCENE_GRADIENTS[2],
    },
    {
        "id": 4,
        "kind": "body",
        "icon": "💡",
        "tag": "CÁCH LÀM ĐÚNG",
        "title": "Gom Việc Thành Từng Khối",
        "desc": "Hãy gom việc cùng loại vào một khối chín mươi phút, tắt thông báo, và chỉ mở hộp thư đúng hai lần trong ngày là bạn lấy lại được phần lớn số giờ đó.",
        "highlightWord": "một khối chín mươi phút",
        "durationInFrames": 360,
        "bgGradient": SCENE_GRADIENTS[3],
    },
]

TEXT_FIELDS = ("icon", "tag", "title", "desc", "highlightWord")


# ─────────────────────────────────────────────────────────────────────────────
# CHUẨN HOÁ & KIỂM TRA KỊCH BẢN
# ─────────────────────────────────────────────────────────────────────────────

class SceneTooShortError(ValueError):
    """Cảnh bị kéo ngắn hơn lời đọc: chặn lại thay vì xuất ra video bị cắt lời."""


def validate_output_filename(filename):
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.mp4", filename):
        raise ValueError("Tên video chỉ được chứa chữ, số, dấu _ hoặc - và đuôi .mp4")


def safe_asset_name(value) -> str:
    """
    Tên file ảnh hoặc clip trong remotion/public, đã loại bỏ mọi đường dẫn.

    React nạp file này bằng staticFile, nên một cái tên kiểu "../../config.json"
    sẽ đọc ra ngoài thư mục public. Ở đây chỉ nhận đúng tên file do tool tự đặt.
    """
    name = (value or "").strip() if isinstance(value, str) else ""
    if not name:
        return ""
    # Cắt phần đường dẫn ra thì "../../config.json" thành "config.json", tức là
    # vẫn nhận một cái tên người dùng không hề gắn. Thà bỏ hẳn còn hơn: tên hợp
    # lệ đều do tool tự đặt nên không bao giờ có dấu gạch chéo.
    if "/" in name or "\\" in name or ".." in name:
        return ""
    return name if re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name) else ""


def normalize_scenes(scenes, report: Optional[List[str]] = None):
    """
    Chuẩn hoá danh sách cảnh trước khi render.

    Chỉ điền giúp phần TRÌNH BÀY còn thiếu (gradient thẻ, thời lượng, emoji).
    Phần CHỮ (tag, title, desc) bắt buộc phải có sẵn — không lấy từ kịch bản mẫu,
    vì như vậy sẽ tạo ra video lai nội dung không liên quan.

    Từ khoá phát sáng được đối chiếu lại với lời thoại tại đây. Cụm nào không có
    thật trong câu sẽ bị bỏ và ghi vào `report` để giao diện nói rõ, thay vì để
    bạn tưởng đã tô mà video lại không có chữ nào sáng lên.
    """
    if scenes is None:
        scenes = DEFAULT_SCENES
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("Cần ít nhất một cảnh để render")

    result = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f"Dữ liệu cảnh {index + 1} không hợp lệ")

        normalized = dict(scene)
        normalized.pop("audioFile", None)
        # Mốc câu đọc chỉ do bước tạo giọng đo lại, không nhận từ trình duyệt.
        normalized.pop("voiceFrames", None)
        # Mốc từng từ cũng vậy: nhận lại số cũ của trình duyệt là chữ chạy sai.
        normalized.pop("wordFrames", None)
        normalized["id"] = index + 1

        kind = normalized.get("kind")
        kind = kind if kind in ("hook", "body") else "body"
        normalized["kind"] = kind

        # Cảnh hook chỉ có câu móc câu và dòng chủ đề, không có nhãn.
        required = ("title", "desc") if kind == "hook" else ("tag", "title", "desc")
        for key in required:
            value = normalized.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Cảnh {index + 1} thiếu nội dung '{key}'")
            normalized[key] = value.strip()
        for key in ("tag", "title", "desc"):
            value = normalized.get(key)
            normalized[key] = value.strip() if isinstance(value, str) else ""

        icon = normalized.get("icon")
        default_icon = "🔥" if kind == "hook" else "💡"
        normalized["icon"] = icon.strip() if isinstance(icon, str) and icon.strip() else default_icon

        hl = normalized.get("highlightWord")
        normalized["highlightWord"] = hl.strip() if isinstance(hl, str) else ""
        normalized, missing = resolve_highlight(normalized)
        if missing and report is not None:
            report.append(
                f"Cảnh {index + 1}: từ khoá {', '.join(chr(34) + m + chr(34) for m in missing)} "
                f"không có trong lời thoại nên không sáng lên được.")

        normalized["imageFile"] = safe_asset_name(normalized.get("imageFile"))
        # Clip B-roll của Fact Story Studio. Chạy dưới lớp nền của cảnh.
        normalized["videoFile"] = safe_asset_name(normalized.get("videoFile"))
        normalized["lockDuration"] = bool(normalized.get("lockDuration"))

        gradient = normalized.get("bgGradient")
        if not isinstance(gradient, str) or "gradient" not in gradient:
            gradient = SCENE_GRADIENTS[index % len(SCENE_GRADIENTS)]
        normalized["bgGradient"] = gradient

        frames = normalized.get("durationInFrames")
        if frames in (None, ""):
            # Không có thời lượng thì ước lượng theo số từ sẽ đọc lên, thay vì
            # dùng một con số cố định không liên quan gì tới lời thoại.
            frames = seconds_to_frames(estimate_scene_seconds(normalized))
        if isinstance(frames, bool):
            raise ValueError("Thời lượng cảnh phải là số frame nguyên dương")
        if isinstance(frames, str):
            try:
                frames = int(float(frames))
            except (TypeError, ValueError):
                raise ValueError("Thời lượng cảnh phải là số frame nguyên dương") from None
        elif isinstance(frames, float):
            frames = int(frames)
        if type(frames) is not int or frames <= 0:
            raise ValueError("Thời lượng cảnh phải là số frame nguyên dương")
        normalized["durationInFrames"] = frames

        result.append(normalized)
    return result


# Dấu hiệu model chép lại khung ví dụ thay vì thật sự viết kịch bản.
# Các mẫu này cố ý hẹp: chúng đòi phần chú thích đi kèm ("cảnh 2", dấu ":" hoặc
# "(") nên không chặn nhầm tiêu đề thật như "Tiêu điểm dinh dưỡng".
_PLACEHOLDER_HEAD = r"(ti[êe]u ?đ[eề]|m[ôo] t[ảa]|n[ộo]i dung|l[ờo]i tho[ạa]i|t[ừu] kh[óo]a|c[âa]u tho[ạa]i)"
PLACEHOLDER_PATTERNS = [
    rf"^{_PLACEHOLDER_HEAD}\s*(c[ảa]nh)?\s*\d*\s*[\(:]",
    rf"^{_PLACEHOLDER_HEAD}\s*c[ảa]nh\s*\d*\s*$",
    r"^(scene|title|desc|description|placeholder|lorem ipsum|your text)\b",
    r"^[.…\-–—\s]+$",
    r"^x{3,}$",
]


def _looks_like_placeholder(text: str) -> bool:
    low = (text or "").strip().lower()
    if len(low) < 3:
        return True
    return any(re.search(p, low) for p in PLACEHOLDER_PATTERNS)


def _strip_accents(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _word_key(word: str) -> str:
    return _strip_accents(re.sub(r"[.,!?;:…\"“”'’()\[\]]", "", word).strip().lower())


def count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", (text or "").strip()) if w])


# ─────────────────────────────────────────────────────────────────────────────
# ĐỐI TƯỢNG NGƯỜI XEM
# ─────────────────────────────────────────────────────────────────────────────
# Tool không còn mặc định là kênh mẹ và bé. Chủ đề nào cũng làm được, và giọng
# văn của kịch bản đổi theo đối tượng người xem bạn chọn ở ô "Đối tượng".

AUDIENCE_PRESETS: List[Dict] = [
    {"id": "general", "label": "Người xem phổ thông",
     "guide": "Xưng hô \"bạn\". Văn nói đời thường, ai nghe cũng hiểu, không dùng thuật ngữ chuyên ngành."},
    {"id": "parents", "label": "Bố mẹ nuôi con nhỏ",
     "guide": "Xưng hô \"mẹ\" hoặc \"bố mẹ\". Giọng ân cần, ưu tiên an toàn của bé, tránh doạ nạt."},
    {"id": "genz", "label": "Gen Z 16 đến 24 tuổi",
     "guide": "Xưng hô \"bạn\". Nhịp nhanh, câu ngắn, ví von đời sống mạng, tuyệt đối không sến."},
    {"id": "office", "label": "Dân văn phòng đi làm",
     "guide": "Xưng hô \"bạn\". Nói về hiệu suất, thời gian, tiền lương, ví dụ đặt trong bối cảnh công sở."},
    {"id": "seller", "label": "Chủ shop và người bán hàng online",
     "guide": "Xưng hô \"bạn\". Nói bằng ngôn ngữ doanh thu, đơn hàng, chi phí, chốt đơn."},
    {"id": "health", "label": "Người quan tâm sức khoẻ",
     "guide": "Xưng hô \"bạn\". Thận trọng, không hứa chữa khỏi, không bịa số liệu y khoa."},
    {"id": "finance", "label": "Người quan tâm tiền bạc và đầu tư",
     "guide": "Xưng hô \"bạn\". Cụ thể bằng con số và ví dụ, luôn nhắc rủi ro, không phím hàng."},
    {"id": "senior", "label": "Người trung niên và cao tuổi",
     "guide": "Xưng hô \"cô chú\" hoặc \"mình\". Nhịp chậm, câu ngắn, tránh từ tiếng Anh."},
    {"id": "student", "label": "Học sinh sinh viên",
     "guide": "Xưng hô \"bạn\". Ví dụ gắn với học hành, thi cử, ký túc xá, làm thêm."},
]

AUDIENCE_BY_ID = {a["id"]: a for a in AUDIENCE_PRESETS}
DEFAULT_AUDIENCE_ID = "general"


def audience_guide(audience: str) -> Tuple[str, str]:
    """
    Nhận giá trị ô "Đối tượng" và trả về (nhãn hiển thị, hướng dẫn giọng văn).

    Chấp nhận cả mã của một đối tượng dựng sẵn lẫn mô tả tự gõ, ví dụ
    "chủ tiệm bánh ở tỉnh". Tự gõ thì tool dùng nguyên văn làm mô tả đối tượng.
    """
    value = (audience or "").strip()
    if not value:
        preset = AUDIENCE_BY_ID[DEFAULT_AUDIENCE_ID]
        return preset["label"], preset["guide"]
    if value in AUDIENCE_BY_ID:
        preset = AUDIENCE_BY_ID[value]
        return preset["label"], preset["guide"]
    return value[:120], "Chọn cách xưng hô và ví dụ đúng với nhóm người xem này."


# ─────────────────────────────────────────────────────────────────────────────
# THỜI LƯỢNG: TỪ SỐ GIÂY MONG MUỐN RA SỐ CẢNH VÀ SỐ TỪ MỖI CẢNH
# ─────────────────────────────────────────────────────────────────────────────
# Đo trên các video đã render: giọng vi-VN ở tốc độ +5% đọc khoảng 3,6 từ mỗi
# giây. Toàn bộ phần ước lượng thời lượng của tool dựa trên con số này.

# Tốc độ đọc nền, đo thật ở tốc độ +5% (xem VOICE_WORDS_PER_SECOND trong
# tts_engine.py). Mỗi giọng có con số riêng; đây là số dùng khi không biết giọng.
WORDS_PER_SECOND = 4.2
BASE_VOICE_RATE = "+5%"
HOOK_SECONDS = 3.0
TITLE_WORDS_ALLOWANCE = 6
SCENE_PAD_SECONDS = 8 / FPS  # phần đệm mỗi cảnh, khớp với bước tạo giọng đọc
DURATION_CHOICES = [15, 30, 45, 60, 90]
MIN_SCENE_COUNT = 2
MAX_SCENE_COUNT = 8
SECONDS_PER_SCENE_TARGET = 9.0


def parse_rate_percent(rate: str) -> float:
    """Đọc chuỗi tốc độ kiểu "-8%" thành số -8. Không hiểu thì coi như 0."""
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(rate or ""))
    if not match:
        return 0.0
    return max(-50.0, min(100.0, float(match.group())))


def words_per_second(rate: str = BASE_VOICE_RATE, voice: str = "") -> float:
    """
    Số từ đọc được mỗi giây của một giọng ở một tốc độ cụ thể.

    Mỗi giọng có tốc độ đọc riêng, đã đo thật ở tốc độ +5%. Hạ tốc độ thì số từ
    mỗi giây giảm theo đúng tỉ lệ, chuyện này cũng đã đo và khớp. Nhờ vậy chọn
    giọng thủ thỉ chậm là kịch bản tự viết ngắn lại, video vẫn ra đúng mốc thời
    lượng bạn chọn.
    """
    from tts_engine import VOICE_WORDS_PER_SECOND, MEASURED_AT_RATE

    measured = VOICE_WORDS_PER_SECOND.get(voice or "", WORDS_PER_SECOND)
    base = 100.0 + parse_rate_percent(MEASURED_AT_RATE)
    now = 100.0 + parse_rate_percent(rate)
    return max(1.2, measured * now / base)


def plan_script(target_seconds: int = 45, scene_count: Optional[int] = None,
                rate: str = BASE_VOICE_RATE, voice: str = "") -> Dict:
    """
    Đổi thời lượng mong muốn thành một kế hoạch viết cụ thể.

    Trả về số cảnh nội dung, khoảng số từ cho lời thoại mỗi cảnh và số từ cho
    câu hook. Đây là thứ giữ cho video ra đúng độ dài bạn chọn: trước đây tool
    luôn viết 3 cảnh mỗi cảnh 18 đến 32 từ, nên video nào cũng dài hơn 30 giây
    dù giao diện ghi 10 giây.
    """
    try:
        target = int(float(target_seconds))
    except (TypeError, ValueError):
        target = 45
    target = max(10, min(180, target))

    body_seconds = max(6.0, target - HOOK_SECONDS)

    if scene_count:
        try:
            count = int(float(scene_count))
        except (TypeError, ValueError):
            count = 0
    else:
        count = 0
    if count <= 0:
        count = round(body_seconds / SECONDS_PER_SCENE_TARGET)
    count = max(MIN_SCENE_COUNT, min(MAX_SCENE_COUNT, count))

    wps = words_per_second(rate, voice)
    per_scene = body_seconds / count
    # Từ 21/09/2026 tiêu đề cảnh không còn được đọc lên, nên cả ngân sách từ
    # thuộc về lời thoại. Trước đây trừ đi phần dành cho tiêu đề, lời thoại chỉ
    # còn mười mấy từ ở video ngắn, không đủ để nói trọn một ý.
    desc_words = int(round(per_scene * wps))
    desc_words = max(16, desc_words)
    hook_words = max(6, int(round(HOOK_SECONDS * wps)))

    return {
        "target_seconds": target,
        "scene_count": count,
        "seconds_per_scene": round(per_scene, 1),
        "desc_words_min": max(12, desc_words - 5),
        "desc_words_max": desc_words + 5,
        "hook_words": hook_words,
        "rate": rate,
        "voice": voice,
        "words_per_second": round(wps, 2),
    }


def estimate_scene_seconds(scene: Dict, rate: str = BASE_VOICE_RATE,
                           voice: str = "") -> float:
    """Ước lượng thời lượng một cảnh theo số từ sẽ được đọc lên và tốc độ giọng."""
    words = count_words(scene_voice_text(scene))
    if not words:
        return 0.0
    return words / words_per_second(rate, voice) + SCENE_PAD_SECONDS


def estimate_total_seconds(scenes: List[Dict], rate: str = BASE_VOICE_RATE,
                           voice: str = "") -> float:
    return round(sum(estimate_scene_seconds(s, rate, voice) for s in scenes or []), 1)


def seconds_to_frames(seconds: float) -> int:
    return max(FPS, int(math.ceil(seconds * FPS)))


# ─────────────────────────────────────────────────────────────────────────────
# TỪ KHOÁ PHÁT SÁNG
# ─────────────────────────────────────────────────────────────────────────────

_HIGHLIGHT_SPLIT = re.compile(r"\s*[,;|/]\s*|\s+\+\s+")


def split_highlight_phrases(raw: str) -> List[str]:
    """Một ô từ khoá có thể chứa nhiều cụm, ngăn nhau bằng dấu phẩy hoặc gạch đứng."""
    return [p.strip() for p in _HIGHLIGHT_SPLIT.split(raw or "") if p.strip()]


def _match_phrase(desc_words: List[str], keys: List[str], phrase: str) -> Optional[str]:
    """
    Tìm một cụm từ trong lời thoại theo đúng cách React tô sáng: khớp trọn từ,
    liền mạch, bỏ qua dấu câu và dấu tiếng Việt.

    Trả về cụm đúng như nó xuất hiện trong lời thoại, hoặc None nếu không có.
    """
    target = [_word_key(w) for w in re.split(r"\s+", phrase) if _word_key(w)]
    if not target:
        return None
    for i in range(len(keys) - len(target) + 1):
        if keys[i:i + len(target)] == target:
            return " ".join(desc_words[i:i + len(target)])
    # Một từ đơn thì cho phép khớp một phần, ví dụ gõ "dinh dưỡng" khi câu có
    # "dinh dưỡng." kèm dấu chấm.
    if len(target) == 1:
        for i, key in enumerate(keys):
            if target[0] in key:
                return desc_words[i]
    return None


def resolve_highlight(scene: Dict, autopick: bool = False) -> Tuple[Dict, List[str]]:
    """
    Chốt lại từ khoá phát sáng của một cảnh.

    Chỉ những cụm CÓ THẬT trong lời thoại mới sáng lên được, vì phần tô sáng của
    React so khớp trọn từ. Cụm nào không tìm thấy sẽ bị bỏ và báo ra ngoài để
    giao diện nói rõ, thay vì để bạn tưởng đã tô mà video lại không có gì sáng.

    autopick=True (chỉ dùng cho kịch bản AI vừa viết): khi không cụm nào khớp
    thì tự chọn một cụm ở giữa câu để cảnh vẫn có điểm nhấn.
    """
    scene = dict(scene)
    desc_words = [w for w in re.split(r"\s+", scene.get("desc", "") or "") if w]
    if not desc_words:
        scene["highlightWord"] = ""
        return scene, []

    keys = [_word_key(w) for w in desc_words]
    matched, missing = [], []
    for phrase in split_highlight_phrases(scene.get("highlightWord", "")):
        found = _match_phrase(desc_words, keys, phrase)
        if found and found not in matched:
            matched.append(found)
        elif not found:
            missing.append(phrase)

    if not matched and autopick:
        span = 3 if len(desc_words) >= 8 else 2
        start = max(0, min(len(desc_words) - span, len(desc_words) // 2 - 1))
        matched = [" ".join(desc_words[start:start + span])]
        missing = []

    scene["highlightWord"] = ", ".join(matched)
    return scene, missing


def _fix_highlight(scene: Dict) -> Dict:
    """Giữ lại cho phần gọi cũ: chốt từ khoá và tự chọn khi không cụm nào khớp."""
    fixed, _ = resolve_highlight(scene, autopick=True)
    return fixed


# ─────────────────────────────────────────────────────────────────────────────
# KIỂM TRA KỊCH BẢN AI TRẢ VỀ
# ─────────────────────────────────────────────────────────────────────────────

# Lần đầu bắt viết đúng khoảng đã yêu cầu, lố tối đa 3 từ. Lần thử lại thì nới
# ra: một kịch bản dài hơn mốc một chút vẫn dùng được, còn trả về tay không thì
# bạn chẳng có gì để render. Video dài bao nhiêu vẫn được nói thật trên giao diện.
STRICT_WORD_SLACK = 3
LENIENT_WORD_SLACK = 12


def validate_ai_scenes(scenes, scene_count: int = SCENE_COUNT,
                       desc_words_min: int = 6,
                       desc_words_max: Optional[int] = None,
                       lenient: bool = False) -> List[Dict]:
    """
    Kiểm tra nghiêm ngặt phần CẢNH NỘI DUNG do AI trả về. Ném ValueError kèm lý
    do cụ thể để giao diện hiển thị được đúng nguyên nhân thay vì báo chung chung.
    """
    if isinstance(scenes, dict):
        for key in ("scenes", "items", "data", "result", "output", "phancanh"):
            if isinstance(scenes.get(key), list):
                scenes = scenes[key]
                break
    if not isinstance(scenes, list):
        raise ValueError("AI không trả về danh sách phân cảnh")
    if len(scenes) != scene_count:
        raise ValueError(f"AI trả về {len(scenes)} cảnh, cần đúng {scene_count} cảnh")

    cleaned = []
    seen_titles = set()
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise ValueError(f"Cảnh {index} không hợp lệ")

        item = {}
        for field in TEXT_FIELDS:
            value = scene.get(field)
            item[field] = value.strip() if isinstance(value, str) else ""

        for field in ("title", "desc"):
            if not item[field]:
                raise ValueError(f"Cảnh {index} thiếu nội dung '{field}'")
            if _looks_like_placeholder(item[field]):
                raise ValueError(f"Cảnh {index} chỉ chép lại khung mẫu ('{item[field][:40]}'), chưa viết nội dung thật")

        # Lời thoại quá ngắn thì giọng đọc chỉ kịp một nhịp, cảnh trôi qua trước
        # khi người xem đọc xong. Coi như kịch bản chưa đạt và yêu cầu viết lại.
        words = count_words(item["desc"])
        if words < max(6, int(desc_words_min * 0.6)):
            raise ValueError(f"Cảnh {index} có lời thoại quá ngắn ('{item['desc'][:40]}')")
        # Lời thoại dài quá là lý do chính khiến video vượt thời lượng bạn chọn:
        # mỗi từ thừa là thêm chừng ba phần mười giây giọng đọc. Bắt viết lại
        # còn hơn xuất ra một video dài gấp rưỡi mà vẫn báo thành công.
        slack = LENIENT_WORD_SLACK if lenient else STRICT_WORD_SLACK
        if desc_words_max and words > desc_words_max + slack:
            raise ValueError(
                f"Cảnh {index} có lời thoại {words} từ, vượt mức {desc_words_max} từ nên video "
                f"sẽ dài hơn thời lượng đã chọn. Viết ngắn lại đúng khoảng đã yêu cầu")
        # NHỊP. Đủ số từ chưa chắc đã nghe được: đo kịch bản bị chê "cục cục"
        # thấy bốn trên năm cảnh là MỘT câu dài hơn ba mươi từ nối bằng ba tới
        # năm dấu phẩy, đọc lên là một hơi dài không có chỗ ngắt. Đây là chỗ
        # đếm lại, chứ dặn AI viết cho có nhịp thì nó vẫn viết câu chạy dài.
        import luat_loi_thoai
        nhip = luat_loi_thoai.soat_nhip(item["desc"], noi_long=lenient)
        if nhip:
            raise ValueError(f"Cảnh {index} sai nhịp: {nhip[0]}. "
                             f"Tách thành câu ngắn câu dài xen nhau")
        # Cảnh CUỐI phải đóng lại. Đây là chỗ người dùng gọi là "end rất kỳ".
        # Lưu ý giới hạn: chỗ này chỉ bắt được kiểu hỏng đo đếm được, tức câu
        # chốt quá dài hoặc mở bằng một từ báo hiệu còn nối tiếp. Kiểu hỏng
        # nặng nhất là câu chốt thả ra một ý MỚI chưa ai kể, ví dụ "Một đời
        # chưa kịp thống nhất thiên hạ." Cái đó là chuyện nghĩa, máy không đo
        # được, nên nó nằm ở câu dặn AI chứ không nằm ở đây.
        if index == scene_count:
            for x in luat_loi_thoai.soat_cau_ket(item["desc"]):
                raise ValueError(f"Cảnh cuối chưa kết trọn: {x}")
        if count_words(item["title"]) > 12:
            raise ValueError(f"Cảnh {index} có tiêu đề quá dài, cần tối đa 8 từ")

        if not item["tag"]:
            item["tag"] = ["LƯU Ý", "VÌ SAO", "MẸO HAY"][(index - 1) % 3]
        if not item["icon"]:
            item["icon"] = ["⚠️", "❌", "💡"][(index - 1) % 3]

        key = _strip_accents(item["title"].lower())
        if key in seen_titles:
            raise ValueError(f"Cảnh {index} trùng nội dung với cảnh trước")
        seen_titles.add(key)

        item = _fix_highlight(item)
        item["kind"] = "body"
        item["id"] = index
        item["bgGradient"] = SCENE_GRADIENTS[(index - 1) % len(SCENE_GRADIENTS)]
        item["durationInFrames"] = seconds_to_frames(estimate_scene_seconds(item))
        cleaned.append(item)

    return cleaned


def build_hook_scene(hook_line: str, topic_line: str, icon: str = "🔥") -> Dict:
    """
    Cảnh mở đầu: ba giây đầu chỉ có câu móc câu và chủ đề, chưa vào nội dung.

    Chỉ CÂU HOOK được đọc lên. Dòng chủ đề là chữ hiện trên màn hình để người
    lướt qua biết ngay video nói về cái gì mà không tốn thêm giây nào.
    """
    scene = {
        "id": 1,
        "kind": "hook",
        "icon": (icon or "🔥").strip()[:8] or "🔥",
        "tag": "",
        "title": (hook_line or "").strip(),
        "desc": (topic_line or "").strip(),
        "highlightWord": "",
        "bgGradient": SCENE_GRADIENTS[0],
    }
    scene["durationInFrames"] = seconds_to_frames(estimate_scene_seconds(scene))
    return scene


def validate_ai_script(payload, plan: Dict, lenient: bool = False) -> Dict:
    """
    Kiểm tra kịch bản đầy đủ: phần hook cộng các cảnh nội dung.

    Trả về dict gồm scenes (đã có cảnh hook đứng đầu), hook_line và topic_line.
    """
    if isinstance(payload, list):
        raise ValueError("AI mới trả về danh sách cảnh, còn thiếu phần hook 3 giây đầu")
    if not isinstance(payload, dict):
        raise ValueError("AI không trả về đúng cấu trúc kịch bản")

    hook_line = (payload.get("hookLine") or payload.get("hook") or "").strip()
    topic_line = (payload.get("topicLine") or payload.get("topic") or "").strip()
    hook_icon = (payload.get("hookIcon") or "🔥").strip()

    if not hook_line:
        raise ValueError("Thiếu câu hook cho 3 giây đầu")
    if _looks_like_placeholder(hook_line):
        raise ValueError(f"Câu hook chỉ là chữ giữ chỗ ('{hook_line[:40]}')")
    if not topic_line:
        raise ValueError("Thiếu dòng chủ đề hiển thị cùng câu hook")
    if _looks_like_placeholder(topic_line):
        raise ValueError(f"Dòng chủ đề chỉ là chữ giữ chỗ ('{topic_line[:40]}')")
    if count_words(hook_line) > plan["hook_words"] + 8:
        raise ValueError(
            f"Câu hook dài {count_words(hook_line)} từ, quá 3 giây đầu. "
            f"Cần tối đa {plan['hook_words'] + 4} từ")
    if count_words(topic_line) > 10:
        raise ValueError("Dòng chủ đề quá dài, cần tối đa 8 từ")

    body = validate_ai_scenes(payload.get("scenes"), plan["scene_count"],
                              desc_words_min=plan["desc_words_min"],
                              desc_words_max=plan["desc_words_max"],
                              lenient=lenient)

    scenes = [build_hook_scene(hook_line, topic_line, hook_icon)]
    for offset, scene in enumerate(body, 2):
        scene = dict(scene)
        scene["id"] = offset
        scene["bgGradient"] = SCENE_GRADIENTS[(offset - 1) % len(SCENE_GRADIENTS)]
        # ĐÓNG dấu kết câu tại đây, đừng chỉ dặn AI. Câu dặn bảo phải kết bằng
        # dấu chấm, nhưng model bỏ quên là chuyện thường, mà thiếu dấu thì bộ
        # đọc không hạ giọng và câu nghe như bị cắt ngang.
        scene["desc"] = ket_cau(scene.get("desc", ""))
        scenes.append(scene)

    return {"scenes": scenes, "hook_line": hook_line, "topic_line": topic_line}


def _extract_json_blob(content: str) -> str:
    """Bóc phần JSON ra khỏi câu trả lời của model (markdown, lời dẫn, cắt cụt)."""
    text = (content or "").strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    elif text.startswith("```"):
        text = text.lstrip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start = text.find("{")
    bracket = text.find("[")
    if start == -1 or (bracket != -1 and bracket < start):
        start = bracket
    if start > 0:
        text = text[start:]

    end = max(text.rfind("]"), text.rfind("}"))
    if end > 0:
        text = text[:end + 1]
    return text.strip()


def _parse_script_response(content: str, plan: Dict, lenient: bool = False) -> Dict:
    """Parse + kiểm tra kịch bản đầy đủ. Ném ValueError nếu không dùng được."""
    blob = _extract_json_blob(content)
    if not blob:
        raise ValueError("AI trả về nội dung rỗng")
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI trả về JSON hỏng hoặc bị cắt giữa chừng ({exc.msg})") from exc
    return validate_ai_script(parsed, plan, lenient=lenient)


def _parse_scenes_response(content: str, scene_count: int = SCENE_COUNT) -> List[Dict]:
    """Giữ lại cho phần gọi cũ: chỉ đọc danh sách cảnh, không có hook."""
    blob = _extract_json_blob(content)
    if not blob:
        raise ValueError("AI trả về nội dung rỗng")
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI trả về JSON hỏng hoặc bị cắt giữa chừng ({exc.msg})") from exc
    return validate_ai_scenes(parsed, scene_count)


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_SYSTEM_PROMPT = (
    "Bạn là biên kịch video ngắn dọc 9:16 cho TikTok, Reels và YouTube Shorts. "
    "Bạn luôn trả lời bằng DUY NHẤT một JSON Object hợp lệ, không kèm lời dẫn, không kèm markdown."
)

# Ngôn ngữ của kịch bản, chữ trên video và TTS luôn đi cùng một thị trường.
# Không dùng tên quốc gia đơn thuần: English (US) và English (UK) chẳng hạn có
# cách nói/giọng đọc khác nhau.
#
# `timezone` là múi giờ IANA của nơi đông khán giả nhất, `city` là tên hiện
# trên giao diện. Khung giờ đăng của từng thị trường nằm ở `posting_times.py`.
SHORTS_MARKETS = [
    {"id": "vi-VN", "label": "🇻🇳 Việt Nam — Tiếng Việt", "language": "Vietnamese", "market": "Vietnam", "default_voice": "hoaimy_diu", "timezone": "Asia/Ho_Chi_Minh", "city": "Việt Nam"},
    {"id": "en-US", "label": "🇺🇸 Hoa Kỳ — English (US)", "language": "American English", "market": "United States", "default_voice": "jenny_us", "timezone": "America/New_York", "city": "New York"},
    {"id": "en-GB", "label": "🇬🇧 Anh — English (UK)", "language": "British English", "market": "United Kingdom", "default_voice": "sonia_uk", "timezone": "Europe/London", "city": "London"},
    {"id": "es-ES", "label": "🇪🇸 Tây Ban Nha — Español", "language": "Spanish (Spain)", "market": "Spain", "default_voice": "elvira_es", "timezone": "Europe/Madrid", "city": "Madrid"},
    {"id": "pt-BR", "label": "🇧🇷 Brazil — Português", "language": "Brazilian Portuguese", "market": "Brazil", "default_voice": "francisca_br", "timezone": "America/Sao_Paulo", "city": "São Paulo"},
    {"id": "fr-FR", "label": "🇫🇷 Pháp — Français", "language": "French (France)", "market": "France", "default_voice": "denise_fr", "timezone": "Europe/Paris", "city": "Paris"},
    {"id": "de-DE", "label": "🇩🇪 Đức — Deutsch", "language": "German (Germany)", "market": "Germany", "default_voice": "katja_de", "timezone": "Europe/Berlin", "city": "Berlin"},
    {"id": "ja-JP", "label": "🇯🇵 Nhật — 日本語", "language": "Japanese", "market": "Japan", "default_voice": "nanami_jp", "timezone": "Asia/Tokyo", "city": "Tokyo"},
    {"id": "ko-KR", "label": "🇰🇷 Hàn Quốc — 한국어", "language": "Korean", "market": "South Korea", "default_voice": "sunhi_kr", "timezone": "Asia/Seoul", "city": "Seoul"},
    {"id": "th-TH", "label": "🇹🇭 Thái Lan — ไทย", "language": "Thai", "market": "Thailand", "default_voice": "premwadee_th", "timezone": "Asia/Bangkok", "city": "Bangkok"},
    {"id": "id-ID", "label": "🇮🇩 Indonesia — Bahasa Indonesia", "language": "Indonesian", "market": "Indonesia", "default_voice": "gadis_id", "timezone": "Asia/Jakarta", "city": "Jakarta"},
    {"id": "zh-CN", "label": "🇨🇳 Trung Quốc — 简体中文", "language": "Simplified Chinese", "market": "China", "default_voice": "xiaoxiao_cn", "timezone": "Asia/Shanghai", "city": "Bắc Kinh"},
]
SHORTS_MARKET_BY_ID = {market["id"]: market for market in SHORTS_MARKETS}


def get_shorts_market(market_id: str = "") -> Dict:
    """Lấy thị trường hợp lệ; mặc định Việt Nam để tương thích kịch bản cũ."""
    return SHORTS_MARKET_BY_ID.get(market_id, SHORTS_MARKET_BY_ID["vi-VN"])

SCRIPT_PROMPT_TEMPLATE = """Viết kịch bản cho một video ngắn dài khoảng {target_seconds} giây.

CHỦ ĐỀ: "{topic}"
ĐỐI TƯỢNG NGƯỜI XEM: {audience_label}
GIỌNG VĂN: {audience_guide}
{context_block}
Kịch bản gồm HAI phần.

PHẦN A — BA GIÂY ĐẦU (hook và chủ đề):
- "hookLine": câu đầu tiên được đọc lên, tối đa {hook_words} từ, phải khiến người xem dừng lướt ngay. Dùng một câu hỏi thẳng vào nỗi đau, một con số bất ngờ, hoặc một khẳng định ngược đời. Không chào hỏi, không "hôm nay mình sẽ".
- "topicLine": chủ đề video viết lại thật gọn để in lên màn hình, 3 đến 8 từ, không phải câu, không dấu chấm cuối. Đây là chữ giúp người lướt biết ngay video nói về cái gì.
- "hookIcon": đúng một emoji hợp với câu hook.

PHẦN B — "scenes": ĐÚNG {scene_count} phân cảnh nội dung, mỗi cảnh khoảng {seconds_per_scene} giây.
Mạch kể xuyên suốt {scene_count} cảnh: {arc}
Mỗi phân cảnh là một object có đúng 5 trường:
- "icon": ĐÚNG một emoji hợp với nội dung cảnh.
- "tag": nhãn ngắn 2 đến 4 từ, VIẾT HOA toàn bộ.
- "title": chữ HIỆN TRÊN MÀN HÌNH cho cảnh này, tối đa 8 từ, không dấu chấm cuối câu. Chữ này KHÔNG được đọc lên, nên đừng đặt thông tin quan trọng chỉ ở đây; người nghe mà không nhìn màn hình vẫn phải hiểu đủ.
- "desc": TOÀN BỘ lời thoại được đọc lên cho cảnh này, {desc_words_min} đến {desc_words_max} từ, văn nói tự nhiên như người thật đang kể.
- "highlightWord": một cụm 1 đến 4 từ, BẮT BUỘC xuất hiện NGUYÊN VĂN và LIỀN MẠCH bên trong "desc" của chính cảnh đó.

QUY TẮC VỀ LỜI THOẠI — quan trọng nhất, vì đây là thứ người xem NGHE:
- Nối {scene_count} cảnh lại phải đọc ra thành MỘT đoạn kể liền mạch, không phải {scene_count} mẩu rời. Trước khi trả về, hãy đọc thầm các "desc" nối tiếp nhau xem có trôi không.
- Mỗi "desc" phải là CÂU HOÀN CHỈNH và kết thúc bằng dấu chấm, chấm hỏi hoặc chấm than. Không bỏ lửng, không kết bằng dấu phẩy hay ba chấm.
- Mỗi "desc" phải nói TRỌN MỘT Ý. Thà nói một ý cho xong còn hơn nhắc ba ý mà ý nào cũng dở dang.
- Cảnh thứ hai trở đi phải bắt vào ý cảnh trước, ví dụ mở bằng "Lý do là", "Nhưng", "Vậy nên", "Cái khó là". Đừng cảnh nào cũng bắt đầu lạnh như mới vào bài.
- "desc" KHÔNG được chép lại hay nhắc lại "title" của chính cảnh đó, vì người xem đã đọc chữ ấy trên màn hình rồi.
- Cảnh CUỐI CÙNG phải nghe ra là đã hết chuyện, không được cụt lửng.

QUY TẮC BẮT BUỘC:
- Số từ của "desc" quyết định độ dài video. ĐẾM SỐ TỪ trước khi trả về: mỗi "desc" phải nằm trong khoảng {desc_words_min} đến {desc_words_max} từ. Viết dài hơn mức này là video vượt thời lượng và bị trả lại.
- Cả {scene_count} cảnh phải bám sát chủ đề đã cho và nội dung khác nhau hoàn toàn.
- "desc" là câu nói, không phải gạch đầu dòng. Không viết hoa cả câu, không nhồi emoji vào desc.
- Không dùng chữ giữ chỗ như "Tiêu đề cảnh 1", "Mô tả...", "từ khóa", dấu ba chấm thay nội dung.
- Không bịa số liệu. Nếu không chắc thì diễn đạt định tính.
- KHÔNG viết lời kêu gọi theo dõi, like, share hay lưu bài vào bất kỳ cảnh nào. Phần đó do người dùng tự đặt bên ngoài kịch bản.
- Chỉ trả về JSON Object. Không thêm bất kỳ chữ nào khác.

Định dạng đầu ra (thay bằng nội dung thật, tuyệt đối không chép lại dòng chữ minh hoạ này):
{{"topicLine":"...","hookIcon":"...","hookLine":"...","scenes":[{{"icon":"...","tag":"...","title":"...","desc":"...","highlightWord":"..."}}]}}"""

CONTEXT_BLOCKS = {
    "article": """
TƯ LIỆU GỐC — một bài báo. Toàn bộ kịch bản phải dựa trên tư liệu này:
\"\"\"
{context}
\"\"\"
Rút ra ý chính của bài, kể lại bằng văn nói của bạn. Không thêm thông tin ngoài bài. Không bịa số liệu bài không có. Giữ nguyên các con số và tên riêng bài đã nêu.
""",
    "product": """
TƯ LIỆU GỐC — mô tả sản phẩm trong ảnh đính kèm:
\"\"\"
{context}
\"\"\"
Kịch bản phải nói đúng về sản phẩm trong ảnh. Nêu lợi ích người mua nhận được, không chỉ liệt kê tính năng. Không bịa giá, không bịa thương hiệu, không hứa hiệu quả không có căn cứ.
""",
    "notes": """
TƯ LIỆU GỐC — ghi chú của người dùng:
\"\"\"
{context}
\"\"\"
Bám sát ghi chú này khi viết.
""",
}

# ─── Kiểu nội dung ───────────────────────────────────────────────────────────
#
# Đo ngày 21/09/2026: sinh một Shorts về "Gia Cát Lượng" bằng mạch kể mặc định
# thì ra thế này:
#
#   "...sai lầm thường gặp là bỏ qua phần chuẩn bị cực kỳ kỷ luật."
#   "Vậy nên nếu học ông, bạn làm ba bước đơn giản: xác định mục tiêu..."
#   "...lưu ý cuối là mưu hay vẫn phải đi cùng đạo đức."
#
# Đó không phải video kể về Gia Cát Lượng, đó là video dạy kỹ năng có nhắc tên
# ông. Nguyên nhân: mạch kể duy nhất của tool là mạch mẹo vặt, nêu vấn đề rồi
# chỉ sai lầm rồi đưa cách làm đúng. Ép một đời người vào khuôn ấy thì cảnh nào
# cũng phải bẻ cong, và nghe ra vừa cụt vừa sáo.
#
# Nên tách ra: mỗi kiểu nội dung một mạch kể và một bộ luật riêng.
KIEU_MEO = "meo"
KIEU_NHAN_VAT = "nhan_vat"

KIEU_NOI_DUNG = [
    {"id": KIEU_MEO, "label": "Mẹo và hướng dẫn",
     "mo_ta": "Nêu vấn đề, chỉ sai lầm, đưa cách làm đúng."},
    {"id": KIEU_NHAN_VAT, "label": "Kể chuyện nhân vật",
     "mo_ta": "Nhân vật lịch sử, danh nhân, người nổi tiếng. Kể chuyện, không dạy đời."},
]
KIEU_HOP_LE = {k["id"] for k in KIEU_NOI_DUNG}

# Mạch kể cho nhân vật. Mỗi cảnh là MỘT KHOẢNH KHẮC có thể nhìn thấy, không
# phải một luận điểm. Đây là điểm khác căn bản so với mạch mẹo vặt.
ARC_NHAN_VAT = {
    2: "cảnh 1 đặt nhân vật vào đúng một khoảnh khắc căng nhất đời ông, cảnh 2 nói chuyện gì xảy ra sau đó và ông để lại gì.",
    3: "cảnh 1 đặt vào một khoảnh khắc căng, cảnh 2 kể ông đã làm gì trong khoảnh khắc ấy, cảnh 3 nói hệ quả và thứ còn lại tới hôm nay.",
    4: "cảnh 1 một khoảnh khắc căng, cảnh 2 ông là ai trước khoảnh khắc đó, cảnh 3 ông đã làm gì và trả giá thế nào, cảnh 4 thứ còn lại tới hôm nay.",
    5: "cảnh 1 một khoảnh khắc căng, cảnh 2 ông là ai trước đó, cảnh 3 bước ngoặt buộc ông chọn, cảnh 4 ông chọn gì và trả giá ra sao, cảnh 5 thứ còn lại tới hôm nay.",
    6: "cảnh 1 một khoảnh khắc căng, cảnh 2 ông là ai trước đó, cảnh 3 thế lực cản đường, cảnh 4 bước ngoặt buộc ông chọn, cảnh 5 ông chọn gì và trả giá ra sao, cảnh 6 thứ còn lại tới hôm nay.",
}

# Luật riêng cho kiểu nhân vật, ghép thêm vào cuối câu lệnh.
LUAT_NHAN_VAT = """

ĐÂY LÀ VIDEO KỂ CHUYỆN VỀ MỘT CON NGƯỜI, KHÔNG PHẢI VIDEO DẠY KỸ NĂNG.
Mấy luật sau đè lên mọi luật ở trên nếu có chỗ nào trái nhau:
- TUYỆT ĐỐI KHÔNG viết lời khuyên cho người xem. Cấm "bạn nên", "bạn hãy", "nếu học ông", "ba bước", "bài học rút ra", "áp dụng vào công việc". Người xem tới để nghe chuyện, không tới để bị dạy.
- KHÔNG có "sai lầm thường gặp" và KHÔNG có "cách làm đúng". Một đời người không có hai thứ đó.
- Mỗi "desc" kể MỘT KHOẢNH KHẮC NHÌN THẤY ĐƯỢC: ai đang ở đâu, đang làm gì, ai đứng trước mặt. Không viết nhận định chung chung kiểu "ông là nhà chiến lược lỗi lạc".
- Nêu mốc cụ thể khi chắc chắn: năm, tuổi, địa danh, tên người. Không chắc thì bỏ hẳn, TUYỆT ĐỐI KHÔNG bịa năm hay con số.
- Gọi nhân vật bằng tên, đừng gọi "ông ấy" suốt cả video.
- Cảnh cuối nói THỨ CÒN LẠI: công trình, câu nói, hậu quả, hoặc một câu hỏi còn treo. Không chốt bằng bài học đạo đức.
- Câu hook không được là một câu hỏi tu từ sáo rỗng. Hãy dùng một nghịch lý hoặc một con số thật của chính đời nhân vật."""

ARC_BY_COUNT = {
    2: "cảnh 1 nêu vấn đề và lý do, cảnh 2 đưa cách làm đúng.",
    3: "cảnh 1 nêu vấn đề, cảnh 2 giải thích vì sao kèm chi tiết cụ thể, cảnh 3 đưa cách làm đúng tới mức làm theo được ngay.",
    4: "cảnh 1 nêu vấn đề, cảnh 2 chỉ ra sai lầm thường gặp, cảnh 3 giải thích cơ chế đằng sau, cảnh 4 đưa cách làm đúng.",
    5: "cảnh 1 nêu vấn đề, cảnh 2 chỉ ra sai lầm thường gặp, cảnh 3 giải thích cơ chế, cảnh 4 đưa cách làm đúng từng bước, cảnh 5 nêu kết quả nhận được và lưu ý cuối.",
    6: "cảnh 1 nêu vấn đề, cảnh 2 hậu quả nếu bỏ qua, cảnh 3 sai lầm thường gặp, cảnh 4 giải thích cơ chế, cảnh 5 cách làm đúng từng bước, cảnh 6 kết quả nhận được và lưu ý cuối.",
}


def build_script_prompt(topic: str, plan: Optional[Dict] = None, audience: str = "",
                        context: str = "", context_kind: str = "", reason: str = "",
                        scene_count: Optional[int] = None, duration_hint: Optional[int] = None,
                        market_id: str = "vi-VN", kind_noi_dung: str = KIEU_MEO) -> str:
    """
    Dựng prompt viết kịch bản.

    plan do plan_script() sinh ra, quyết định số cảnh và số từ mỗi cảnh nên
    video ra đúng thời lượng bạn chọn.
    """
    if plan is None:
        plan = plan_script(duration_hint or 45, scene_count)
    label, guide = audience_guide(audience)

    context_block = ""
    if context:
        template = CONTEXT_BLOCKS.get(context_kind or "notes", CONTEXT_BLOCKS["notes"])
        context_block = template.format(context=context.strip()[:12000])

    count = plan["scene_count"]
    kieu = kind_noi_dung if kind_noi_dung in KIEU_HOP_LE else KIEU_MEO
    if kieu == KIEU_NHAN_VAT:
        arc = ARC_NHAN_VAT.get(count) or (
            f"cảnh 1 đặt vào một khoảnh khắc căng, các cảnh giữa kể nhân vật đã làm gì "
            f"và trả giá thế nào, cảnh {count} nói thứ còn lại tới hôm nay.")
    else:
        arc = ARC_BY_COUNT.get(count) or (
            f"cảnh 1 nêu vấn đề, các cảnh giữa bóc tách nguyên nhân và sai lầm thường gặp, "
            f"cảnh {count} đưa cách làm đúng và kết luận trọn vẹn.")

    market = get_shorts_market(market_id)
    prompt = SCRIPT_PROMPT_TEMPLATE.format(
        topic=topic.strip(),
        target_seconds=plan["target_seconds"],
        audience_label=label,
        audience_guide=guide,
        context_block=context_block,
        hook_words=plan["hook_words"],
        scene_count=count,
        seconds_per_scene=plan["seconds_per_scene"],
        desc_words_min=plan["desc_words_min"],
        desc_words_max=plan["desc_words_max"],
        arc=arc,
    )
    prompt += (
        f"\n\nNGÔN NGỮ & THỊ TRƯỜNG BẮT BUỘC:\n"
        f"- Viết TOÀN BỘ hookLine, topicLine, tag, title, desc và highlightWord bằng {market['language']} tự nhiên cho khán giả tại {market['market']}.\n"
        "- Bản địa hoá cách nói, ví dụ và nhịp câu như video ngắn của người bản xứ; không dịch từng chữ.\n"
        "- Giữ tên riêng, số liệu từ tư liệu gốc; các khoá JSON vẫn giữ tiếng Anh.\n"
    )
    import luat_loi_thoai
    prompt += luat_loi_thoai.LUAT_NHIP
    if kieu == KIEU_NHAN_VAT:
        prompt += LUAT_NHAN_VAT
    if reason:
        prompt += RETRY_SUFFIX.format(reason=reason, scene_count=count)
    return prompt


RETRY_SUFFIX = """

LẦN TRƯỚC BẠN TRẢ VỀ SAI. Lý do: {reason}
Hãy viết lại từ đầu, sửa đúng lỗi trên, và chỉ trả về JSON Object gồm hookLine, topicLine, hookIcon và {scene_count} phần tử trong scenes."""


# ─────────────────────────────────────────────────────────────────────────────
# ẢNH ĐÍNH KÈM
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def read_image_part(image_path: str) -> Tuple[str, str]:
    """Đọc ảnh thành (kiểu file, chuỗi base64) để gửi kèm cho model nhìn ảnh."""
    import base64

    if not image_path or not os.path.isfile(image_path):
        raise ValueError("Không tìm thấy file ảnh")
    ext = os.path.splitext(image_path)[1].lower()
    mime = IMAGE_MIME.get(ext)
    if not mime:
        raise ValueError("Ảnh phải là JPG, PNG, WEBP hoặc GIF")
    size = os.path.getsize(image_path)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"Ảnh nặng {size / 1024 / 1024:.1f} MB, tối đa 8 MB")
    with open(image_path, "rb") as f:
        return mime, base64.b64encode(f.read()).decode("ascii")


# ─────────────────────────────────────────────────────────────────────────────
# 9ROUTER / OPENAI
# ─────────────────────────────────────────────────────────────────────────────

def _read_chat_content(res: requests.Response) -> str:
    """Đọc nội dung từ /chat/completions, chịu được cả khi proxy trả về SSE."""
    try:
        return (res.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        pass
    chunks = []
    for line in res.text.splitlines():
        line = line.strip()
        if not line.startswith("data:") or line.endswith("[DONE]"):
            continue
        try:
            payload = json.loads(line[5:].strip())
            choice = payload.get("choices", [{}])[0]
            piece = (choice.get("delta", {}) or {}).get("content") or (choice.get("message", {}) or {}).get("content")
            if piece:
                chunks.append(piece)
        except Exception:
            continue
    return "".join(chunks).strip()


def generate_script_with_9router(
    topic: str,
    plan: Dict,
    base_url: str = "http://localhost:20128/v1",
    model: str = "combo1",
    api_key: str = "9router-key",
    timeout: int = 120,
    audience: str = "",
    context: str = "",
    context_kind: str = "",
    image_path: str = "",
    reason: str = "",
    lenient: bool = False,
    market_id: str = "vi-VN",
    kind_noi_dung: str = KIEU_MEO
) -> Tuple[Optional[Dict], str]:
    """Gọi 9Router/OpenAI sinh kịch bản đầy đủ. Trả về (script, error_message)."""
    headers = {"Authorization": f"Bearer {api_key or '9router-key'}", "Content-Type": "application/json"}
    prompt = build_script_prompt(topic, plan=plan, audience=audience,
                                 kind_noi_dung=kind_noi_dung,
                                 context=context, context_kind=context_kind, reason=reason,
                                 market_id=market_id)

    if image_path:
        try:
            mime, b64 = read_image_part(image_path)
        except ValueError as exc:
            return None, str(exc)
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
    else:
        user_content = prompt

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SCRIPT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.8,
        "max_tokens": 4000,
        "stream": False,
    }

    try:
        res = requests.post(f"{base_url.rstrip('/')}/chat/completions",
                            headers=headers, json=body, timeout=timeout)
        if res.status_code == 400 and "max_tokens" in res.text:
            body.pop("max_tokens", None)
            res = requests.post(f"{base_url.rstrip('/')}/chat/completions",
                                headers=headers, json=body, timeout=timeout)

        if res.status_code != 200:
            detail = res.text[:200]
            try:
                err = res.json()
                detail = err.get("error", {}).get("message") or err.get("message") or detail
            except Exception:
                pass
            if image_path:
                detail = f"{detail} (model đang chọn có thể không đọc được ảnh)"
            if res.status_code == 401 and api_key in ("", None, "9router-key"):
                detail = ("Chưa điền khoá 9Router trong phần Cấu hình AI, nên tool đang gọi bằng "
                          "chuỗi giữ chỗ. Điền khoá thật rồi thử lại.")
            return None, f"9Router HTTP {res.status_code}: {detail}"

        content = _read_chat_content(res)
        if not content:
            return None, "9Router trả về nội dung rỗng"
        return _parse_script_response(content, plan, lenient), ""

    except requests.exceptions.ConnectionError:
        return None, f"Không kết nối được 9Router tại {base_url}. Kiểm tra 9Router đã chạy chưa."
    except requests.exceptions.Timeout:
        return None, f"9Router quá thời gian chờ ({timeout}s). Combo có thể đang dùng model thinking quá nặng."
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Lỗi gọi 9Router: {exc}"


def generate_scenes_with_9router(
    topic: str,
    base_url: str = "http://localhost:20128/v1",
    model: str = "combo1",
    api_key: str = "9router-key",
    timeout: int = 90,
    scene_count: int = SCENE_COUNT,
    reason: str = "",
) -> Tuple[Optional[List[Dict]], str]:
    """Giữ lại cho phần gọi cũ: chỉ lấy danh sách cảnh, bỏ phần hook."""
    plan = plan_script(45, scene_count)
    script, err = generate_script_with_9router(
        topic, plan, base_url=base_url, model=model, api_key=api_key,
        timeout=timeout, reason=reason)
    if not script:
        return None, err
    return [s for s in script["scenes"] if s.get("kind") != "hook"], ""


# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE GEMINI (dự phòng, và là nơi xử lý ảnh sản phẩm)
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_SCENE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "icon": {"type": "STRING"},
            "tag": {"type": "STRING"},
            "title": {"type": "STRING"},
            "desc": {"type": "STRING",
                     "description": "Lời thoại đọc lên, phải nằm đúng trong khoảng số từ mà đề bài yêu cầu"},
            "highlightWord": {"type": "STRING"},
        },
        "required": ["icon", "tag", "title", "desc", "highlightWord"],
    },
}

GEMINI_SCRIPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "topicLine": {"type": "STRING"},
        "hookIcon": {"type": "STRING"},
        "hookLine": {"type": "STRING"},
        "scenes": GEMINI_SCENE_SCHEMA,
    },
    "required": ["topicLine", "hookIcon", "hookLine", "scenes"],
}

GEMINI_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-3.7-flash", "gemini-3.1-flash-lite"]

# Ngân sách suy luận khi có ảnh sản phẩm. Đọc một tấm ảnh rồi viết kịch bản bán
# hàng cần model nghĩ trước, nên ở luồng này ta BẬT thinking thay vì tắt như
# luồng chữ. Đổi lại phải nới hạn mức token đầu ra, nếu không phần suy luận ăn
# hết token và câu trả lời bị cắt ngang.
IMAGE_THINKING_BUDGET = 4096
IMAGE_MAX_OUTPUT_TOKENS = 12288


def generate_script_with_gemini(
    topic: str,
    plan: Dict,
    gemini_api_key: str,
    gemini_model: str = "gemini-2.5-flash",
    timeout: int = 90,
    audience: str = "",
    context: str = "",
    context_kind: str = "",
    image_path: str = "",
    thinking: bool = False,
    reason: str = "",
    lenient: bool = False,
    market_id: str = "vi-VN",
    kind_noi_dung: str = KIEU_MEO
) -> Tuple[Optional[Dict], str]:
    """
    Gọi thẳng Google Gemini, dùng structured output (responseSchema) nên model
    không thể trả về JSON hỏng, thiếu trường hay lẫn lời dẫn.

    thinking=False (mặc định, luồng chữ): tắt hẳn bước suy luận. Gemini 2.5 có
    bước suy luận nội bộ, không tắt thì phần đó ăn hết hạn mức token và câu trả
    lời bị cắt ngang — đúng kiểu lỗi làm mất cảnh cuối.
    thinking=True (luồng ảnh sản phẩm): bật suy luận có giới hạn kèm hạn mức
    token đầu ra lớn hơn.
    """
    if not gemini_api_key:
        return None, "Chưa cấu hình Gemini API Key trong phần Cấu hình AI."

    prompt = build_script_prompt(topic, plan=plan, audience=audience,
                                 kind_noi_dung=kind_noi_dung,
                                 context=context, context_kind=context_kind, reason=reason,
                                 market_id=market_id)

    parts: List[Dict] = [{"text": prompt}]
    if image_path:
        try:
            mime, b64 = read_image_part(image_path)
        except ValueError as exc:
            return None, str(exc)
        parts.append({"inlineData": {"mimeType": mime, "data": b64}})

    candidates = [gemini_model] + [m for m in GEMINI_FALLBACK_MODELS if m != gemini_model]

    # Ghi lỗi của TỪNG model. Trước đây chỉ giữ lỗi của model cuối cùng, nên
    # khi model đầu trả lời được mà kịch bản không đạt, lý do thật bị che mất
    # sau một dòng "đang quá tải" của model chẳng liên quan.
    errors: List[str] = []
    use_thinking = bool(thinking)
    for model in candidates:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_api_key}"
        generation_config = {
            "temperature": 0.8,
            "maxOutputTokens": IMAGE_MAX_OUTPUT_TOKENS if use_thinking else 8192,
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_SCRIPT_SCHEMA,
            "thinkingConfig": {"thinkingBudget": IMAGE_THINKING_BUDGET if use_thinking else 0},
        }
        payload = {
            "systemInstruction": {"parts": [{"text": SCRIPT_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }

        try:
            res = requests.post(url, json=payload, timeout=timeout)

            # Model cũ không hiểu thinkingConfig / responseSchema thì bỏ dần ra rồi thử lại.
            if res.status_code == 400:
                for drop in ("thinkingConfig", "responseSchema"):
                    if drop in generation_config:
                        generation_config.pop(drop, None)
                        res = requests.post(url, json=payload, timeout=timeout)
                        if res.status_code == 200:
                            break

            if res.status_code != 200:
                detail = res.text[:200]
                try:
                    detail = res.json().get("error", {}).get("message", detail)
                except Exception:
                    pass
                message = f"Gemini HTTP {res.status_code} ({model}): {detail}"
                errors.append(message)
                # 400/404: model không hiểu yêu cầu hoặc không tồn tại.
                # 429/500/503: Google đang quá tải hoặc chặn tạm thời.
                # Cả hai nhóm đều nên thử tiếp model sau thay vì bỏ cuộc ngay,
                # vì lúc Gemini quá tải thường chỉ một model bị chứ không phải tất cả.
                if res.status_code in (400, 404, 429, 500, 502, 503, 504):
                    continue
                return None, " | ".join(errors)

            data = res.json()
            candidate = (data.get("candidates") or [{}])[0]
            finish = candidate.get("finishReason", "")
            content_parts = (candidate.get("content", {}) or {}).get("parts") or []
            content = "".join(p.get("text", "") for p in content_parts).strip()

            if not content:
                errors.append(f"Gemini ({model}) không trả về nội dung (finishReason={finish or 'không rõ'})")
                use_thinking = False  # lần sau tắt suy luận cho chắc
                continue
            if finish == "MAX_TOKENS":
                errors.append(f"Gemini ({model}) bị cắt giữa chừng vì chạm giới hạn token")
                use_thinking = False
                continue

            return _parse_script_response(content, plan, lenient), ""

        except ValueError as exc:
            errors.append(f"Gemini ({model}): {exc}")
            continue
        except Exception as exc:
            errors.append(f"Lỗi gọi Gemini ({model}): {exc}")
            continue

    return None, " | ".join(errors) or "Gemini không phản hồi"


def generate_scenes_with_gemini(
    topic: str,
    gemini_api_key: str,
    gemini_model: str = "gemini-2.5-flash",
    timeout: int = 60,
    scene_count: int = SCENE_COUNT,
    reason: str = "",
) -> Tuple[Optional[List[Dict]], str]:
    """Giữ lại cho phần gọi cũ: chỉ lấy danh sách cảnh, bỏ phần hook."""
    plan = plan_script(45, scene_count)
    script, err = generate_script_with_gemini(
        topic, plan, gemini_api_key=gemini_api_key, gemini_model=gemini_model,
        timeout=timeout, reason=reason)
    if not script:
        return None, err
    return [s for s in script["scenes"] if s.get("kind") != "hook"], ""


# ─────────────────────────────────────────────────────────────────────────────
# ĐIỀU PHỐI: 9ROUTER → GEMINI, MỖI BÊN ĐƯỢC THỬ LẠI MỘT LẦN
# ─────────────────────────────────────────────────────────────────────────────

def generate_scenes(
    topic: str,
    base_url: str = "http://localhost:20128/v1",
    model: str = "combo1",
    api_key: str = "9router-key",
    gemini_api_key: str = "",
    gemini_model: str = "gemini-2.5-flash",
    scene_count: Optional[int] = None,
    audience: str = "",
    target_seconds: int = 45,
    context: str = "",
    context_kind: str = "",
    image_path: str = "",
    voice_rate: str = BASE_VOICE_RATE,
    voice_name: str = "",
    market_id: str = "vi-VN",
    kind_noi_dung: str = KIEU_MEO
) -> Dict:
    """
    Viết kịch bản đầy đủ: câu hook ba giây đầu cộng các cảnh nội dung.

    Trả về dict:
        success   bool
        scenes    list | None   (None khi thất bại — KHÔNG trả kịch bản mẫu)
        source    "9router" | "gemini" | ""
        warning   str           (9Router lỗi nhưng Gemini cứu được)
        error     str           (cả hai đều hỏng)
        attempts  list[str]     (nhật ký từng lần thử, để hiện chi tiết lỗi trên UI)
        plan      dict          (số cảnh và số từ đã dùng để viết)
        topic_line / hook_line  (chữ hiện trong ba giây đầu)
        estimated_seconds float (thời lượng ước tính trước khi tạo giọng đọc)
    """
    # Giọng chậm thì cùng một câu đọc lâu hơn, nên số từ phải tính theo tốc độ
    # của chính giọng bạn đang chọn.
    plan = plan_script(target_seconds, scene_count, rate=voice_rate, voice=voice_name)
    attempts: List[str] = []

    def _try(label, fn):
        script, err = fn("", False)
        if script:
            attempts.append(f"{label}: thành công")
            return script, ""
        attempts.append(f"{label}: {err}")
        # Thử lại một lần, nói rõ cho model biết nó sai ở đâu, và nới mức chặn
        # độ dài lời thoại để không trả về tay không chỉ vì lố vài từ.
        script, err2 = fn(err, True)
        if script:
            attempts.append(f"{label} (thử lại): thành công")
            return script, ""
        attempts.append(f"{label} (thử lại): {err2}")
        return None, err2 or err

    def _ok(script: Dict, source: str, used_model: str, warning: str) -> Dict:
        seconds = estimate_total_seconds(script["scenes"], voice_rate, voice_name)
        target = plan["target_seconds"]
        # Lệch nhiều thì nói thẳng ngay ở dòng trạng thái, đừng để bạn tự phát
        # hiện lúc video đã render xong.
        if seconds > target * 1.2:
            length_note = (f"Kịch bản này sẽ ra video khoảng {seconds} giây, dài hơn mốc "
                           f"{target} giây bạn chọn. Xoá bớt chữ trong lời thoại là về đúng mốc.")
            warning = f"{warning} {length_note}".strip() if warning else length_note
        return {
            "success": True,
            "scenes": script["scenes"],
            "hook_line": script["hook_line"],
            "topic_line": script["topic_line"],
            "source": source,
            "model": used_model,
            "plan": plan,
            "estimated_seconds": seconds,
            "warning": warning,
            "error": "",
            "attempts": attempts,
        }

    # Người dùng có thể chọn bỏ qua hẳn 9Router để so sánh kết quả với Gemini.
    force_gemini = (model or "").strip() == FORCE_GEMINI

    router_error = ""
    if base_url and not force_gemini:
        script, router_error = _try(
            "9Router",
            lambda reason, lenient: generate_script_with_9router(
                topic, plan, base_url=base_url, model=model, api_key=api_key,
                audience=audience, context=context, context_kind=context_kind,
                image_path=image_path, reason=reason, lenient=lenient, market_id=market_id,
                kind_noi_dung=kind_noi_dung),
        )
        if script:
            return _ok(script, "9router", model, "")

    script, gemini_error = _try(
        "Gemini",
        lambda reason, lenient: generate_script_with_gemini(
            topic, plan, gemini_api_key=gemini_api_key, gemini_model=gemini_model,
            audience=audience, context=context, context_kind=context_kind,
            image_path=image_path, thinking=bool(image_path), reason=reason,
            lenient=lenient, market_id=market_id, kind_noi_dung=kind_noi_dung),
    )
    if script:
        return _ok(script, "gemini", gemini_model,
                   "" if force_gemini
                   else f"9Router lỗi ({router_error}). Đã tự chuyển sang {gemini_model}.")

    return {
        "success": False, "scenes": None, "source": "", "model": "", "warning": "",
        "hook_line": "", "topic_line": "", "plan": plan, "estimated_seconds": 0.0,
        "error": (f"Gemini: {gemini_error}" if force_gemini
                  else f"9Router: {router_error or 'không dùng được'} | Gemini: {gemini_error}"),
        "attempts": attempts,
    }




# ─────────────────────────────────────────────────────────────────────────────
# GIỌNG ĐỌC
# ─────────────────────────────────────────────────────────────────────────────

def _ffmpeg_bin() -> str:
    local = os.path.join(os.path.dirname(REMOTION_DIR), "ffmpeg")
    if os.path.isfile(local):
        return local
    return shutil.which("ffmpeg") or "ffmpeg"


# Cắt khoảng lặng thừa hai đầu để giọng đọc vào đúng lúc cảnh xuất hiện.
_TRIM_HEAD = "silenceremove=start_periods=1:start_silence=0:start_threshold=-45dB:detection=peak"
_TRIM_TAIL = f"areverse,{_TRIM_HEAD},areverse"
_TRIM_FILTER = f"{_TRIM_HEAD},{_TRIM_TAIL}"


def _wav_seconds(path: str) -> float:
    """Độ dài một file wav, đọc thẳng từ header nên không cần gọi ffprobe."""
    with wave.open(path, "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def _mp3_to_trimmed_wav(ffmpeg: str, mp3: str, out_wav: str, work: str) -> float:
    """
    Cắt khoảng lặng hai đầu, và trả về số giây đã cắt ở ĐẦU file.

    Phải biết con số này thì mốc từng từ Edge TTS trả về mới dùng được: mốc đó
    tính từ đầu file gốc, còn file cuối đã bị xén mất phần lặng dẫn vào.
    Vì vậy cắt làm hai lượt thay vì một lượt như trước.
    """
    raw = os.path.join(work, "raw.wav")
    head = os.path.join(work, "head.wav")
    base = [ffmpeg, "-y", "-v", "error"]
    tail = ["-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le"]
    subprocess.run(base + ["-i", mp3] + tail + [raw], check=True, capture_output=True, timeout=60)
    subprocess.run(base + ["-i", raw, "-af", _TRIM_HEAD] + tail + [head],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(base + ["-i", head, "-af", _TRIM_TAIL] + tail + [out_wav],
                   check=True, capture_output=True, timeout=60)
    lead = _wav_seconds(raw) - _wav_seconds(head)
    for path in (raw, head):
        try:
            os.remove(path)
        except OSError:
            pass
    return max(0.0, lead)


def marks_path(voice_path: str) -> str:
    """File mốc từng từ nằm cạnh file giọng, cùng mã băm nên cùng vòng đời."""
    return os.path.splitext(voice_path)[0] + ".marks.json"


def _purge_orphan_marks(keep: List[str]) -> None:
    """Xoá file mốc của những giọng đã không còn dùng, tránh phình thư mục."""
    wanted = {os.path.basename(marks_path(p)) for p in keep}
    try:
        names = os.listdir(REMOTION_PUBLIC_DIR)
    except OSError:
        return
    for name in names:
        if name.startswith("voice_") and name.endswith(".marks.json") and name not in wanted:
            try:
                os.remove(os.path.join(REMOTION_PUBLIC_DIR, name))
            except OSError:
                pass


def store_product_image(data: bytes, ext: str) -> str:
    """
    Cất ảnh sản phẩm vào thư mục public của Remotion và trả về tên file.

    Tên đặt theo mã băm nội dung nên tải lại đúng tấm ảnh cũ thì dùng lại file
    cũ, không sinh rác. React đọc ảnh này qua staticFile().
    """
    import hashlib

    ext = (ext or "").lower()
    if not ext.startswith("."):
        ext = "." + ext
    if ext not in IMAGE_MIME:
        raise ValueError("Ảnh phải là JPG, PNG, WEBP hoặc GIF")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Ảnh nặng {len(data) / 1024 / 1024:.1f} MB, tối đa 8 MB")
    if not data:
        raise ValueError("File ảnh rỗng")

    os.makedirs(REMOTION_PUBLIC_DIR, exist_ok=True)
    name = f"product_{hashlib.md5(data).hexdigest()[:12]}{ext}"
    path = os.path.join(REMOTION_PUBLIC_DIR, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return name


def download_article_images(urls: List[str], limit: int = 6) -> Tuple[List[Dict], List[str]]:
    """
    Tải ảnh minh hoạ của bài báo về thư mục public để ghép vào cảnh.

    Ảnh nào tải hỏng thì bỏ qua VÀ ghi lý do, không im lặng: bài có sáu ảnh mà
    chỉ ghép được hai thì bạn phải biết vì sao, thay vì tưởng bài chỉ có hai.

    Đặt tên theo mã băm nội dung nên dán lại cùng một bài sẽ dùng lại đúng file
    cũ. Phần dọn ảnh thừa do purge_orphan_product_images lo, giữ 30 ảnh mới nhất.
    """
    saved: List[Dict] = []
    notes: List[str] = []

    for index, url in enumerate(urls or []):
        if len(saved) >= limit:
            break
        try:
            res = requests.get(url, timeout=20, stream=True, headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            })
            if res.status_code != 200:
                notes.append(f"ảnh {index + 1}: trang trả về mã {res.status_code}")
                continue

            ctype = (res.headers.get("Content-Type") or "").lower()
            ext = ".jpg"
            if "png" in ctype:
                ext = ".png"
            elif "webp" in ctype:
                ext = ".webp"
            elif "jpeg" not in ctype and "jpg" not in ctype:
                notes.append(f"ảnh {index + 1}: không phải ảnh JPG, PNG hay WEBP")
                continue

            blob = b""
            for chunk in res.iter_content(65536):
                blob += chunk
                if len(blob) > MAX_IMAGE_BYTES:
                    break
            res.close()

            if len(blob) < 8000:
                notes.append(f"ảnh {index + 1}: file quá nhỏ, có thể là ảnh giữ chỗ")
                continue

            saved.append({"file": store_product_image(blob, ext), "source": url})
        except Exception as exc:
            notes.append(f"ảnh {index + 1}: {exc}")

    return saved, notes


def purge_orphan_product_images(keep: Optional[List[str]] = None, max_keep: int = 30) -> int:
    """
    Dọn ảnh sản phẩm cũ trong remotion/public.

    Mỗi file còn lại đều bị Remotion đóng gói vào bundle ở lần render sau, nên
    không dọn thì tool chậm dần. Giữ lại các ảnh mới nhất để bạn render lại
    được kịch bản vừa làm.
    """
    keep_set = set(keep or [])
    if not os.path.isdir(REMOTION_PUBLIC_DIR):
        return 0
    files = []
    for name in os.listdir(REMOTION_PUBLIC_DIR):
        if not name.startswith("product_") or name in keep_set:
            continue
        full = os.path.join(REMOTION_PUBLIC_DIR, name)
        try:
            files.append((os.path.getmtime(full), full))
        except OSError:
            pass
    files.sort(reverse=True)
    removed = 0
    for _, full in files[max_keep:]:
        try:
            os.remove(full)
            removed += 1
        except OSError:
            pass
    return removed


def purge_orphan_voice_files(keep: Optional[List[str]] = None) -> int:
    """
    Dọn các file giọng đọc còn sót trong remotion/public.

    Mỗi file wav còn lại đều bị Remotion đóng gói vào bundle ở lần render sau,
    nên không dọn thì tool chậm dần theo thời gian.
    """
    keep_set = set(keep or [])
    removed = 0
    if not os.path.isdir(REMOTION_PUBLIC_DIR):
        return 0
    for name in os.listdir(REMOTION_PUBLIC_DIR):
        if not name.startswith("voice_") or not name.endswith(".wav"):
            continue
        if name in keep_set:
            continue
        try:
            os.remove(os.path.join(REMOTION_PUBLIC_DIR, name))
            removed += 1
        except OSError:
            pass
    return removed


def _file_dang_dung() -> set:
    """Tên tranh, giọng và ảnh bìa mà các dự án đang giữ. Đọc thẳng từ đĩa."""
    dung = set()
    try:
        import longform_projects as lp
        thu_muc = lp.PROJECTS_DIR
        for ma in os.listdir(thu_muc):
            duong = os.path.join(thu_muc, ma)
            if not os.path.isdir(duong):
                continue
            for ten in os.listdir(duong):
                if not ten.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(duong, ten), encoding="utf-8") as fh:
                        doc = json.load(fh)
                except (OSError, ValueError):
                    continue
                for canh in doc.get("scenes") or []:
                    for khoa in ("imageFile", "audioFile"):
                        if isinstance(canh, dict) and canh.get(khoa):
                            dung.add(canh[khoa])
                bia = doc.get("thumbnail")
                if isinstance(bia, dict) and bia.get("file"):
                    dung.add(bia["file"])
    except (OSError, ImportError):
        pass
    return dung


def don_kho_anh(ngay: int = 7, xem_truoc: bool = False) -> Dict:
    """
    Dọn tranh và giọng không còn dự án nào dùng.

    Kho này chỉ lớn lên: 460 file và 520 MB tính tới 14/09/2026, vì hàm dọn
    giọng viết xong rồi không nơi nào gọi. Hai điều kiện phải đúng cùng lúc mới
    xoá: không dự án nào nhắc tới file đó, và file đã cũ hơn `ngay`. Điều kiện
    thứ hai để chừa lại việc đang làm dở ở những tab không lưu thành dự án.
    """
    moc = time.time() - ngay * 86400
    dung = _file_dang_dung()
    tong, dem, ten = 0, 0, []
    try:
        names = os.listdir(REMOTION_PUBLIC_DIR)
    except OSError:
        return {"freed_bytes": 0, "removed": 0, "files": []}
    for name in names:
        path = os.path.join(REMOTION_PUBLIC_DIR, name)
        try:
            if not os.path.isfile(path) or name in dung or os.path.getmtime(path) >= moc:
                continue
            size = os.path.getsize(path)
        except OSError:
            continue
        if not xem_truoc:
            try:
                os.remove(path)
            except OSError:
                continue
        tong += size
        dem += 1
        ten.append(name)
    return {"freed_bytes": tong, "removed": dem, "files": ten}


def cleanup_voice_files(scenes: Optional[List[Dict]]) -> None:
    """Xoá đúng các file giọng đọc của một lần render sau khi đã xuất xong video."""
    for scene in scenes or []:
        name = scene.get("audioFile") if isinstance(scene, dict) else None
        if not name:
            continue
        path = os.path.join(REMOTION_PUBLIC_DIR, name)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# Dấu kết câu. Thiếu nó thì bộ đọc không hạ giọng ở cuối, câu nghe như bị cắt
# ngang giữa chừng. Đây đúng là lỗi "nói không hết ý" đo được ngày 21/09/2026.
KET_CAU = ".!?…"


def ket_cau(cau: str) -> str:
    """Bảo đảm câu kết thúc bằng một dấu kết, để bộ đọc hạ giọng đúng chỗ."""
    cau = (cau or "").strip().strip(",;:-–— ")
    if not cau:
        return ""
    return cau if cau[-1] in KET_CAU else cau + "."


def scene_voice_text(scene: Dict) -> str:
    """
    Câu sẽ được đọc lên cho một cảnh. Dùng chung cho cả render và nghe thử.

    Cảnh hook chỉ đọc câu móc câu. Dòng chủ đề đi kèm là chữ hiện trên màn hình
    để người lướt qua biết ngay video nói về cái gì, đọc lên nữa là quá ba giây.

    Ngày 21/09/2026: BỎ phần đọc tiêu đề cảnh. Trước đây hàm này trả về
    "tiêu đề. lời thoại", nên giọng đọc ra thành một mẩu cụt rồi mới tới câu
    thật, cảnh nào cũng vậy. Sinh thử một kịch bản và in ra đúng câu được đọc:

        "Sạch quá chưa chắc tốt. Bạn nghĩ vo càng sạch cơm càng ngon, nhưng..."
        "Vo đến nước trong veo. Nhiều người quen vo đến khi nước trong veo,..."

    Mẩu cụt, chấm, rồi câu. Năm cảnh liền nhau như vậy nghe rời rạc hẳn. Chính
    câu lệnh gửi cho AI cũng nói "desc" mới là "lời thoại sẽ được đọc lên thành
    tiếng", tức code đang làm khác điều đã dặn.

    Tiêu đề KHÔNG mất đi: nó vẫn hiện trên màn hình như cũ, chỉ thôi được đọc.
    Phần từ tiết kiệm được trả lại cho lời thoại ở plan_script.
    """
    # Video dài mượn hàm này để đọc lời dẫn, và nó gắn cờ này. Giữ NGUYÊN cách
    # tính cũ cho đường đó: lời dẫn video dài vốn đã là văn xuôi có dấu câu đầy
    # đủ, mà đổi một ký tự là đổi mã băm tên file giọng, tức mọi file giọng đã
    # đọc trong bộ nhớ đệm thành vô dụng và phải đọc lại từ đầu.
    if scene.get("giu_nguyen_cau"):
        return f"{scene.get('title', '')}. {scene.get('desc', '')}".strip(" .")
    if scene.get("kind") == "hook":
        return ket_cau(scene.get("title", ""))
    return ket_cau(scene.get("desc", ""))


def scene_voice_triple(scene: Dict, voice: str, rate: str, pitch: str) -> Tuple[str, str, str]:
    """
    Giọng, tốc độ và cao độ dùng cho MỘT cảnh.

    Timeline Editor cho phép đặt giọng riêng cho từng cảnh, ví dụ cảnh hook đọc
    giọng nam dẫn nhanh còn phần thân đọc giọng nữ thủ thỉ. Cảnh nào không đặt
    riêng thì dùng giọng chung của cả video, y như trước đây.

    Ô chọn giọng gửi lên mã bộ ba (ví dụ "namminh_tram"), nhưng kịch bản cũ có
    thể còn ghi thẳng tên giọng của Microsoft, nên nhận cả hai.
    """
    raw = (scene.get("voice") or "").strip()
    if not raw:
        return voice, rate, pitch

    from tts_engine import resolve_shorts_voice
    resolved = resolve_shorts_voice(raw, scene.get("voiceRate"), scene.get("voicePitch"))
    return resolved["voice"], resolved["rate"], resolved["pitch"]


def locked_frames(scene: Dict) -> int:
    """
    Số frame người dùng tự chốt cho một cảnh trên timeline, 0 nếu để tool tự tính.

    Chỉ khi bạn kéo mép cảnh trên timeline thì cảnh mới mang dấu `lockDuration`.
    Cảnh không có dấu này vẫn co giãn theo độ dài thật của giọng đọc.
    """
    if not scene.get("lockDuration"):
        return 0
    frames = scene.get("durationInFrames")
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        return 0
    return frames if frames > 0 else 0


def _voice_key(text: str, voice: str, rate: str, pitch: str) -> str:
    import hashlib
    raw = f"{text}|{voice}|{rate}|{pitch}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


# Khoảng lặng sau tiếng cuối của CẢNH CUỐI CÙNG.
#
# Mọi cảnh vốn chỉ được chừa 8 frame, tức 0,27 giây. Với cảnh giữa thì không
# sao vì nó cắt ngay sang cảnh sau. Nhưng cảnh cuối thì video dừng phựt đúng
# 0,27 giây sau chữ cuối, nghe như bị cắt ngang chứ không phải kết thúc. Đây là
# chỗ người dùng gọi là "cảnh cuối end rất kỳ" ngày 21/09/2026.
#
# 24 frame là 0,8 giây, đủ để câu cuối buông hết rồi mới hết video.
DUOI_CANH_CUOI_FRAMES = 24


def generate_voice_for_scenes(
    scenes: list,
    voice: str = "vi-VN-HoaiMyNeural",
    rate: str = "+5%",
    pitch: str = "+0Hz",
    duoi_canh_cuoi: int = 8,
    so_lan_thu: int = 3,
    on_fallback_gtts: Optional[Callable] = None,
) -> list:
    """
    Tạo giọng đọc riêng cho từng cảnh và đặt lại thời lượng cảnh theo độ dài thật.

    Tên file được đặt theo mã băm của (lời thoại + giọng + tốc độ + cao độ), nên
    khi bạn chạy thử rồi render thật với cùng kịch bản, phần tạo giọng được bỏ
    qua hoàn toàn. Cuối mỗi lần chạy, mọi file giọng không còn dùng tới đều bị
    dọn, nên thư mục public không phình lên theo thời gian.

    Có thử lại nhiều lần và có giọng dự phòng gTTS khi Edge-TTS chặn.
    """
    import edge_tts

    os.makedirs(REMOTION_PUBLIC_DIR, exist_ok=True)

    ffmpeg = _ffmpeg_bin()
    generated: List[str] = []
    failures: List[str] = []

    # Lên danh sách file cần có, và xem file nào đã sẵn sàng từ lần chạy trước.
    plan = []
    for index, scene in enumerate(scenes):
        text = scene_voice_text(scene)
        cue_voice, cue_rate, cue_pitch = scene_voice_triple(scene, voice, rate, pitch)
        filename = f"voice_{_voice_key(text, cue_voice, cue_rate, cue_pitch)}.wav"
        path = os.path.join(REMOTION_PUBLIC_DIR, filename)
        cached = os.path.exists(path) and os.path.getsize(path) > 1000
        plan.append({"index": index, "text": text, "filename": filename,
                     "path": path, "cached": cached,
                     "voice": cue_voice, "rate": cue_rate, "pitch": cue_pitch})

    # Gộp các cảnh có lời thoại giống hệt nhau: chúng cùng một mã băm nên chỉ
    # cần đọc một lần rồi dùng chung file.
    todo, seen = [], set()
    for item in plan:
        if item["cached"] or not item["text"] or item["filename"] in seen:
            continue
        seen.add(item["filename"])
        todo.append(item)
    reused = len(plan) - len(todo)
    if reused:
        print(f"[⚡ TTS] Bỏ qua {reused}/{len(plan)} cảnh nhờ file giọng đọc đã có sẵn.")

    try:
        with tempfile.TemporaryDirectory(prefix="remotion-tts-") as tmp:

            async def synthesize_all():
                semaphore = asyncio.Semaphore(2)

                async def one(item: Dict) -> Tuple[int, bool]:
                    index, text = item["index"], item["text"]
                    cue_voice, cue_rate, cue_pitch = item["voice"], item["rate"], item["pitch"]
                    mp3 = os.path.join(tmp, f"{index}.mp3")
                    # Đọc đúng số và đơn vị cho giọng tiếng Việt; giọng khác giữ nguyên.
                    from text_normalizer import normalize_for_speech
                    spoken = normalize_for_speech(text, lang=(cue_voice or "")[:2])

                    async def speak() -> list:
                        """
                        Đọc và giữ luôn mốc từng từ.

                        `.save()` vứt mất mốc này. Đọc qua `.stream()` thì Edge
                        TTS trả kèm sự kiện WordBoundary, nhờ đó chữ karaoke
                        chạy theo giọng thật thay vì theo ước lượng độ dài từ.
                        """
                        marks = []
                        with open(mp3, "wb") as fh:
                            # boundary mặc định là theo CÂU. Phải bật sang theo
                            # TỪ thì mới có mốc để chữ karaoke bám giọng.
                            try:
                                comm = edge_tts.Communicate(
                                    text=spoken, voice=cue_voice, rate=cue_rate,
                                    pitch=cue_pitch, boundary="WordBoundary")
                            except TypeError:
                                # Bản edge-tts cũ chưa có tham số này.
                                comm = edge_tts.Communicate(text=spoken, voice=cue_voice,
                                                            rate=cue_rate, pitch=cue_pitch)
                            async for part in comm.stream():
                                if part["type"] == "audio":
                                    fh.write(part["data"])
                                elif part["type"] == "WordBoundary":
                                    # Edge TTS đếm bằng đơn vị 100 nano giây.
                                    marks.append({"t": part["offset"] / 10_000_000,
                                                  "w": part.get("text", "")})
                        return marks

                    async with semaphore:
                        last = ""
                        for attempt in range(so_lan_thu):
                            try:
                                marks = await asyncio.wait_for(speak(), timeout=90)
                                if os.path.exists(mp3) and os.path.getsize(mp3) > 200:
                                    item["marks"] = marks
                                    return index, True
                                last = "Edge-TTS trả về file rỗng"
                            except Exception as exc:
                                last = str(exc)
                            await asyncio.sleep(0.8 * (attempt + 1))

                        # Dự phòng: gTTS. Giọng máy hơn nhưng còn hơn mất cả video.
                        try:
                            from gtts import gTTS
                            lang = cue_voice.split("-")[0].lower() if "-" in cue_voice else "vi"
                            gTTS(text=spoken, lang=lang).save(mp3)
                            if os.path.exists(mp3) and os.path.getsize(mp3) > 200:
                                msg = f"[⚠️ TTS CẢNH BÁO] Cảnh {index + 1} phải dùng giọng dự phòng gTTS ({last})"
                                print(msg)
                                if on_fallback_gtts:
                                    try:
                                        on_fallback_gtts(index, spoken, last)
                                    except Exception:
                                        pass
                                return index, True
                        except Exception as exc:
                            last = f"{last} | gTTS: {exc}"

                        failures.append(f"cảnh {index + 1}: {last}")
                        return index, False

                return await asyncio.gather(*[one(item) for item in todo])

            results = dict(asyncio.run(synthesize_all())) if todo else {}

            if failures:
                raise RuntimeError("; ".join(failures))

            # Chuyển mỗi file mp3 vừa tạo sang wav đã cắt khoảng lặng, mỗi mã
            # băm đúng một lần.
            for item in todo:
                index = item["index"]
                if not results.get(index):
                    raise RuntimeError(f"cảnh {index + 1}: không tạo được giọng đọc")
                generated.append(item["path"])
                lead = _mp3_to_trimmed_wav(ffmpeg, os.path.join(tmp, f"{index}.mp3"),
                                           item["path"], tmp)
                # Dời mốc từng từ về gốc thời gian của file đã xén đầu.
                # Kẹp về 0 chứ KHÔNG bỏ mốc âm. Edge TTS đánh mốc từ đầu tiên
                # sớm hơn lúc thật sự phát ra tiếng, nên sau khi trừ phần lặng
                # đã cắt nó thành số âm nhỏ. Bỏ đi là lệch số từ, mà lệch số từ
                # thì cả cảnh mất mốc thật và phải quay về ước lượng.
                marks = [{"t": round(max(0.0, m["t"] - lead), 3), "w": m["w"]}
                         for m in (item.get("marks") or [])]
                if marks:
                    with open(marks_path(item["path"]), "w", encoding="utf-8") as fh:
                        json.dump({"lead": round(lead, 3), "marks": marks}, fh)

            timed_scenes = []
            too_short = []
            for item in plan:
                index, scene = item["index"], scenes[item["index"]]
                if not os.path.exists(item["path"]):
                    raise RuntimeError(f"cảnh {index + 1}: không tạo được giọng đọc")

                with wave.open(item["path"], "rb") as audio:
                    seconds = audio.getnframes() / audio.getframerate()
                if seconds <= 0:
                    raise ValueError(f"cảnh {index + 1}: giọng đọc rỗng sau khi cắt khoảng lặng")

                # Mốc người dùng tự kéo trên timeline được giữ nguyên. Nhưng nếu
                # mốc đó ngắn hơn câu đọc thì dừng lại và nói rõ, tuyệt đối không
                # cắt cụt lời rồi vẫn báo render thành công.
                needed = math.ceil(seconds * FPS) + 2
                chosen = locked_frames(scene)
                if chosen and chosen < needed:
                    too_short.append(
                        f"cảnh {index + 1} bạn đặt {chosen / FPS:.1f} giây nhưng giọng đọc mất "
                        f"{seconds:.1f} giây (cần ít nhất {needed / FPS:.1f} giây)")
                    continue

                # Mốc từng từ đo từ chính giọng vừa đọc. Chỉ dùng khi số mốc
                # khớp số từ hiện lên màn hình; chuẩn hoá số có thể tách "1990"
                # thành mấy từ đọc, lúc đó lệch nên quay về cách ước lượng cũ.
                shown = (scene_voice_text(scene) or "").split()
                word_frames: List[float] = []
                mpath = marks_path(item["path"])
                if os.path.exists(mpath):
                    try:
                        with open(mpath, encoding="utf-8") as fh:
                            marks = (json.load(fh) or {}).get("marks") or []
                        if len(marks) == len(shown) and shown:
                            word_frames = [round(max(0.0, m["t"]) * FPS, 2) for m in marks]
                    except (OSError, ValueError, KeyError):
                        word_frames = []

                timed_scenes.append({
                    **scene,
                    "wordFrames": word_frames,
                    "audioFile": item["filename"],
                    # Số frame câu đọc thật chiếm: React rải chữ sáng dần đúng
                    # trong khoảng này, kể cả khi cảnh bị kéo dài trên timeline.
                    "voiceFrames": math.ceil(seconds * FPS),
                    # Cảnh cuối được chừa thêm khoảng lặng để câu cuối buông
                    # hết. Người dùng kéo tay trên timeline thì tôn trọng con
                    # số đó, không tự cộng thêm.
                    "durationInFrames": chosen or (
                        math.ceil(seconds * FPS)
                        + (duoi_canh_cuoi if index == len(scenes) - 1 else 8)),
                })

            if too_short:
                raise SceneTooShortError(
                    "Thời lượng bạn kéo trên timeline ngắn hơn lời đọc: "
                    + "; ".join(too_short)
                    + ". Hãy kéo cảnh dài ra hoặc rút gọn lời thoại rồi render lại.")

        # Giữ lại đúng file của lần chạy này để lần render sau dùng lại được,
        # xoá mọi file giọng đọc thừa từ những kịch bản cũ.
        purge_orphan_voice_files(keep=[p["filename"] for p in plan])
        _purge_orphan_marks(keep=[p["path"] for p in plan])
        return timed_scenes

    except SceneTooShortError:
        raise
    except Exception as exc:
        for path in generated:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        raise RuntimeError(f"Không tạo được giọng đọc: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# RENDER
# ─────────────────────────────────────────────────────────────────────────────

def _npx_bin() -> str:
    """
    Tìm npx bằng đường dẫn tuyệt đối.

    Khi Flask được khởi động ngoài shell đăng nhập, PATH có thể không có Node và
    lỗi trả về sẽ là "No such file or directory: 'npx'" rất khó hiểu.
    """
    found = shutil.which("npx")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/npx", "/usr/local/bin/npx", "/usr/bin/npx"):
        if os.path.isfile(candidate):
            return candidate
    return "npx"


DRAFT_SCALE = 0.4

# Remotion in tiến trình ra từng dòng khi không chạy trong terminal thật.
# Đo ngày 14/09/2026 với bản 4.0.522: "Bundling 47%" lúc đóng gói, rồi
# "Rendered 12/31, time remaining: 1s" cho từng khung hình, cuối cùng là
# "Encoded 31/31". Ba mẫu này đủ để dựng một thanh tiến trình thật.
_MAU_BUNDLING = re.compile(r"Bundling\s+(\d+)%")
_MAU_KHUNG = re.compile(r"Rendered\s+(\d+)/(\d+)")
_MAU_ENCODE = re.compile(r"Encoded\s+(\d+)/(\d+)")


def doc_tien_trinh_remotion(dong: str):
    """
    Bóc tiến trình từ một dòng Remotion in ra.

    Trả về (phần trăm, câu mô tả) hoặc None nếu dòng đó không nói gì về tiến
    trình. Đóng gói chiếm 15 phần trăm đầu vì nó nhanh và chỉ chạy lần đầu,
    dựng khung hình chiếm phần lớn, ghép video chiếm 10 phần cuối.
    """
    got = _MAU_ENCODE.search(dong)
    if got:
        xong, tong = int(got.group(1)), max(1, int(got.group(2)))
        return 90 + int(10 * xong / tong), f"Đang ghép video, {xong}/{tong} khung hình"
    got = _MAU_KHUNG.search(dong)
    if got:
        xong, tong = int(got.group(1)), max(1, int(got.group(2)))
        con = ""
        thoi = re.search(r"time remaining:\s*([\dhms ]+)", dong)
        if thoi:
            con = f", còn khoảng {thoi.group(1).strip()}"
        return 15 + int(75 * xong / tong), f"Đang dựng hình, {xong}/{tong} khung{con}"
    got = _MAU_BUNDLING.search(dong)
    if got:
        return int(15 * int(got.group(1)) / 100), f"Đang đóng gói giao diện, {got.group(1)}%"
    if "Getting composition" in dong:
        return 15, "Đang đọc kịch bản"
    return None


# Dòng nói nguyên nhân nằm ở ĐẦU khối lỗi của Remotion, còn phần đuôi chỉ là
# stack trace trong node_modules. Bản cũ lấy err[-400:], tức cắt đúng phần cần
# và giữ đúng phần vô dụng. Ngày 22/09/2026 người dùng dán lại một lỗi và nó
# bắt đầu bằng "/qui.nt/Documents/.../read-file.js:59:15) at process..." —
# không có cách nào biết file nào đọc không được.
_DAU_HIEU_LOI = ("ENOENT", "no such file", "not found", "Cannot find",
                 "Cannot read", "Failed to", "Error:", "TypeError", "SyntaxError",
                 "is not defined", "Unexpected")


def _cot_loi(dong_ra: List[str], toi_da: int = 420) -> str:
    """
    Rút câu nói nguyên nhân từ khối lỗi của Remotion.

    Lấy dòng đầu tiên có dấu hiệu lỗi, kèm tên file asset nếu tìm được, vì lỗi
    hay gặp nhất là Remotion đọc một tranh hoặc một file giọng không có trên
    đĩa. Không tìm được dòng nào thì mới quay về mấy dòng cuối.
    """
    sach = [d.strip() for d in dong_ra if d.strip()
            and "node_modules" not in d and not d.strip().startswith("at ")]
    nguyen_nhan = next((d for d in sach
                        if any(x in d for x in _DAU_HIEU_LOI)), "")
    # Tên file asset, tìm trên CẢ khối kể cả dòng trong node_modules: đây là thứ
    # người dùng cần để biết phải vẽ lại hay tạo lại giọng cho cảnh nào.
    ca = "\n".join(dong_ra)
    asset = ""
    for m in re.finditer(r"[\w./-]*?((?:product_lv|thumb_|voice_|music_)[\w.-]+)", ca):
        asset = m.group(1)
        break
    phan = [p for p in (nguyen_nhan, f"file: {asset}" if asset else "") if p]
    if not phan:
        phan = sach[-3:] or [d.strip() for d in dong_ra[-3:]]
    return " | ".join(phan)[:toi_da]


def _chay_va_doc(cmd, timeout, on_progress=None):
    """
    Chạy lệnh và đọc dòng ra ngay khi nó xuất hiện.

    Trả về (mã thoát, danh sách dòng). Dòng nào nói về tiến trình thì gọi
    `on_progress(phần trăm, câu mô tả)`. Hàm gọi lại hỏng cũng không được làm
    hỏng lượt dựng, nên mọi lỗi của nó bị nuốt.
    """
    dong_ra = []
    env = os.environ.copy()
    env["PATH"] = f"/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"
    proc = subprocess.Popen(cmd, cwd=REMOTION_DIR, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
    han = time.time() + timeout
    try:
        for dong in proc.stdout:
            dong = dong.rstrip()
            if dong:
                dong_ra.append(dong)
            if on_progress:
                got = doc_tien_trinh_remotion(dong)
                if got:
                    try:
                        on_progress(got[0], got[1])
                    except Exception:  # noqa: BLE001
                        pass
            if time.time() > han:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
    return proc.wait(), dong_ra


def render_remotion_video(
    composition_id: str = "MevabeShorts",
    output_filename: str = "mevabe_remotion_demo.mp4",
    props: dict = None,
    timeout: int = 900,
    draft: bool = False,
    on_progress=None,
) -> dict:
    """
    Render composition React thành MP4 trong thư mục downloads/.

    draft=True xuất bản nháp thu nhỏ còn 40% (432x768) với chất lượng thấp hơn,
    dùng để xem thử chuyển cảnh và nghe tiếng trước khi render bản đầy đủ.
    Mọi thứ khác giữ nguyên nên bản nháp phản ánh đúng thành phẩm.
    """
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    try:
        validate_output_filename(output_filename)
    except ValueError as exc:
        return {"success": False, "output_path": "", "message": str(exc)}

    if not os.path.isdir(os.path.join(REMOTION_DIR, "node_modules")):
        return {"success": False, "output_path": "",
                "message": "Chưa cài thư viện Remotion. Mở Terminal, vào thư mục remotion và chạy: npm install"}

    output_path = os.path.join(DOWNLOADS_DIR, output_filename)
    props_file = None
    # --log=info để Remotion in tiến trình từng dòng. Mức warn cũ im lặng tuyệt
    # đối, nên suốt lúc dựng không có gì để hiện ngoài một cái đồng hồ.
    cmd = [_npx_bin(), "remotion", "render", composition_id, output_path, "--log=info"]
    if draft:
        cmd += [f"--scale={DRAFT_SCALE}", "--crf=30", "--jpeg-quality=70"]

    try:
        if props:
            # Truyền props qua file thay vì nhét JSON vào dòng lệnh: an toàn với
            # emoji, tiếng Việt và kịch bản dài.
            handle, props_file = tempfile.mkstemp(prefix="remotion-props-", suffix=".json")
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(props, f, ensure_ascii=False)
            cmd.append(f"--props={props_file}")

        print(f"[🎬 Remotion] Đang render {'BẢN NHÁP' if draft else 'bản đầy đủ'} '{composition_id}'...")
        # Đọc từng dòng thay vì chờ chạy xong rồi mới đọc một thể: chỉ có đọc
        # dọc đường mới biết đang ở khung hình nào trong tổng số bao nhiêu.
        ma, dong_ra = _chay_va_doc(cmd, timeout, on_progress)

        if ma == 0 and os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f"[✅ Remotion] Xuất video thành công: {output_path} ({size_mb:.1f} MB)")
            return {"success": True, "output_path": output_path,
                    "message": f"Render video thành công ({size_mb:.1f} MB)"}

        err = "\n".join(dong_ra[-40:]).strip()
        cot = _cot_loi(dong_ra)
        print(f"[❌ Remotion Error] {err}")
        return {"success": False, "output_path": "",
                "message": f"Remotion render lỗi: {cot or 'không rõ nguyên nhân'}"}

    except subprocess.TimeoutExpired:
        return {"success": False, "output_path": "",
                "message": f"Remotion render quá thời gian chờ ({timeout}s)"}
    except FileNotFoundError:
        return {"success": False, "output_path": "",
                "message": "Không tìm thấy lệnh npx. Hãy cài Node.js hoặc khởi động app từ Terminal."}
    except Exception as exc:
        return {"success": False, "output_path": "", "message": f"Lỗi thực thi Remotion: {exc}"}
    finally:
        if props_file and os.path.exists(props_file):
            try:
                os.remove(props_file)
            except OSError:
                pass


CTA_FIELDS = ("badgeText", "footerText", "footerIcon", "headerIcon")


def build_render_props(scenes: List[Dict], channel_name: str, sub_title: str,
                       template_id: str = DEFAULT_TEMPLATE_ID,
                       cta: Optional[Dict] = None,
                       show_footer: bool = True, show_badge: bool = True,
                       sfx: bool = True, format_id: str = "") -> Dict:
    """
    Gói props gửi sang React: cảnh, bộ token của template và lời kêu gọi hành động.

    Lời kêu gọi hành động (chữ trên huy hiệu góc phải và dải chữ dưới đáy) trước
    đây nằm chết trong template, nên đổi template là đổi luôn câu kêu gọi. Giờ
    nó là dữ liệu truyền từ bên ngoài: template lo phần nhìn, bạn lo phần chữ.
    """
    from remotion_templates import _find_raw
    from video_formats import get_format, theme_for_format

    fmt = get_format(format_id)
    raw = _find_raw(template_id) or {}
    # Chọn bản HTML đúng khổ. Khung 9:16 chưa có bản 1:1 thì ValueError, nói rõ.
    theme = theme_for_format(resolve_tokens(template_id), fmt, raw.get("name", ""))
    for key, value in (cta or {}).items():
        if key in CTA_FIELDS and isinstance(value, str) and value.strip():
            theme[key] = value.strip()[:120]
    if not show_footer:
        theme["footerText"] = ""
    if not show_badge:
        theme["badgeText"] = ""

    return {
        "scenes": scenes,
        "channelName": (channel_name or "KÊNH CỦA BẠN")[:60],
        "subTitle": (sub_title or "")[:80],
        "templateStyle": template_id,
        "theme": theme,
        # Tiếng gió vút ở mỗi cú chuyển cảnh và một tiếng trầm lúc mở hook.
        "sfx": bool(sfx),
        # Khổ video và phần đệm tránh vùng giao diện của nền tảng.
        "format": fmt["id"],
        "canvas": {"width": fmt["width"], "height": fmt["height"]},
        "safe": fmt["safe"],
    }


if __name__ == "__main__":
    print("Testing Remotion video rendering...")
    print(json.dumps(render_remotion_video(
        props=build_render_props(DEFAULT_SCENES, "KÊNH CỦA BẠN", "")
    ), ensure_ascii=False))
