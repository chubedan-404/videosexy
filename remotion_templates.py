"""
remotion_templates.py
─────────────────────
Kho template cho AI Shorts Studio.

Mỗi template là một bộ "token" mô tả toàn bộ diện mạo video (màu nền, thẻ, chữ,
footer, thanh tiến trình, kiểu hoạt hoạ). Component React `MevabeShorts` nhận
trọn bộ token này qua prop `theme`, nên thêm một template mới KHÔNG cần sửa code
React: chỉ cần thêm dữ liệu.

- Template dựng sẵn: khai báo trong BUILTIN_TEMPLATES, không cho sửa/xoá.
- Template người dùng tự tạo: lưu trong config.json dưới khoá "remotion_templates".
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Dict, List, Optional, Tuple

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# Giá trị nền cho mọi token. Template chỉ cần khai báo phần khác biệt.
BASE_TOKENS: Dict = {
    "fontFamily": "'Be Vietnam Pro', 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif",
    "bg": "#0f172a",
    "bgOverlay": "",
    "decor": "none",

    "headerBg": "rgba(255,255,255,0.08)",
    "headerBorder": "1px solid rgba(255,255,255,0.15)",
    "headerRadius": 24,
    "headerIcon": "🎬",
    "headerTitleColor": "#ffffff",
    "headerSubColor": "#cbd5e1",
    "headerUppercase": False,
    "badgeBg": "linear-gradient(135deg, #f43f5e, #e11d48)",
    "badgeColor": "#ffffff",
    "badgeText": "Follow Ngay",
    "badgeRadius": 999,

    "cardBg": "rgba(30,41,59,0.75)",
    "cardBorder": "2px solid rgba(255,255,255,0.15)",
    "cardRadius": 36,
    "cardBlur": 30,
    "cardShadow": "0 25px 50px -12px rgba(0,0,0,0.6)",
    "cardAlign": "center",
    "cardPadding": "56px 48px",

    "iconSize": 104,
    "iconWobble": 6,
    "tagBg": "",
    "tagColor": "#ffffff",
    "tagRadius": 100,
    "tagSize": 24,
    "tagUppercase": False,
    "titleColor": "#ffffff",
    "titleSize": 62,
    "descColor": "#e2e8f0",
    "descSize": 40,
    "highlightColor": "#facc15",
    "highlightEffect": "glow",
    "divider": "",

    "footerBg": "rgba(15,23,42,0.8)",
    "footerBorder": "1px solid rgba(255,255,255,0.1)",
    "footerColor": "#94a3b8",
    "footerIcon": "💬",
    "footerText": "Lưu lại để xem sau nhé!",
    "footerRadius": 20,
    "footerUppercase": False,

    "progressTrack": "rgba(255,255,255,0.15)",
    "progressFill": "linear-gradient(90deg, #f43f5e, #a855f7, #06b6d4)",
    "progressHeight": 10,

    "anim": "spring_pop",
    # Lời thoại sáng dần theo giọng đọc (karaoke), hiện dần từng từ (appear),
    # hay hiện cả câu một lần (none).
    "wordReveal": "karaoke",
    # Hạt nhiễu phim và viền tối phủ trên cùng, tính theo phần trăm.
    "grain": 5,
    "vignette": 28,

    # Ba giây đầu: câu hook chữ to và dòng chủ đề. Không dùng thẻ như cảnh
    # thường, vì lúc này người xem chưa quyết định ở lại hay lướt tiếp.
    "hookTitleSize": 90,
    "hookTitleColor": "#ffffff",
    "hookTopicBg": "linear-gradient(135deg, #f43f5e, #a855f7)",
    "hookTopicColor": "#ffffff",
    "hookIconSize": 150,

    # Ảnh sản phẩm gắn kèm cảnh (luồng tạo video từ ảnh).
    "imageRadius": 28,
    "imageHeight": 420,
    "imageBorder": "",

    # Template tự viết bằng HTML. Để trống thì dùng bố cục dựng sẵn của tool.
    "html": "",
    "css": "",
    # Bản HTML riêng cho video vuông 1:1 (Facebook Post). Khung 9:16 đặt vào
    # video vuông bị cắt mất nửa dưới, nên khung HTML cần bản riêng.
    "htmlSquare": "",
    "cssSquare": "",
}

# Bảng màu gradient dùng cho thẻ tag của từng cảnh khi AI không tự chọn.
SCENE_GRADIENTS: List[str] = [
    "linear-gradient(135deg, #f43f5e 0%, #fb7185 50%, #fda4af 100%)",
    "linear-gradient(135deg, #ec4899 0%, #d946ef 50%, #a855f7 100%)",
    "linear-gradient(135deg, #06b6d4 0%, #3b82f6 50%, #6366f1 100%)",
    "linear-gradient(135deg, #f59e0b 0%, #fbbf24 50%, #fde68a 100%)",
    "linear-gradient(135deg, #10b981 0%, #34d399 50%, #6ee7b7 100%)",
]

ENUM_TOKENS = {
    "decor": {"bubbles", "blobs", "scanlines", "none"},
    "cardAlign": {"center", "left"},
    "highlightEffect": {"glow", "underline", "plain"},
    "anim": {"spring_pop", "slide_up", "slide_left", "zoom_in"},
    "wordReveal": {"karaoke", "appear", "none"},
}

NUMERIC_TOKENS = {
    "headerRadius": (0, 200),
    "badgeRadius": (0, 999),
    "cardRadius": (0, 200),
    "cardBlur": (0, 80),
    "iconSize": (30, 220),
    "iconWobble": (0, 30),
    "tagRadius": (0, 200),
    "tagSize": (10, 60),
    "titleSize": (20, 110),
    "descSize": (14, 80),
    "footerRadius": (0, 200),
    "progressHeight": (0, 40),
    "hookTitleSize": (30, 150),
    "hookIconSize": (0, 260),
    "imageRadius": (0, 200),
    "imageHeight": (0, 1000),
    "grain": (0, 20),
    "vignette": (0, 60),
}

# Khung HTML mẫu. Khung là cả trang 1080x1920: nền, tên kênh, heading, footer
# do bạn dựng; {{content}} là chỗ tool đặt chữ AI viết, có chuyển động theo
# giọng đọc. Khung còn đọc được biến --rm-t (0 đến 1 theo cả video) để tự
# chuyển động bằng CSS, ở đây là nền phóng và xoay rất chậm.
FRAME_SAMPLE_HTML = """<div class="fr">
  <div class="fr-bg"></div>
  <header class="fr-head">
    <div class="fr-brand"><span class="fr-dot"></span>{{channelName}}</div>
    <div class="fr-count">{{sceneIndex}} / {{sceneCount}}</div>
  </header>
  <h1 class="fr-heading">{{subTitle}}</h1>
  <main class="fr-stage">{{content}}</main>
  <footer class="fr-foot">
    <span class="fr-cta">{{cta}}</span>
    <span class="fr-badge">{{badge}}</span>
  </footer>
  <div class="fr-progress">{{progress}}</div>
</div>"""

FRAME_SAMPLE_CSS = """.fr{position:relative;width:1080px;height:1920px;overflow:hidden;box-sizing:border-box;display:flex;flex-direction:column;padding:calc(var(--rm-safe-top,130px) + 24px) 80px calc(var(--rm-safe-bottom,170px) + 24px);background:#0c0a09;color:#f5efe6;font-family:'Be Vietnam Pro',sans-serif}
.fr-bg{position:absolute;inset:-12%;background:radial-gradient(60% 40% at 18% 14%,rgba(234,88,12,.30),transparent 70%),radial-gradient(55% 45% at 86% 86%,rgba(217,119,6,.22),transparent 70%);transform:scale(calc(1 + var(--rm-t,0) * .12)) rotate(calc(var(--rm-t,0) * 6deg))}
.fr-bg::after{content:'';position:absolute;inset:0;background-image:linear-gradient(rgba(245,239,230,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(245,239,230,.05) 1px,transparent 1px);background-size:90px 90px;-webkit-mask-image:radial-gradient(ellipse at center,#000 25%,transparent 72%);mask-image:radial-gradient(ellipse at center,#000 25%,transparent 72%)}
.fr-head{position:relative;display:flex;justify-content:space-between;align-items:center;padding-bottom:28px;border-bottom:1px solid rgba(245,239,230,.16)}
.fr-brand{display:flex;align-items:center;gap:16px;font-size:30px;font-weight:800;letter-spacing:.03em;text-transform:uppercase}
.fr-dot{width:16px;height:16px;border-radius:50%;background:#f97316;box-shadow:0 0 0 7px rgba(249,115,22,.18)}
.fr-count{font-size:26px;font-weight:700;color:rgba(245,239,230,.55);font-variant-numeric:tabular-nums}
.fr-heading{position:relative;margin:36px 0 0;font-size:46px;font-weight:900;line-height:1.08;letter-spacing:-.02em;color:#fdba74;text-transform:uppercase}
.fr-heading:empty{display:none}
.fr-stage{position:relative;flex:1;display:flex;align-items:center;justify-content:center;padding:0 calc(var(--rm-safe-right,0px) / 2)}
.fr-foot{position:relative;display:flex;justify-content:space-between;align-items:center;gap:24px;padding-top:28px;padding-right:max(0px,calc(var(--rm-safe-right,0px) - 60px));border-top:1px solid rgba(245,239,230,.16);font-size:28px;font-weight:700;color:rgba(245,239,230,.72)}
.fr-badge{flex:none;background:#f97316;color:#1c0a02;padding:12px 26px;border-radius:999px;font-size:22px;font-weight:900;letter-spacing:.06em;text-transform:uppercase}
.fr-badge:empty{display:none}
.fr-progress{position:absolute;left:80px;right:80px;bottom:calc(var(--rm-safe-bottom,170px) - 16px)}
.fr .rm-title,.fr .rm-hook-title{color:#fff7ed}
.fr .rm-desc{color:rgba(245,239,230,.84)}
.fr .rm-tag{background:rgba(249,115,22,.14);color:#fdba74;border:1px solid rgba(249,115,22,.45);box-shadow:none}
.fr .rm-topic{background:#f97316;color:#1c0a02}"""

# Bản vuông 1:1 của khung mẫu: cùng HTML, chỉ đổi khổ và cỡ chữ.
FRAME_SAMPLE_CSS_SQUARE = FRAME_SAMPLE_CSS + """
.fr{height:1080px;padding:56px 64px 64px}
.fr-head{padding-bottom:20px}
.fr-brand{font-size:26px}
.fr-heading{margin-top:22px;font-size:36px}
.fr-foot{padding-top:20px;font-size:24px}
.fr-progress{bottom:24px;left:64px;right:64px}"""

BUILTIN_TEMPLATES: List[Dict] = [
    {
        "id": "dark_glass",
        "name": "Dark Glass",
        "thumb": "🌌",
        "desc": "Premium tối",
        "tokens": {
            "bg": "#0f172a",
            "bgOverlay": (
                "radial-gradient(circle at 50% 20%, rgba(244,63,94,0.35), transparent 70%), "
                "radial-gradient(circle at 80% 80%, rgba(168,85,247,0.4), transparent 70%), #0b0f19"
            ),
            "decor": "bubbles",
            "footerText": "Lưu lại để xem sau nhé!",
        },
    },
    {
        "id": "bright_pastel",
        "name": "Bright Pastel",
        "thumb": "🌸",
        "desc": "Nhẹ nhàng sáng",
        "tokens": {
            "bg": "#fff8f0",
            "bgOverlay": "",
            "decor": "blobs",
            "headerBg": "rgba(255,255,255,0.85)",
            "headerBorder": "2px solid rgba(255,192,203,0.5)",
            "headerRadius": 28,
            "headerTitleColor": "#e11d48",
            "headerSubColor": "#9f4c6b",
            "badgeBg": "linear-gradient(135deg, #f43f5e, #fb923c)",
            "cardBg": "rgba(255,255,255,0.92)",
            "cardBorder": "2px solid rgba(255,192,203,0.6)",
            "cardRadius": 40,
            "cardBlur": 20,
            "cardShadow": "0 20px 60px rgba(255,100,130,0.15)",
            "cardPadding": "56px 48px",
            "iconSize": 112,
            "iconWobble": 5,
            "titleColor": "#1a1a1a",
            "titleSize": 62,
            "descColor": "#4b4b4b",
            "descSize": 38,
            "highlightColor": "#e11d48",
            "highlightEffect": "underline",
            "footerBg": "rgba(255,255,255,0.9)",
            "footerBorder": "2px solid rgba(255,192,203,0.5)",
            "footerColor": "#9f4c6b",
            "footerIcon": "💕",
            "footerText": "Lưu lại kẻo trôi nhé!",
            "footerRadius": 24,
            "progressTrack": "rgba(255,192,203,0.3)",
            "progressFill": "linear-gradient(90deg, #f43f5e, #fb923c, #facc15)",
            "progressHeight": 10,
            # Nền sáng: chữ hook trắng mặc định sẽ chìm mất.
            "hookTitleColor": "#1a1a1a",
            "hookTopicBg": "linear-gradient(135deg, #f43f5e, #fb923c)",
            "grain": 3,
            "vignette": 6,
        },
    },
    {
        "id": "bold_minimal",
        "name": "Bold Minimal",
        "thumb": "⚡",
        "desc": "Gen-Z nổi bật",
        "tokens": {
            "bg": "#0a0a0a",
            "bgOverlay": "",
            "decor": "scanlines",
            "headerBg": "#111",
            "headerBorder": "2px solid #333",
            "headerRadius": 20,
            "headerSubColor": "#666",
            "headerUppercase": True,
            "badgeBg": "#facc15",
            "badgeColor": "#0a0a0a",
            "badgeText": "FOLLOW",
            "badgeRadius": 12,
            "cardBg": "#111",
            "cardBorder": "2px solid #333",
            "cardRadius": 28,
            "cardBlur": 0,
            "cardShadow": "",
            "cardAlign": "left",
            "cardPadding": "44px",
            "iconSize": 90,
            "iconWobble": 0,
            "tagBg": "#facc15",
            "tagColor": "#0a0a0a",
            "tagRadius": 10,
            "tagSize": 20,
            "tagUppercase": True,
            "titleSize": 64,
            "descColor": "#aaa",
            "descSize": 38,
            "highlightEffect": "plain",
            "divider": "#facc15",
            "footerBg": "#111",
            "footerBorder": "2px solid #facc15",
            "footerColor": "#facc15",
            "footerIcon": "💾",
            "footerText": "SAVE • SHARE • FOLLOW",
            "footerUppercase": True,
            "progressTrack": "#111",
            "progressFill": "#facc15",
            "progressHeight": 8,
            "anim": "slide_left",
            "hookTopicBg": "#facc15",
            "hookTopicColor": "#0a0a0a",
            "grain": 7,
            "vignette": 34,
        },
    },
    {
        "id": "fact_story_reel",
        "name": "Fact Story Reel",
        "thumb": "🌍",
        "desc": "Bạn Có Biết? • chữ + visual dưới",
        "tokens": {
            "bg": "#050505",
            "bgOverlay": "",
            "decor": "none",
            "highlightColor": "#22d3ee",
            "descColor": "#f8fafc",
            "footerText": "Theo dõi để xem thêm sự thật mỗi ngày",
            "footerColor": "#86efac",
            "html": """
<div class=\"fact-reel\">
  <header><div class=\"fact-brand\">{{channelName}}</div><div class=\"fact-sub\">{{subTitle}}</div></header>
  <div class=\"fact-kicker\">SỰ KIỆN CÓ THẬT MỖI NGÀY</div>
  <div class=\"fact-question\">BẠN CÓ BIẾT?</div>
  <div class=\"fact-copy\">{{descHtml}}</div>
  <div class=\"fact-visual\">{{image}}</div>
  <footer>{{cta}}</footer>
  <div class=\"fact-progress\">{{progress}}</div>
</div>
""",
            "css": """
.fact-reel{position:relative;height:1840px;background:#050505;color:#fff;overflow:hidden;font-family:'Be Vietnam Pro',Arial,sans-serif;display:flex;flex-direction:column;text-align:center}.fact-reel header{height:148px;display:flex;align-items:center;justify-content:center;gap:16px;border-bottom:2px solid #86efac55}.fact-brand{font-size:38px;font-weight:900;color:#bef264;text-transform:uppercase;text-shadow:0 0 18px #65a30d}.fact-sub{font-size:23px;font-weight:800;color:#f8fafc}.fact-kicker{font-size:32px;font-weight:900;color:#bef264;margin-top:35px}.fact-question{font-size:86px;line-height:1;font-weight:1000;color:#fff;margin:26px 0 28px;text-shadow:4px 4px 0 #7f1d1d}.fact-copy{padding:0 38px;font-size:33px;line-height:1.52;text-align:left;font-family:Georgia,'Times New Roman',serif;min-height:510px}.fact-copy .hl{color:#22d3ee!important;text-shadow:0 0 12px #0891b2}.fact-visual{height:640px;background:linear-gradient(145deg,#111827,#020617);display:flex;align-items:center;justify-content:center;overflow:hidden}.fact-visual img{width:100%!important;height:640px!important;object-fit:cover!important;border-radius:0!important}.fact-visual:empty:after{content:'THÊM ẢNH MINH HOẠ / B-ROLL';font:800 26px 'Be Vietnam Pro';letter-spacing:2px;color:#64748b} .fact-reel footer{margin-top:auto;padding:26px;font-size:28px;font-weight:800;color:#86efac;border-top:2px solid #86efac44} .fact-progress{position:absolute;left:0;right:0;bottom:0}
""",
        },
    },
    {
        # Mẫu cho kiểu "khung HTML": HTML dựng cả nền, tên kênh, heading và
        # footer, còn {{content}} ở giữa là chỗ chữ AI viết cho từng cảnh.
        "id": "html_frame",
        "name": "Khung HTML Mẫu",
        "thumb": "🧩",
        "desc": "Khung HTML • chữ giữa do AI viết",
        "tokens": {
            "bg": "#0c0a09",
            "highlightColor": "#fb923c",
            "highlightEffect": "glow",
            "progressTrack": "rgba(245,239,230,0.14)",
            "progressFill": "#f97316",
            "progressHeight": 6,
            "titleSize": 66,
            "descSize": 42,
            "hookTitleSize": 96,
            "html": FRAME_SAMPLE_HTML,
            "css": FRAME_SAMPLE_CSS,
            "htmlSquare": FRAME_SAMPLE_HTML,
            "cssSquare": FRAME_SAMPLE_CSS_SQUARE,
        },
    },
]

BUILTIN_IDS = {t["id"] for t in BUILTIN_TEMPLATES}
DEFAULT_TEMPLATE_ID = "dark_glass"

_ID_RE = re.compile(r"^[a-z0-9_-]{2,40}$")


# ─────────────────────────────────────────────────────────────────────────────
# ĐỌC / GHI
# ─────────────────────────────────────────────────────────────────────────────

def _load_config() -> Dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: Dict) -> bool:
    from config_store import write_json
    return write_json(CONFIG_FILE, cfg)


def load_custom_templates() -> List[Dict]:
    raw = _load_config().get("remotion_templates")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict) and item.get("id") and item["id"] not in BUILTIN_IDS:
            out.append(item)
    return out


def list_templates() -> List[Dict]:
    """Danh sách template cho giao diện: dựng sẵn trước, tự tạo sau."""
    items = []
    for tpl in BUILTIN_TEMPLATES:
        items.append({
            "id": tpl["id"],
            "name": tpl["name"],
            "thumb": tpl.get("thumb", "🎨"),
            "desc": tpl.get("desc", ""),
            "builtin": True,
            "tokens": resolve_tokens(tpl["id"]),
        })
    for tpl in load_custom_templates():
        items.append({
            "id": tpl["id"],
            "name": tpl.get("name") or tpl["id"],
            "thumb": tpl.get("thumb", "🎨"),
            "desc": tpl.get("desc", "Template của bạn"),
            "builtin": False,
            "tokens": resolve_tokens(tpl["id"]),
        })
    return items


def _find_raw(template_id: str) -> Optional[Dict]:
    for tpl in BUILTIN_TEMPLATES:
        if tpl["id"] == template_id:
            return tpl
    for tpl in load_custom_templates():
        if tpl["id"] == template_id:
            return tpl
    return None


def resolve_tokens(template_id: str) -> Dict:
    """
    Trả về BỘ TOKEN ĐẦY ĐỦ của một template.

    Template tự tạo có thể khai báo `base` để kế thừa một template dựng sẵn,
    phần `tokens` của nó chỉ ghi đè những gì khác biệt.
    """
    tokens = deepcopy(BASE_TOKENS)
    raw = _find_raw(template_id)
    if raw is None:
        raw = _find_raw(DEFAULT_TEMPLATE_ID) or {}
    base_id = raw.get("base")
    if base_id and base_id in BUILTIN_IDS and base_id != template_id:
        base_raw = _find_raw(base_id) or {}
        tokens.update(base_raw.get("tokens") or {})
    tokens.update(raw.get("tokens") or {})
    return sanitize_tokens(tokens)


# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE VIẾT BẰNG HTML
# ─────────────────────────────────────────────────────────────────────────────
# Có hai kiểu template HTML:
#
# 1. Khung HTML: HTML dựng cả khung 1080x1920 (nền, tên kênh, heading,
#    footer) và đặt {{content}} ở chỗ muốn chữ AI viết. Tool dựng khối chữ
#    của từng cảnh vào đó, có chuyển động theo giọng đọc.
# 2. Thẻ HTML: HTML chỉ dựng thẻ nội dung giữa khung, tool lo nền, đầu, chân.
#
# Các chỗ điền, tool sẽ thay bằng nội dung thật:
#
#   {{content}} {{icon}} {{tag}} {{title}} {{desc}} {{descHtml}} {{highlight}}
#   {{channelName}} {{subTitle}} {{cta}} {{badge}} {{topicLine}}
#   {{sceneIndex}} {{sceneCount}} {{image}} {{progress}}
#
# {{content}}, {{title}}, {{descHtml}}, {{image}} và {{progress}} được dựng
# có chuyển động; {{descHtml}} là lời thoại sáng dần theo giọng đọc, {{image}}
# là ảnh sản phẩm (rỗng khi cảnh không có ảnh), {{progress}} là thanh tiến
# trình chia đoạn theo cảnh.

HTML_PLACEHOLDERS = [
    "content", "icon", "tag", "title", "desc", "descHtml", "highlight", "channelName",
    "subTitle", "cta", "badge", "topicLine", "sceneIndex", "sceneCount", "image",
    "progress",
]

# Rộng tay vì file thiết kế sẵn hay nhúng ảnh base64. Giới hạn cũ 20000 ký tự
# cắt cụt giữa ảnh, làm hỏng cả phần HTML phía sau.
MAX_HTML_CHARS = 150000
MAX_CSS_CHARS = 40000

# Những thứ không được phép có trong template HTML. Đoạn HTML này chạy bên
# trong trình duyệt lúc render, nên script và tải tài nguyên từ ngoài đều bị
# gỡ: một template chỉ nên mô tả bố cục và màu sắc.
_HTML_SCRIPT_BLOCK = re.compile(r"<\s*(script|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_HTML_BLOCKED_TAGS = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|link|meta|base|form|input|video|audio|source)\b[^>]*>",
    re.I)
_HTML_EVENT_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_HTML_JS_URL = re.compile(r"(href|src|xlink:href)\s*=\s*(\"|')\s*javascript:[^\"']*(\"|')", re.I)
_HTML_REMOTE_SRC = re.compile(r"(src|href)\s*=\s*(\"|')\s*(https?:)?//[^\"']*(\"|')", re.I)


def sanitize_html_template(html: str) -> Tuple[str, List[str]]:
    """
    Dọn đoạn HTML người dùng dán vào và cho biết đã gỡ những gì.

    Trả về (html đã dọn, danh sách những thứ bị gỡ) để giao diện nói rõ chứ
    không lặng lẽ bỏ bớt phần người dùng viết.
    """
    text = str(html or "")[:MAX_HTML_CHARS]
    removed: List[str] = []

    # Gỡ trọn cả phần ruột của script và style-ngoài-luồng, không chỉ cặp thẻ,
    # nếu không mã bên trong sẽ nằm lại thành chữ hiện lên video.
    if _HTML_SCRIPT_BLOCK.search(text):
        removed.append("khối script")
        text = _HTML_SCRIPT_BLOCK.sub("", text)
    if _HTML_BLOCKED_TAGS.search(text):
        removed.append("thẻ iframe, form hoặc thẻ tải tài nguyên ngoài")
        text = _HTML_BLOCKED_TAGS.sub("", text)
    if _HTML_EVENT_ATTR.search(text):
        removed.append("thuộc tính chạy mã như onclick, onload")
        text = _HTML_EVENT_ATTR.sub("", text)
    if _HTML_JS_URL.search(text):
        removed.append("đường dẫn javascript:")
        text = _HTML_JS_URL.sub("", text)
    if _HTML_REMOTE_SRC.search(text):
        removed.append("ảnh và tài nguyên tải từ Internet (video render offline nên sẽ không hiện)")
        text = _HTML_REMOTE_SRC.sub("", text)

    return text.strip(), removed


def sanitize_css_template(css: str) -> str:
    """Dọn đoạn CSS đi kèm template HTML."""
    text = str(css or "")[:MAX_CSS_CHARS]
    text = re.sub(r"@import[^;]*;", "", text, flags=re.I)
    text = re.sub(r"url\s*\(\s*['\"]?\s*(https?:)?//[^)]*\)", "none", text, flags=re.I)
    text = re.sub(r"</?\s*(script|style)\b[^>]*>", "", text, flags=re.I)
    return text.strip()


def sanitize_tokens(tokens: Dict, partial: bool = False) -> Dict:
    """
    Ép mọi token về đúng kiểu và khoảng giá trị an toàn trước khi đưa vào React.

    partial=True: chỉ giữ đúng những token được khai báo, dùng khi LƯU template
    tự tạo. Nếu trả về bộ đầy đủ, phần kế thừa từ template gốc sẽ bị ghi đè bằng
    giá trị nền và template mới mất hết diện mạo của bản gốc.
    partial=False: trả bộ đầy đủ, dùng khi dựng theme để render.
    """
    clean: Dict = {} if partial else deepcopy(BASE_TOKENS)
    if not isinstance(tokens, dict):
        return clean

    for key, default in BASE_TOKENS.items():
        if key not in tokens:
            continue
        value = tokens[key]

        if key in ENUM_TOKENS:
            clean[key] = value if value in ENUM_TOKENS[key] else default
        elif key in NUMERIC_TOKENS:
            low, high = NUMERIC_TOKENS[key]
            try:
                clean[key] = max(low, min(high, int(float(value))))
            except (TypeError, ValueError):
                clean[key] = default
        elif isinstance(default, bool):
            clean[key] = bool(value)
        elif key in ("html", "htmlSquare"):
            clean[key] = sanitize_html_template(value)[0]
        elif key in ("css", "cssSquare"):
            clean[key] = sanitize_css_template(value)
        else:
            # Token chuỗi: chỉ nhận văn bản, cắt độ dài để không phình props.
            clean[key] = str(value)[:400] if isinstance(value, (str, int, float)) else default
    return clean


def save_custom_template(template_id: str, name: str, tokens: Dict,
                         thumb: str = "🎨", desc: str = "", base: str = DEFAULT_TEMPLATE_ID) -> Tuple[bool, str]:
    """Tạo mới hoặc cập nhật một template của người dùng."""
    tid = (template_id or "").strip().lower().replace(" ", "_")
    if not _ID_RE.match(tid):
        return False, "Mã template chỉ gồm chữ thường, số, dấu _ hoặc - (2 đến 40 ký tự)."
    if tid in BUILTIN_IDS:
        return False, f"'{tid}' là template dựng sẵn, không thể ghi đè. Hãy đặt mã khác."
    if not (name or "").strip():
        return False, "Vui lòng đặt tên cho template."
    if base not in BUILTIN_IDS:
        base = DEFAULT_TEMPLATE_ID

    cfg = _load_config()
    items = [t for t in (cfg.get("remotion_templates") or [])
             if isinstance(t, dict) and t.get("id") and t["id"] != tid]
    items.append({
        "id": tid,
        "name": name.strip()[:60],
        "thumb": (thumb or "🎨").strip()[:8],
        "desc": (desc or "Template của bạn").strip()[:80],
        "base": base,
        "tokens": sanitize_tokens(tokens, partial=True),
    })
    cfg["remotion_templates"] = items
    if not _save_config(cfg):
        return False, "Không ghi được config.json."

    message = f"Đã lưu template '{name.strip()}'."
    removed_all: List[str] = []
    for key in ("html", "htmlSquare"):
        if isinstance(tokens, dict) and tokens.get(key):
            removed_all += [r for r in sanitize_html_template(tokens[key])[1] if r not in removed_all]
    if removed_all:
        message += " Đã gỡ khỏi HTML: " + ", ".join(removed_all) + "."
    return True, message


def delete_custom_template(template_id: str) -> Tuple[bool, str]:
    if template_id in BUILTIN_IDS:
        return False, "Không thể xoá template dựng sẵn."
    cfg = _load_config()
    items = cfg.get("remotion_templates") or []
    remain = [t for t in items if isinstance(t, dict) and t.get("id") != template_id]
    if len(remain) == len(items):
        return False, "Không tìm thấy template cần xoá."
    cfg["remotion_templates"] = remain
    if not _save_config(cfg):
        return False, "Không ghi được config.json."
    return True, "Đã xoá template."
