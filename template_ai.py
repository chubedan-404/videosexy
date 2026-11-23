"""
template_ai.py
──────────────
AI chỉnh template HTML cho khớp một kênh đăng (Facebook Reel, YouTube Shorts,
Facebook Post 1:1).

Hai hướng, người dùng chọn:

- fix: giữ nguyên thiết kế của file (màu, font, thương hiệu, câu chữ), chỉ đổi
  kích thước, bố cục, vị trí cho vừa khổ và tránh vùng giao diện nền tảng đè lên.
- generate: dựng khung mới hoàn toàn theo phong cách của file. Hợp với file vốn
  không sinh ra để làm video, như email hay landing page.

Việc đo template có lệch khổ không nằm ở trình duyệt (auditTemplateFit trong
static/app.js), vì phải dựng HTML thật mới đo được. Danh sách lỗi đo được gửi
kèm lên đây để AI biết cần sửa gì.

Thứ tự AI giống mọi tính năng khác: 9Router trước, hỏng thì Gemini.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

import requests

from remotion_templates import sanitize_css_template, sanitize_html_template
from video_formats import get_format, is_square

FORCE_GEMINI = "gemini-direct"
GEMINI_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]
MODES = ("fix", "generate")
MAX_SOURCE_CHARS = 30000

GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "html": {"type": "STRING"},
        "css": {"type": "STRING"},
        "changes": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["html", "css", "changes"],
}

SYSTEM_PROMPT = """You are a senior motion designer. You write HTML/CSS frames for short social videos.
A headless browser renders your frame at an exact pixel size, then a video tool animates the scene text inside it.

Return ONLY a JSON object: {"html": "...", "css": "...", "changes": ["...", "..."]}.
- "html": markup only, no <style>, no <script>, no <html>/<head>/<body>.
- "css": plain CSS for that markup.
- "changes": 3 to 7 short sentences IN VIETNAMESE (with diacritics) telling the user what you changed and why.

Hard rules for the frame:
1. Exactly one root <div>. Its CSS: width:{W}px; height:{H}px; position:relative; overflow:hidden; box-sizing:border-box.
   Everything must fit inside it. No fixed widths like 600px, no layout tables, no max-width that leaves empty sides.
2. Put the placeholder {{content}} exactly once, as the only child of a stage element that takes the remaining space
   (flex:1; min-height:0; display:flex; align-items:center; justify-content:center). Never give the stage a fixed height or min-height.
   The tool injects each scene's text there: either a hook headline (up to 3 lines at about {HOOK}px) or a tag + title + 3 to 6 lines
   of body text at about {BODY}px, and shrinks it automatically if it does not fit.
   HEIGHT BUDGET: after the safe padding, {BUDGET}px of height remain. Every other row (brand row, heading, footer, badges, logos)
   must share at most {ROWS}px so the stage keeps at least {STAGE}px. Nothing may be pushed below the bottom padding.
3. Use these placeholders instead of hard-coding the same text: {{channelName}} (channel/brand name), {{subTitle}} (a heading line),
   {{cta}} (footer call to action), {{badge}} (follow button text), {{progress}} (a progress bar, give its container a width),
   {{sceneIndex}} and {{sceneCount}}. Hide empty ones with :empty{display:none}.
4. {ZONES}
5. Readable on a phone: no text smaller than 26px. Strong contrast between text and background.
6. Font: 'Be Vietnam Pro', sans-serif is installed (weights 500-900). No @import, no url(http...), no web fonts, no external images.
7. No @keyframes, no animation, no transition: they are not frame-accurate in the renderer.
   For slow motion you may use var(--rm-t), a number going from 0 to 1 over the whole video, inside calc(),
   e.g. transform: scale(calc(1 + var(--rm-t) * 0.08)).
8. Style the injected scene text through these classes so it matches your frame:
   .rm-title, .rm-desc, .rm-tag, .rm-hl (highlighted phrase), .rm-hook-title, .rm-topic.
9. {IMAGES}
10. HTML under 12000 characters, CSS under 8000. Keep any Vietnamese text with its diacritics."""

MODE_BRIEF = {
    "fix": """TASK: ADAPT the source design to the canvas above.
Keep its identity: palette, fonts, logo or brand text, decorative style, and its copy where it still belongs in a video frame.
Change sizes, layout and positions so it fills the canvas edge to edge, fixes every listed problem, and makes {{content}} the main area.
Drop parts that do not belong in a video frame (email preheaders, unsubscribe or legal footers, long paragraphs, forms):
the scene text will appear in {{content}}.""",
    "generate": """TASK: DESIGN A NEW frame for the canvas above in the visual style of the source.
Extract its palette, fonts, logo or brand name and shape language. Do not copy its layout.
Premium and uncluttered: a brand row at the top ({{channelName}}), an optional heading line ({{subTitle}}),
a large stage for {{content}}, a footer with {{cta}} and {{badge}}, and a {{progress}} bar.""",
}


# ─────────────────────────────────────────────────────────────────────────────
# DỰNG PROMPT
# ─────────────────────────────────────────────────────────────────────────────

_DATA_URI = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", re.I)


def protect_data_uris(text: str, store: Dict[str, str]) -> str:
    """
    Thay ảnh nhúng base64 bằng mã ngắn trước khi gửi AI.

    Một ảnh nhúng dài cả trăm nghìn ký tự: gửi nguyên thì tốn token mà AI còn
    hay chép sai. Sau khi AI trả về, restore_data_uris đặt ảnh gốc lại đúng chỗ.
    """
    def repl(match):
        key = f"__IMG_{len(store) + 1}__"
        store[key] = match.group(0)
        return key
    return _DATA_URI.sub(repl, text or "")


def restore_data_uris(text: str, store: Dict[str, str]) -> str:
    for key, value in store.items():
        text = text.replace(key, value)
    return text


def zones_rule(fmt: Dict) -> str:
    if not fmt["zones"]:
        return ("This is a feed post: no platform UI covers it. Keep a 48px margin around text and logos "
                "so nothing touches the edges.")
    rects = "; ".join(f"{z['name']}: x={z['x']} y={z['y']} w={z['w']} h={z['h']}" for z in fmt["zones"])
    safe = fmt["safe"]
    return (f"{fmt['label']} draws its own interface over these rectangles, so NO text, logo, button or CTA may sit inside them "
            f"(background and decoration are fine): {rects}. Pad your layout with the CSS variables "
            f"var(--rm-safe-top) (= {safe['top']}px here), var(--rm-safe-bottom) (= {safe['bottom']}px) and "
            f"var(--rm-safe-right) (= {safe['right']}px) instead of hard numbers, so the same frame also fits the other 9:16 platform.")


def build_prompt(mode: str, fmt: Dict, html: str, css: str, issues: List[str],
                 brief: str = "", reason: str = "", image_keys: Optional[List[str]] = None) -> Tuple[str, str]:
    """Trả về (system prompt, user prompt)."""
    square = is_square(fmt)
    safe = fmt["safe"]
    margin = 48 if square else 24
    budget = fmt["height"] - safe["top"] - safe["bottom"] - 2 * margin
    stage = 560 if square else min(900, int(budget * 0.66))
    images = (f"Keep these image tokens exactly as they are, they stand for embedded images: {', '.join(image_keys)}. "
              "Do not invent other image tokens or image paths."
              if image_keys else
              "The source has no usable images (remote images cannot load when rendering offline). "
              "Do not add <img> tags or image URLs; use CSS shapes, gradients and text for the brand look.")
    head = (SYSTEM_PROMPT
            .replace("{W}", str(fmt["width"]))
            .replace("{H}", str(fmt["height"]))
            .replace("{HOOK}", "72" if square else "90")
            .replace("{BODY}", "34" if square else "40")
            .replace("{BUDGET}", str(budget))
            .replace("{ROWS}", str(budget - stage))
            .replace("{STAGE}", str(stage))
            .replace("{IMAGES}", images)
            .replace("{ZONES}", zones_rule(fmt)))
    parts = [
        f"PLATFORM: {fmt['label']} ({fmt['ratio']}, canvas {fmt['width']}x{fmt['height']}px).",
        MODE_BRIEF[mode],
    ]
    if issues:
        parts.append("PROBLEMS MEASURED ON THE SOURCE AT THIS CANVAS SIZE (fix all of them):\n"
                     + "\n".join(f"- {text}" for text in issues[:12]))
    if brief:
        parts.append(f"USER NOTE: {brief[:500]}")
    if reason:
        parts.append(f"YOUR PREVIOUS ANSWER WAS REJECTED: {reason}. Fix that and answer again.")
    parts.append("SOURCE HTML:\n" + (html or "(empty)")[:MAX_SOURCE_CHARS])
    parts.append("SOURCE CSS:\n" + (css or "(empty)")[:MAX_SOURCE_CHARS // 2])
    return head, "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# ĐỌC CÂU TRẢ LỜI
# ─────────────────────────────────────────────────────────────────────────────

def _json_blob(content: str) -> str:
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("AI không trả về JSON")
    return text[start:end + 1]


def parse_response(content: str, store: Dict[str, str]) -> Dict:
    """
    Kiểm câu trả lời của AI trước khi đưa cho người dùng.

    Thiếu {{content}} là hỏng hẳn: khung đẹp mấy mà không có chỗ cho chữ AI thì
    video ra chỉ là trang tĩnh. Trường hợp đó trả lỗi để hỏi lại, không trả về.
    """
    try:
        data = json.loads(_json_blob(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON của AI hỏng: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("AI không trả về object JSON")
    html = str(data.get("html") or "")
    css = str(data.get("css") or "")
    # AI hay lỡ tay để CSS trong thẻ <style> ngay trong HTML: tách ra.
    styles: List[str] = []
    html = re.sub(r"<style\b[^>]*>(.*?)</style>",
                  lambda m: styles.append(m.group(1)) or "", html, flags=re.I | re.S)
    css = "\n".join([css] + styles).strip()
    if not re.search(r"\{\{\s*content\s*\}\}", html):
        raise ValueError("HTML thiếu chỗ điền {{content}}")
    if len(re.findall(r"\{\{\s*content\s*\}\}", html)) > 1:
        raise ValueError("HTML có nhiều hơn một chỗ {{content}}")

    html = restore_data_uris(html, store)
    css = restore_data_uris(css, store)
    # Mã ảnh AI tự bịa (không có trong file gốc) trỏ tới đường dẫn không tồn tại:
    # gỡ hẳn thẻ ảnh đó thay vì để video có ô ảnh vỡ.
    invented = re.findall(r"__IMG_\d+__", html + css)
    if invented:
        html = re.sub(r"<img\b[^>]*__IMG_\d+__[^>]*>", "", html, flags=re.I)
        html = re.sub(r"__IMG_\d+__", "", html)
        css = re.sub(r"url\(\s*['\"]?__IMG_\d+__['\"]?\s*\)", "none", css)
    clean_html, removed = sanitize_html_template(html)
    clean_css = sanitize_css_template(css)
    changes = [str(c).strip() for c in (data.get("changes") or []) if str(c).strip()][:8]
    if invented:
        changes.append("Tool đã gỡ ảnh AI tự thêm vào mà file gốc không có.")
    if removed:
        changes.append("Tool đã gỡ khỏi HTML: " + ", ".join(removed) + ".")
    if re.search(r"@keyframes|animation\s*:", clean_css, re.I):
        changes.append("CSS vẫn còn @keyframes hoặc animation: khi render chúng không chạy đúng nhịp.")
    return {"html": clean_html, "css": clean_css, "changes": changes}


# ─────────────────────────────────────────────────────────────────────────────
# GỌI AI
# ─────────────────────────────────────────────────────────────────────────────

def _read_chat_content(res: requests.Response) -> str:
    try:
        data = res.json()
    except ValueError:
        return res.text or ""
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def _ask_9router(system: str, prompt: str, store: Dict[str, str], base_url: str, model: str,
                 api_key: str, timeout: int = 180) -> Tuple[Optional[Dict], str]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 12000,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key or '9router-key'}", "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}/chat/completions"
    try:
        res = requests.post(url, headers=headers, json=body, timeout=timeout)
        if res.status_code == 400 and "max_tokens" in res.text:
            body.pop("max_tokens", None)
            res = requests.post(url, headers=headers, json=body, timeout=timeout)
        if res.status_code != 200:
            detail = res.text[:200]
            try:
                err = res.json()
                detail = err.get("error", {}).get("message") or err.get("message") or detail
            except Exception:
                pass
            if res.status_code == 401 and api_key in ("", None, "9router-key"):
                detail = "Chưa điền khoá 9Router trong phần Cấu hình AI."
            return None, f"9Router HTTP {res.status_code}: {detail}"
        content = _read_chat_content(res)
        if not content:
            return None, "9Router trả về nội dung rỗng"
        return parse_response(content, store), ""
    except requests.exceptions.ConnectionError:
        return None, f"Không kết nối được 9Router tại {base_url}. Kiểm tra 9Router đã chạy chưa."
    except requests.exceptions.Timeout:
        return None, f"9Router quá thời gian chờ ({timeout}s)"
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Lỗi gọi 9Router: {exc}"


def _ask_gemini(system: str, prompt: str, store: Dict[str, str], api_key: str, model: str,
                timeout: int = 180) -> Tuple[Optional[Dict], str]:
    if not api_key:
        return None, "Chưa cấu hình Gemini API Key trong phần Cấu hình AI."
    errors = []
    for candidate in [model] + [m for m in GEMINI_FALLBACK_MODELS if m != model]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{candidate}:generateContent?key={api_key}"
        config = {
            "temperature": 0.5,
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_SCHEMA,
            # Cho suy nghĩ một chút về bố cục, nhưng chặn trần để câu trả lời
            # không bị cắt ngang vì hết token.
            "thinkingConfig": {"thinkingBudget": 2048},
        }
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config,
        }
        try:
            res = requests.post(url, json=payload, timeout=timeout)
            if res.status_code == 400 and "thinking" in res.text.lower():
                config.pop("thinkingConfig")
                res = requests.post(url, json=payload, timeout=timeout)
            if res.status_code != 200:
                detail = res.text[:200]
                try:
                    detail = res.json().get("error", {}).get("message", detail)
                except Exception:
                    pass
                errors.append(f"Gemini HTTP {res.status_code} ({candidate}): {detail}")
                continue
            data = res.json()
            first = (data.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (first.get("content", {}) or {}).get("parts") or []
                           if not p.get("thought")).strip()
            if first.get("finishReason") == "MAX_TOKENS":
                errors.append(f"Gemini ({candidate}) bị cắt giữa chừng vì chạm giới hạn token")
                continue
            if not text:
                errors.append(f"Gemini ({candidate}) không trả về nội dung (finishReason={first.get('finishReason') or 'không rõ'})")
                continue
            return parse_response(text, store), ""
        except ValueError as exc:
            errors.append(f"Gemini ({candidate}): {exc}")
        except Exception as exc:
            errors.append(f"Lỗi gọi Gemini ({candidate}): {exc}")
    return None, " | ".join(errors) or "Gemini không phản hồi"


def fit_template(mode: str, format_id: str, html: str, css: str = "", issues: Optional[List[str]] = None,
                 brief: str = "", base_url: str = "http://localhost:20128/v1", model: str = "combo1",
                 api_key: str = "9router-key", gemini_api_key: str = "",
                 gemini_model: str = "gemini-2.5-flash", **_ignored) -> Dict:
    """
    Trả về dict: success, html, css, changes, source, model, warning, error,
    attempts (nhật ký từng lần thử để giao diện hiện khi lỗi).
    """
    if mode not in MODES:
        return {"success": False, "error": "Chế độ phải là fix hoặc generate.", "attempts": []}
    if not (html or "").strip():
        return {"success": False, "error": "Chưa có HTML để AI chỉnh.", "attempts": []}

    fmt = get_format(format_id)
    store: Dict[str, str] = {}
    src_html = protect_data_uris(html, store)
    src_css = protect_data_uris(css, store)
    issues = [str(i) for i in (issues or []) if str(i).strip()]
    attempts: List[str] = []

    image_keys = list(store.keys())

    def _try(label, ask):
        system, prompt = build_prompt(mode, fmt, src_html, src_css, issues, brief, image_keys=image_keys)
        result, err = ask(system, prompt)
        if result:
            attempts.append(f"{label}: thành công")
            return result, ""
        attempts.append(f"{label}: {err}")
        system, prompt = build_prompt(mode, fmt, src_html, src_css, issues, brief, reason=err,
                                      image_keys=image_keys)
        result, err2 = ask(system, prompt)
        if result:
            attempts.append(f"{label} (thử lại): thành công")
            return result, ""
        attempts.append(f"{label} (thử lại): {err2}")
        return None, err2 or err

    force_gemini = (model or "").strip() == FORCE_GEMINI
    router_error = ""
    if base_url and not force_gemini:
        result, router_error = _try("9Router", lambda s, p: _ask_9router(s, p, store, base_url, model, api_key))
        if result:
            return {"success": True, **result, "source": "9router", "model": model,
                    "format": fmt["id"], "warning": "", "error": "", "attempts": attempts}

    result, gemini_error = _try("Gemini", lambda s, p: _ask_gemini(s, p, store, gemini_api_key, gemini_model))
    if result:
        warning = "" if force_gemini else f"9Router lỗi ({router_error}). Đã tự chuyển sang {gemini_model}."
        return {"success": True, **result, "source": "gemini", "model": gemini_model,
                "format": fmt["id"], "warning": warning, "error": "", "attempts": attempts}

    return {"success": False, "html": "", "css": "", "changes": [], "source": "", "model": "",
            "format": fmt["id"], "warning": "",
            "error": (f"Gemini: {gemini_error}" if force_gemini
                      else f"9Router: {router_error or 'không dùng được'} | Gemini: {gemini_error}"),
            "attempts": attempts}
