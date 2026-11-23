"""
video_formats.py
────────────────
Các khổ video mà Global Creator xuất ra, và vùng giao diện của từng nền tảng
đè lên video.

Reel và Shorts cùng khổ 1080x1920 nhưng khác chỗ bị che: Shorts che phần dưới
nhiều hơn (tên kênh, nút Đăng ký, tiêu đề), Reel che cột nút bên phải và phần
chú thích. Facebook Post 1:1 là bài đăng thường trong bảng tin, không có lớp
giao diện nào đè lên nhưng chỉ đăng được dạng Video, vì API Reel của Facebook
chỉ nhận video dọc.

Toạ độ vùng bị che là ước lượng theo giao diện ứng dụng năm 2026 trên màn hình
1080x1920, có thể lệch vài chục pixel giữa các máy. Mục đích là giữ chữ quan
trọng tránh xa những vùng đó, không phải đo tới từng pixel.
"""

from __future__ import annotations

import re
import subprocess
from copy import deepcopy
from typing import Dict, List, Optional, Tuple

DEFAULT_FORMAT_ID = "fb_reel"

VIDEO_FORMATS: Dict[str, Dict] = {
    "fb_reel": {
        "id": "fb_reel",
        "label": "Facebook Reel",
        "short": "Reel",
        "ratio": "9:16",
        "width": 1080,
        "height": 1920,
        # Phần đệm an toàn: chữ quan trọng nằm trong khoảng này là không bị che.
        "safe": {"top": 180, "bottom": 420, "right": 150},
        "zones": [
            {"name": "thanh trên cùng của Reels", "x": 0, "y": 0, "w": 1080, "h": 180},
            {"name": "tên Page, chú thích và nhạc nền", "x": 0, "y": 1500, "w": 1080, "h": 420},
            {"name": "cột nút thích, bình luận, chia sẻ", "x": 930, "y": 1000, "w": 150, "h": 500},
        ],
        "post_types": ["reel", "video", "both"],
    },
    "yt_shorts": {
        "id": "yt_shorts",
        "label": "YouTube Shorts",
        "short": "Shorts",
        "ratio": "9:16",
        "width": 1080,
        "height": 1920,
        "safe": {"top": 150, "bottom": 520, "right": 150},
        "zones": [
            {"name": "thanh tìm kiếm của Shorts", "x": 0, "y": 0, "w": 1080, "h": 150},
            {"name": "tên kênh, nút Đăng ký và tiêu đề", "x": 0, "y": 1400, "w": 1080, "h": 520},
            {"name": "cột nút thích, bình luận, chia sẻ, remix", "x": 930, "y": 780, "w": 150, "h": 620},
        ],
        "post_types": ["reel", "video", "both"],
    },
    "fb_post": {
        "id": "fb_post",
        "label": "Facebook Post 1:1",
        "short": "Post 1:1",
        "ratio": "1:1",
        "width": 1080,
        "height": 1080,
        "safe": {"top": 0, "bottom": 0, "right": 0},
        "zones": [],
        # Reel của Facebook bắt buộc video dọc: video vuông chỉ đăng dạng Video.
        "post_types": ["video"],
    },
}


def get_format(format_id: Optional[str]) -> Dict:
    """Khổ theo mã; mã lạ hoặc để trống thì dùng Facebook Reel như trước đây."""
    return deepcopy(VIDEO_FORMATS.get((format_id or "").strip(), VIDEO_FORMATS[DEFAULT_FORMAT_ID]))


def list_formats() -> List[Dict]:
    return [deepcopy(fmt) for fmt in VIDEO_FORMATS.values()]


def is_square(fmt: Dict) -> bool:
    return fmt["height"] / fmt["width"] < 1.3


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE THEO KHỔ
# ─────────────────────────────────────────────────────────────────────────────

_CONTENT_SLOT = re.compile(r"\{\{\s*content\s*\}\}")
_FULL_LAYOUT = re.compile(r"1840px|1920px|height\s*:\s*100%|fact-reel|full-screen", re.I)


def template_kind(tokens: Dict) -> str:
    """
    Cùng quy tắc với templateKind trong remotion/src/MevabeShorts.tsx.

    builtin: bố cục dựng sẵn, tự co giãn theo mọi khổ.
    frame: khung HTML có {{content}}.
    full: trang HTML cũ tự có đầu và chân.
    card: chỉ thẻ nội dung, tool lo nền, đầu, chân nên cũng co giãn được.
    """
    html = (tokens or {}).get("html") or ""
    if not html.strip():
        return "builtin"
    if _CONTENT_SLOT.search(html):
        return "frame"
    if re.search(r"<header\b|<footer\b", html, re.I) or _FULL_LAYOUT.search(html + (tokens.get("css") or "")):
        return "full"
    return "card"


def theme_for_format(theme: Dict, fmt: Dict, template_name: str = "") -> Dict:
    """
    Chọn đúng bản HTML của template cho khổ sắp render.

    Khung HTML dựng theo kích thước cố định: khung 9:16 đặt vào video vuông sẽ
    bị cắt mất nửa dưới. Template loại khung hoặc trang mà chưa có bản 1:1 thì
    dừng lại và nói rõ, không render ra video hỏng.
    """
    theme = dict(theme)
    square_html = theme.pop("htmlSquare", "") or ""
    square_css = theme.pop("cssSquare", "") or ""
    if not is_square(fmt):
        return theme
    if square_html.strip():
        theme["html"] = square_html
        theme["css"] = square_css
        return theme
    if template_kind(theme) in ("frame", "full"):
        name = f"“{template_name}” " if template_name else ""
        raise ValueError(
            f"Template {name}dựng bằng HTML cho khổ dọc 9:16, chưa có bản 1:1. "
            "Mở template, chọn kênh Facebook Post 1:1 rồi bấm AI sửa cho khớp để tạo bản 1:1, "
            "hoặc chọn một template dựng sẵn.")
    return theme


# ─────────────────────────────────────────────────────────────────────────────
# KIỂM KHỔ VIDEO TRƯỚC KHI ĐĂNG
# ─────────────────────────────────────────────────────────────────────────────

def probe_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Rộng và cao của video, đọc bằng FFmpeg có sẵn trong tool (không cần ffprobe)."""
    from remotion_generator import _ffmpeg_bin
    try:
        res = subprocess.run([_ffmpeg_bin(), "-hide_banner", "-i", path],
                             capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", res.stderr or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def reel_block_reason(path: str, post_type: str) -> str:
    """
    Lý do không đăng được dạng Reel, rỗng nếu đăng được.

    Facebook từ chối Reel không phải video dọc. Chặn trước khi tải lên để khỏi
    chờ vài phút rồi mới nhận lỗi, và để chế độ "Cả 2" không đăng dở dang.
    """
    if (post_type or "").lower() not in ("reel", "both"):
        return ""
    dims = probe_dimensions(path)
    if not dims:
        return ""
    width, height = dims
    if height / max(1, width) >= 1.7:
        return ""
    return (f"Video này khổ {width}x{height}, không phải video dọc 9:16 nên Facebook không nhận dạng Reel. "
            "Hãy đăng dạng Video.")
