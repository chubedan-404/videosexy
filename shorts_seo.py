"""
Gợi ý tiêu đề, mô tả và hashtag chuẩn SEO cho video vừa render ở Global Creator Studio.

Đầu vào là chính kịch bản của video (câu hook, dòng chủ đề, lời thoại từng
cảnh), nên gợi ý bám đúng nội dung chứ không đoán từ một dòng tiêu đề.

Thứ tự AI giống hệt phần viết kịch bản: 9Router trước, hỏng thì Gemini. Mỗi bên
được thử lại một lần kèm lý do sai của lần trước. Cả hai đều hỏng thì trả lỗi
rõ ràng, KHÔNG trả bộ tiêu đề mẫu.

Hai khối trả về:
    title_line        tiêu đề kèm hashtag, tối đa TITLE_LINE_MAX ký tự (giới hạn
                      tiêu đề của YouTube Shorts, dùng luôn cho Reels và TikTok)
    description_full  mô tả kèm khối hashtag ở cuối
"""
import json
import re
from typing import Dict, List, Optional, Tuple

import requests

from remotion_generator import (
    FORCE_GEMINI, GEMINI_FALLBACK_MODELS, _extract_json_blob, _looks_like_placeholder,
    _read_chat_content, _strip_accents, audience_guide, get_shorts_market,
)

TITLE_LINE_MAX = 100
TITLE_MIN = 12
TITLE_HASHTAGS_MAX = 3
DESC_HASHTAGS_MIN = 3
DESC_HASHTAGS_MAX = 6
DESC_MIN = 80
DESC_MAX = 1500
# Dòng đầu của mô tả là phần hiện ra trước nút "Xem thêm" trên Facebook và
# YouTube, nên từ khoá chính phải nằm trong khoảng này.
DESC_FIRST_LINE_MAX = 125

SEO_SYSTEM_PROMPT = (
    "Bạn là chuyên gia SEO cho video ngắn dọc trên Facebook Reels, YouTube Shorts và TikTok. "
    "Bạn luôn trả lời bằng DUY NHẤT một JSON Object hợp lệ, không kèm lời dẫn, không kèm markdown."
)

SEO_PROMPT_TEMPLATE = """Viết bộ tiêu đề, mô tả và hashtag để đăng video ngắn dưới đây.

NGÔN NGỮ VÀ THỊ TRƯỜNG: toàn bộ chữ, kể cả hashtag, viết bằng {language} tự nhiên cho người xem tại {market}.
NGƯỜI XEM: {audience_label}. {audience_guide}

CHỦ ĐỀ NGƯỜI DÙNG NHẬP: {topic}

KỊCH BẢN VIDEO (chỉ được dùng thông tin có trong đây, không bịa số liệu hay lời hứa mới):
{script}

YÊU CẦU SEO:
- "title": tiêu đề KHÔNG chứa hashtag, dài {title_min} tới {title_max} ký tự. Đặt từ khoá mà người xem sẽ gõ tìm kiếm ngay trong 40 ký tự đầu. Nói rõ người xem được gì sau video. Không viết hoa cả câu, không giật tít rẻ tiền, tối đa một emoji.
- "title_hashtags": 2 hoặc 3 hashtag ngắn đi kèm tiêu đề, tiêu đề cộng hashtag không quá {line_max} ký tự tính cả dấu cách.
- "description": 3 tới 5 câu ngắn, KHÔNG chứa hashtag. Câu đầu tiên dưới {first_line_max} ký tự, nhắc lại từ khoá chính và điều người xem nhận được, vì đây là phần hiện ra trước nút Xem thêm. Các câu sau tóm tắt giá trị thật của video. Câu cuối là một câu hỏi mời người xem bình luận. Không chèn đường link.
- "hashtags": {tags_min} tới {tags_max} hashtag cho phần mô tả: 1 tới 2 hashtag rộng của ngách, 2 tới 3 hashtag đúng chủ đề cụ thể của video, 1 hashtag về nhu cầu hoặc cộng đồng người xem. Không trùng nhau.
- Mọi hashtag bắt đầu bằng #, viết liền không dấu cách, không dấu gạch ngang, không dấu câu.{hashtag_note}
{retry}
Trả về JSON đúng cấu trúc:
{{"title": "...", "title_hashtags": ["#...", "#..."], "description": "...", "hashtags": ["#...", "#...", "#..."]}}"""

GEMINI_SEO_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "title_hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "description": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["title", "title_hashtags", "description", "hashtags"],
}

# Dấu câu làm gãy hashtag trên mạng xã hội. Không dùng \w để lọc vì \w của
# Python bỏ mất dấu thanh của chữ Thái, làm hỏng hashtag tiếng Thái.
_HASHTAG_BREAKERS = re.compile(r"[\s.,;:!?¡¿'\"“”‘’()\[\]{}<>/\\|@$%^&*+=~`\-–—…·،。、，！？：；「」『』（）#]")
_INLINE_HASHTAG = re.compile(r"(?:^|\s)(#[^\s#]+)")


def clean_hashtag(raw: str) -> str:
    """'# Giấc ngủ-ngon!' thành '#Giấcngủngon'. Trả chuỗi rỗng nếu không còn gì."""
    body = _HASHTAG_BREAKERS.sub("", str(raw or ""))
    return f"#{body}" if body else ""


def _unique_hashtags(values: List[str], limit: int) -> List[str]:
    seen, out = set(), []
    for value in values:
        tag = clean_hashtag(value)
        key = tag.lower()
        if len(tag) < 3 or key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def _pull_inline_hashtags(text: str) -> Tuple[str, List[str]]:
    """Tách hashtag model lỡ viết lẫn trong câu chữ ra một danh sách riêng."""
    found = _INLINE_HASHTAG.findall(text or "")
    stripped = _INLINE_HASHTAG.sub(" ", text or "")
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped, found


def build_title_line(title: str, hashtags: List[str], limit: int = TITLE_LINE_MAX) -> Tuple[str, List[str]]:
    """
    Ghép tiêu đề với hashtag, bỏ bớt hashtag cuối nếu vượt giới hạn. Chữ của
    tiêu đề không bao giờ bị cắt. Trả về (dòng tiêu đề, các hashtag bị bỏ).
    """
    line, dropped = title, []
    for tag in hashtags:
        candidate = f"{line} {tag}"
        if len(candidate) <= limit:
            line = candidate
        else:
            dropped.append(tag)
    return line, dropped


def _has_vietnamese_marks(tag: str) -> bool:
    return _strip_accents(tag).replace("đ", "d").replace("Đ", "D") != tag


def validate_seo(payload, first_line_max: int = DESC_FIRST_LINE_MAX, market_id: str = "vi-VN") -> Dict:
    """Kiểm tra và chuẩn hoá câu trả lời của AI. Ném ValueError kèm lý do cụ thể."""
    if not isinstance(payload, dict):
        raise ValueError("AI không trả về một object JSON")

    title, title_inline = _pull_inline_hashtags(str(payload.get("title") or ""))
    title = re.sub(r"\s+", " ", title).strip()
    # Viết hoa chữ đầu câu; chữ không có hoa thường (Nhật, Thái...) giữ nguyên.
    title = title[:1].upper() + title[1:]
    if _looks_like_placeholder(title) or len(title) < TITLE_MIN:
        raise ValueError(f"Tiêu đề trống, quá ngắn hoặc là chữ giữ chỗ: '{title}'")

    title_tags = _unique_hashtags(list(payload.get("title_hashtags") or []) + title_inline, TITLE_HASHTAGS_MAX)
    if not title_tags:
        raise ValueError("Tiêu đề thiếu hashtag đi kèm")
    title_line, dropped = build_title_line(title, title_tags)
    if len(dropped) == len(title_tags):
        raise ValueError(f"Tiêu đề dài {len(title)} ký tự, không còn chỗ cho hashtag trong giới hạn "
                         f"{TITLE_LINE_MAX} ký tự. Hãy viết tiêu đề ngắn hơn.")

    description, desc_inline = _pull_inline_hashtags(str(payload.get("description") or ""))
    if len(description) < DESC_MIN or _looks_like_placeholder(description):
        raise ValueError(f"Mô tả quá ngắn ({len(description)} ký tự), cần ít nhất {DESC_MIN}")
    if len(description) > DESC_MAX:
        raise ValueError(f"Mô tả quá dài ({len(description)} ký tự), tối đa {DESC_MAX}")

    hashtags = _unique_hashtags(list(payload.get("hashtags") or []) + desc_inline, DESC_HASHTAGS_MAX)
    if len(hashtags) < DESC_HASHTAGS_MIN:
        raise ValueError(f"Phần mô tả chỉ có {len(hashtags)} hashtag hợp lệ, cần ít nhất {DESC_HASHTAGS_MIN}")

    # Hashtag tiếng Việt quen dùng dạng không dấu. Tool KHÔNG tự bỏ dấu, vì bỏ
    # dấu bằng máy có thể đổi nghĩa (#ngủ thành #ngu); trả lại để AI tự chọn từ.
    if market_id == "vi-VN":
        accented = [t for t in title_tags + hashtags if _has_vietnamese_marks(t)]
        if accented:
            raise ValueError("Hashtag tiếng Việt phải viết không dấu, đang có: " + " ".join(accented)
                             + ". Chọn từ khác nếu bỏ dấu làm đổi nghĩa (ví dụ dùng #giacngu thay cho #ngủ).")

    notes = []
    # Tiếng Nhật và tiếng Trung không có dấu cách sau dấu chấm câu.
    first_line = re.split(r"(?<=[。！？])|(?<=[.!?])\s|\n", description, maxsplit=1)[0].strip()
    # Tiếng Thái không dùng dấu chấm câu nên không tách được câu đầu; bỏ qua lời nhắc.
    if market_id != "th-TH" and len(first_line) > first_line_max:
        notes.append(f"Câu đầu của mô tả dài {len(first_line)} ký tự, phần sau {first_line_max} ký tự "
                     "sẽ bị ẩn sau nút Xem thêm.")
    if dropped:
        notes.append("Bỏ bớt " + " ".join(dropped) + f" để tiêu đề không quá {TITLE_LINE_MAX} ký tự.")

    return {
        "title": title,
        "title_hashtags": [t for t in title_tags if t not in dropped],
        "title_line": title_line,
        "description": description,
        "hashtags": hashtags,
        "description_full": f"{description}\n\n{' '.join(hashtags)}",
        "notes": notes,
    }


def parse_seo_response(content: str, market_id: str = "vi-VN") -> Dict:
    blob = _extract_json_blob(content)
    if not blob:
        raise ValueError("AI trả về nội dung rỗng")
    if "{" not in blob:
        # Ví dụ combo 9Router trỏ vào model đã ngừng: trả HTTP 200 kèm một câu thông báo.
        raise ValueError(f"AI không trả JSON mà trả lời: “{content.strip()[:160]}”")
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI trả về JSON hỏng hoặc bị cắt giữa chừng ({exc.msg})") from exc
    return validate_seo(parsed, market_id=market_id)


def script_digest(scenes: List[Dict]) -> str:
    """Tóm kịch bản thành các dòng chữ cho AI đọc: câu hook trước, rồi từng cảnh."""
    lines, number = [], 0
    for scene in scenes or []:
        title = str(scene.get("title") or "").strip()
        desc = str(scene.get("desc") or "").strip()
        if scene.get("kind") == "hook":
            if title:
                lines.append(f"Câu mở đầu (hook): {title}")
            if desc:
                lines.append(f"Dòng chủ đề trên màn hình: {desc}")
        elif title or desc:
            number += 1
            lines.append(f"Cảnh {number}: {title}. {desc}")
    return "\n".join(lines)


def build_seo_prompt(scenes: List[Dict], topic: str = "", market_id: str = "vi-VN",
                     audience: str = "", reason: str = "") -> str:
    market = get_shorts_market(market_id)
    label, guide = audience_guide(audience)
    hashtag_note = (" Với tiếng Việt, hashtag viết không dấu, ví dụ #giacngu #meovat."
                    if market["id"] == "vi-VN" else "")
    retry = (f"\nLẦN TRƯỚC BỊ TỪ CHỐI VÌ: {reason}. Sửa đúng lỗi đó.\n" if reason else "")
    return SEO_PROMPT_TEMPLATE.format(
        language=market["language"], market=market["market"],
        audience_label=label, audience_guide=guide,
        topic=(topic or "").strip() or "(không nhập, suy ra từ kịch bản)",
        script=script_digest(scenes),
        title_min=TITLE_MIN, title_max=TITLE_LINE_MAX - 20, line_max=TITLE_LINE_MAX,
        first_line_max=DESC_FIRST_LINE_MAX,
        tags_min=DESC_HASHTAGS_MIN, tags_max=DESC_HASHTAGS_MAX,
        hashtag_note=hashtag_note, retry=retry,
    )


def _ask_9router(prompt: str, base_url: str, model: str, api_key: str, timeout: int = 90,
                 market_id: str = "vi-VN") -> Tuple[Optional[Dict], str]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SEO_SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 1500,
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
        return parse_seo_response(content, market_id), ""
    except requests.exceptions.ConnectionError:
        return None, f"Không kết nối được 9Router tại {base_url}. Kiểm tra 9Router đã chạy chưa."
    except requests.exceptions.Timeout:
        return None, f"9Router quá thời gian chờ ({timeout}s)"
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Lỗi gọi 9Router: {exc}"


def _ask_gemini(prompt: str, api_key: str, model: str, timeout: int = 60,
                market_id: str = "vi-VN") -> Tuple[Optional[Dict], str]:
    if not api_key:
        return None, "Chưa cấu hình Gemini API Key trong phần Cấu hình AI."
    errors = []
    for candidate in [model] + [m for m in GEMINI_FALLBACK_MODELS if m != model]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{candidate}:generateContent?key={api_key}"
        config = {
            "temperature": 0.7,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_SEO_SCHEMA,
            # Tắt suy luận nội bộ: không tắt thì nó ăn hết hạn mức token và câu
            # trả lời bị cắt ngang.
            "thinkingConfig": {"thinkingBudget": 0},
        }
        payload = {
            "systemInstruction": {"parts": [{"text": SEO_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config,
        }
        try:
            res = requests.post(url, json=payload, timeout=timeout)
            if res.status_code == 400 and "thinkingConfig" in config:
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
            text = "".join(p.get("text", "") for p in (first.get("content", {}) or {}).get("parts") or []).strip()
            if not text:
                errors.append(f"Gemini ({candidate}) không trả về nội dung (finishReason={first.get('finishReason') or 'không rõ'})")
                continue
            if first.get("finishReason") == "MAX_TOKENS":
                errors.append(f"Gemini ({candidate}) bị cắt giữa chừng vì chạm giới hạn token")
                continue
            return parse_seo_response(text, market_id), ""
        except ValueError as exc:
            errors.append(f"Gemini ({candidate}): {exc}")
        except Exception as exc:
            errors.append(f"Lỗi gọi Gemini ({candidate}): {exc}")
    return None, " | ".join(errors) or "Gemini không phản hồi"


def generate_seo(scenes: List[Dict], topic: str = "", market_id: str = "vi-VN", audience: str = "",
                 base_url: str = "http://localhost:20128/v1", model: str = "combo1", api_key: str = "9router-key",
                 gemini_api_key: str = "", gemini_model: str = "gemini-2.5-flash", **_ignored) -> Dict:
    """
    Trả về dict: success, seo (khi thành công), source, model, warning, error,
    attempts (nhật ký từng lần thử để giao diện hiện khi lỗi).
    """
    if not any((s.get("title") or s.get("desc")) for s in scenes or []):
        return {"success": False, "error": "Chưa có kịch bản để viết tiêu đề và mô tả.", "attempts": []}

    attempts: List[str] = []

    def _try(label, ask):
        result, err = ask(build_seo_prompt(scenes, topic, market_id, audience))
        if result:
            attempts.append(f"{label}: thành công")
            return result, ""
        attempts.append(f"{label}: {err}")
        result, err2 = ask(build_seo_prompt(scenes, topic, market_id, audience, reason=err))
        if result:
            attempts.append(f"{label} (thử lại): thành công")
            return result, ""
        attempts.append(f"{label} (thử lại): {err2}")
        return None, err2 or err

    force_gemini = (model or "").strip() == FORCE_GEMINI
    router_error = ""
    if base_url and not force_gemini:
        seo, router_error = _try("9Router", lambda p: _ask_9router(p, base_url, model, api_key, market_id=market_id))
        if seo:
            return {"success": True, "seo": seo, "source": "9router", "model": model,
                    "warning": "", "error": "", "attempts": attempts}

    seo, gemini_error = _try("Gemini", lambda p: _ask_gemini(p, gemini_api_key, gemini_model, market_id=market_id))
    if seo:
        warning = "" if force_gemini else f"9Router lỗi ({router_error}). Đã tự chuyển sang {gemini_model}."
        return {"success": True, "seo": seo, "source": "gemini", "model": gemini_model,
                "warning": warning, "error": "", "attempts": attempts}

    return {"success": False, "seo": None, "source": "", "model": "", "warning": "",
            "error": (f"Gemini: {gemini_error}" if force_gemini
                      else f"9Router: {router_error or 'không dùng được'} | Gemini: {gemini_error}"),
            "attempts": attempts}
