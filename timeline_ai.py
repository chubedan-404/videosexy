"""
timeline_ai.py
──────────────
Sửa timeline bằng câu lệnh tiếng Việt.

AI KHÔNG được trả về một bản timeline mới. Nó chỉ được trả về một danh sách
thao tác nhỏ, có tên rõ ràng, trên những khối đang có. Tool tự kiểm tra từng
thao tác rồi mới cho giao diện áp dụng.

Lý do làm chặt như vậy: nếu để model viết lại cả kịch bản, nó sẽ vừa sửa chỗ
bạn nhờ vừa lặng lẽ đổi những câu bạn không hề nhắc tới, và bạn không có cách
nào biết. Với danh sách thao tác thì mọi thay đổi đều đếm được, hiện được lên
màn hình để bạn duyệt, và bỏ được từng cái một.

Thứ tự gọi AI: 9Router trước, Gemini 2.5 sau, đúng như mọi chỗ khác trong tool.
Hỏng cả hai thì báo lỗi, không bịa ra thao tác nào.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

import requests

from remotion_generator import (
    GEMINI_FALLBACK_MODELS,
    _extract_json_blob,
    _read_chat_content,
)

# Số khối tối đa gửi kèm một lần hỏi. Kịch bản dài hơn thì cắt bớt phần đuôi,
# vì nhồi cả trăm câu vào một lời nhắc chỉ làm model trả lời cẩu thả hơn.
MAX_CLIPS_IN_PROMPT = 80
MAX_INSTRUCTION_CHARS = 800

SYSTEM_PROMPT = (
    "Bạn là trợ lý dựng video ngắn tiếng Việt. Người dùng đang mở một timeline "
    "và nhờ bạn sửa. Bạn chỉ được trả lời bằng JSON đúng khuôn đã cho, gồm danh "
    "sách thao tác và một câu tóm tắt tiếng Việt. Không giải thích thêm, không "
    "viết lời dẫn, không dùng dấu tiếng Anh thay cho tiếng Việt."
)

# Bộ thao tác cố ý hẹp. Thêm thao tác mới thì phải thêm cả phần kiểm tra bên dưới.
OP_FIELDS = {
    "set_text": ("id", "value"),
    "set_source": ("id", "value"),
    "set_time": ("id", "start", "end"),
    "set_duration": ("id", "seconds"),
    "set_voice": ("id", "voice"),
    "set_speaker": ("id", "speaker"),
    "set_field": ("id", "field", "value"),
    "delete": ("id",),
    "merge": ("id", "with"),
    "split": ("id", "at_word"),
    "insert_after": ("id", "text"),
    "move": ("id", "to_index"),
}

SCENE_FIELDS = ("tag", "icon", "highlightWord")

GEMINI_OPS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "ops": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "op": {"type": "STRING"},
                    "id": {"type": "STRING"},
                    "value": {"type": "STRING"},
                    "field": {"type": "STRING"},
                    "start": {"type": "NUMBER"},
                    "end": {"type": "NUMBER"},
                    "seconds": {"type": "NUMBER"},
                    "voice": {"type": "STRING"},
                    "speaker": {"type": "STRING"},
                    "with": {"type": "STRING"},
                    "at_word": {"type": "INTEGER"},
                    "to_index": {"type": "INTEGER"},
                    "text": {"type": "STRING"},
                    "source": {"type": "STRING"},
                },
                "required": ["op"],
            },
        },
    },
    "required": ["ops", "summary"],
}


def _clip_line(clip: Dict, mode: str) -> str:
    """Một dòng mô tả khối, đủ để model biết nó đang sửa cái gì."""
    span = f"{float(clip.get('start', 0)):.1f}-{float(clip.get('end', 0)):.1f}s"
    bits = [f"id={clip.get('id')}", f"#{clip.get('order', 0) + 1}", span]
    if mode == "sequential":
        bits.append("hook" if clip.get("kind") == "hook" else "body")
        if clip.get("locked"):
            bits.append("đã chốt thời lượng")
        bits.append(f"tiêu đề: {clip.get('source', '')}")
        bits.append(f"lời thoại: {clip.get('text', '')}")
        if clip.get("voice"):
            bits.append(f"giọng riêng: {clip['voice']}")
    else:
        bits.append(f"nhân vật: {clip.get('laneId', '')}")
        bits.append(f"gốc: {clip.get('source', '')}")
        bits.append(f"dịch: {clip.get('text', '')}")
    return " | ".join(str(b) for b in bits)


def build_prompt(instruction: str, clips: List[Dict], mode: str,
                 lanes: List[Dict], voices: List[Dict]) -> str:
    """Lời nhắc gửi cho AI: khối đang có, thao tác được phép, và điều người dùng nhờ."""
    lines = [_clip_line(clip, mode) for clip in clips[:MAX_CLIPS_IN_PROMPT]]

    if mode == "sequential":
        which = ("Đây là timeline của một video dựng theo cảnh. Các cảnh chạy nối "
                 "đuôi nhau, không có khoảng trống. Cảnh đầu là hook ba giây.")
        timing = ("- set_duration: đổi độ dài một cảnh, đơn vị giây, ví dụ "
                  '{\"op\":\"set_duration\",\"id\":\"sc2\",\"seconds\":4.5}. '
                  "Chỉ dùng khi người dùng nói rõ về thời lượng.\n"
                  "- set_voice: đổi giọng đọc của một cảnh, dùng đúng mã giọng trong danh sách giọng.\n"
                  "- set_field: đổi nhãn, biểu tượng hoặc từ khoá phát sáng. "
                  'field chỉ nhận "tag", "icon" hoặc "highlightWord". '
                  "Từ khoá phát sáng bắt buộc phải là cụm có thật trong lời thoại của chính cảnh đó.\n"
                  "- move: đổi thứ tự cảnh, to_index đếm từ 0.\n")
        text_note = ("set_source đổi TIÊU ĐỀ cảnh, set_text đổi LỜI THOẠI sẽ được đọc lên.")
    else:
        which = ("Đây là timeline phụ đề của một video có sẵn. Mốc thời gian bám "
                 "theo hình trong video gốc, nên TUYỆT ĐỐI không tự đổi thời gian "
                 "trừ khi người dùng nói thẳng về thời gian.")
        timing = ("- set_time: đổi mốc bắt đầu và kết thúc của một câu, đơn vị giây.\n"
                  "- set_speaker: đổi nhân vật của một câu, dùng đúng mã nhân vật trong danh sách.\n")
        text_note = ("set_source đổi câu GỐC, set_text đổi bản DỊCH. Bình thường "
                     "người dùng chỉ muốn sửa bản dịch.")

    voice_block = ""
    if mode == "sequential" and voices:
        voice_block = "GIỌNG DÙNG ĐƯỢC (dùng đúng mã bên trái):\n" + "\n".join(
            f"- {v.get('value')}: {v.get('label')}" for v in voices[:40]) + "\n\n"
    if mode != "sequential" and lanes:
        voice_block = "NHÂN VẬT DÙNG ĐƯỢC (dùng đúng mã bên trái):\n" + "\n".join(
            f"- {l.get('id')}: {l.get('name')}" for l in lanes[:20]) + "\n\n"

    return f"""{which}

CÁC KHỐI ĐANG CÓ:
{chr(10).join(lines)}

{voice_block}NGƯỜI DÙNG NHỜ:
{instruction.strip()[:MAX_INSTRUCTION_CHARS]}

THAO TÁC ĐƯỢC PHÉP:
- set_text: đổi lời của một khối, {{"op":"set_text","id":"...","value":"..."}}
- set_source: đổi phần chữ còn lại của khối đó
{timing}- delete: xoá một khối
- merge: gộp khối id với khối liền sau nó, {{"op":"merge","id":"...","with":"..."}}
- split: tách một khối làm hai, at_word là số từ giữ lại cho nửa đầu
- insert_after: thêm một khối mới ngay sau khối id, kèm trường text

QUY TẮC BẮT BUỘC:
1. Chỉ dùng đúng những id có trong danh sách trên. Không bịa id mới.
2. Chỉ sửa những gì người dùng nhờ. Khối nào không được nhắc tới thì không đụng vào.
2b. Nhưng phải làm ĐỦ mọi việc người dùng nhờ. Câu nhờ có hai vế thì trả về đủ
   thao tác cho cả hai vế. Người dùng gọi nhân vật hay giọng bằng TÊN, bạn phải
   tự tra ra mã tương ứng trong danh sách bên trên rồi dùng mã đó.
3. {text_note}
4. Viết tiếng Việt tự nhiên, giữ nguyên nghĩa, không thêm emoji vào lời thoại.
5. Nếu câu nhờ không rõ hoặc không làm được bằng các thao tác trên, trả về ops rỗng
   và nói rõ vì sao trong summary. Đừng đoán bừa.
6. summary là một câu tiếng Việt nói bạn vừa làm gì.

Trả về đúng một khối JSON: {{"ops": [...], "summary": "..."}}"""


def _parse_ops_payload(content: str) -> Tuple[Optional[Dict], str]:
    """
    Đọc JSON thao tác từ câu trả lời của model.

    Khi không phải JSON thì trả kèm luôn đoạn đầu của thứ nhận được. Nhà cung
    cấp hay trả về một câu thông báo bằng chữ thường, ví dụ model đã bị gỡ, và
    câu đó mới là lý do thật. Chỉ nói "JSON không đọc được" là giấu mất nó.
    """
    try:
        data = json.loads(_extract_json_blob(content))
    except Exception:
        snippet = " ".join((content or "").split())[:160]
        return None, (f"trả lời không phải JSON: {snippet}" if snippet
                      else "trả về nội dung rỗng")
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        return None, "trả lời thiếu danh sách thao tác"
    return data, ""


def call_9router(prompt: str, base_url: str, model: str, api_key: str,
                 timeout: int = 90) -> Tuple[Optional[Dict], str]:
    """Gọi 9Router. stream phải là false, nếu không phản hồi về dạng chuỗi sự kiện."""
    headers = {"Authorization": f"Bearer {api_key or '9router-key'}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
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
            if res.status_code == 401 and api_key in ("", None, "9router-key"):
                detail = ("Chưa điền khoá 9Router trong phần Cấu hình AI, nên tool đang gọi "
                          "bằng chuỗi giữ chỗ.")
            return None, f"9Router HTTP {res.status_code}: {detail}"
        content = _read_chat_content(res)
        if not content:
            return None, "9Router trả về nội dung rỗng"
        return _parse_ops_payload(content)
    except requests.exceptions.ConnectionError:
        return None, f"Không kết nối được 9Router tại {base_url}. Kiểm tra 9Router đã chạy chưa."
    except requests.exceptions.Timeout:
        return None, f"9Router quá thời gian chờ ({timeout}s)."
    except Exception as exc:
        return None, f"Lỗi gọi 9Router: {exc}"


def call_gemini(prompt: str, api_key: str, model: str = "gemini-2.5-flash",
                timeout: int = 90) -> Tuple[Optional[Dict], str]:
    """
    Gọi thẳng Gemini, dùng đầu ra có cấu trúc.

    thinkingBudget bằng không: Gemini 2.5 có bước suy luận nội bộ, không tắt thì
    phần đó ăn hết hạn mức token và câu trả lời bị cắt ngang giữa chừng.
    """
    if not api_key:
        return None, "Chưa cấu hình Gemini API Key trong phần Cấu hình AI."

    errors: List[str] = []
    for candidate in [model] + [m for m in GEMINI_FALLBACK_MODELS if m != model]:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{candidate}:generateContent?key={api_key}")
        generation_config = {
            "temperature": 0.3,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_OPS_SCHEMA,
            "thinkingConfig": {"thinkingBudget": 0},
        }
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        try:
            res = requests.post(url, json=payload, timeout=timeout)
            if res.status_code == 400:
                for drop in ("thinkingConfig", "responseSchema"):
                    generation_config.pop(drop, None)
                    res = requests.post(url, json=payload, timeout=timeout)
                    if res.status_code == 200:
                        break
            if res.status_code != 200:
                errors.append(f"{candidate}: HTTP {res.status_code}")
                continue
            parts = (res.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            content = "".join(p.get("text", "") for p in parts)
            if not content.strip():
                errors.append(f"{candidate}: trả về rỗng")
                continue
            data, err = _parse_ops_payload(content)
            if data:
                return data, ""
            errors.append(f"{candidate}: {err}")
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    return None, "; ".join(errors) or "Gemini không trả lời được"


def validate_ops(raw_ops: List, clips: List[Dict], mode: str,
                 lanes: List[Dict], voices: List[Dict]) -> Tuple[List[Dict], List[str]]:
    """
    Lọc danh sách thao tác AI trả về.

    Thao tác nào trỏ vào khối không có thật, thiếu trường, hay đặt giọng không
    tồn tại đều bị bỏ VÀ ghi lý do. Bỏ im lặng là kiểu hỏng khó chịu nhất: bạn
    tưởng AI đã sửa mười câu trong khi nó chỉ sửa được sáu.
    """
    by_id = {str(c.get("id")): c for c in clips}
    order = [str(c.get("id")) for c in clips]
    lane_ids = {str(l.get("id")) for l in lanes}
    voice_ids = {str(v.get("value")) for v in voices}

    ok: List[Dict] = []
    rejected: List[str] = []

    for index, item in enumerate(raw_ops):
        if not isinstance(item, dict):
            rejected.append(f"Thao tác thứ {index + 1} không đúng định dạng.")
            continue
        name = str(item.get("op") or "").strip()
        if name not in OP_FIELDS:
            rejected.append(f"Thao tác \"{name or 'trống'}\" không nằm trong danh sách cho phép.")
            continue

        clip_id = str(item.get("id") or "")
        if clip_id not in by_id:
            rejected.append(f"Thao tác {name} trỏ vào khối không có thật ({clip_id or 'trống'}).")
            continue
        clip = by_id[clip_id]
        number = clip.get("order", 0) + 1

        if name in ("set_text", "set_source"):
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                rejected.append(f"Khối {number}: {name} không có nội dung mới.")
                continue
            ok.append({"op": name, "id": clip_id, "value": value.strip(), "number": number})

        elif name == "set_time":
            if mode == "sequential":
                rejected.append(f"Khối {number}: video dựng theo cảnh không đặt được mốc thời gian rời.")
                continue
            try:
                start, end = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError):
                rejected.append(f"Khối {number}: mốc thời gian không phải số.")
                continue
            if start < 0 or end - start < 0.3:
                rejected.append(f"Khối {number}: mốc thời gian không hợp lệ ({start:.1f}s đến {end:.1f}s).")
                continue
            ok.append({"op": name, "id": clip_id, "start": start, "end": end, "number": number})

        elif name == "set_duration":
            if mode != "sequential":
                rejected.append(f"Khối {number}: câu phụ đề đặt bằng set_time chứ không phải set_duration.")
                continue
            try:
                seconds = float(item["seconds"])
            except (KeyError, TypeError, ValueError):
                rejected.append(f"Cảnh {number}: thời lượng không phải số.")
                continue
            if not 0.5 <= seconds <= 120:
                rejected.append(f"Cảnh {number}: thời lượng {seconds:.1f}s nằm ngoài khoảng cho phép.")
                continue
            ok.append({"op": name, "id": clip_id, "seconds": seconds, "number": number})

        elif name == "set_voice":
            voice = str(item.get("voice") or "")
            if mode != "sequential":
                rejected.append(f"Câu {number}: đổi giọng ở studio này làm bằng set_speaker.")
                continue
            if voice not in voice_ids:
                rejected.append(f"Cảnh {number}: không có giọng nào mã \"{voice}\".")
                continue
            ok.append({"op": name, "id": clip_id, "voice": voice, "number": number})

        elif name == "set_speaker":
            speaker = str(item.get("speaker") or "")
            if mode == "sequential":
                rejected.append(f"Cảnh {number}: đổi giọng ở studio này làm bằng set_voice.")
                continue
            if speaker not in lane_ids:
                rejected.append(f"Câu {number}: không có nhân vật nào mã \"{speaker}\".")
                continue
            ok.append({"op": name, "id": clip_id, "speaker": speaker, "number": number})

        elif name == "set_field":
            field = str(item.get("field") or "")
            value = item.get("value")
            if mode != "sequential" or field not in SCENE_FIELDS:
                rejected.append(f"Khối {number}: không sửa được trường \"{field}\" ở studio này.")
                continue
            if not isinstance(value, str):
                rejected.append(f"Cảnh {number}: giá trị của {field} không phải chữ.")
                continue
            ok.append({"op": name, "id": clip_id, "field": field, "value": value.strip(), "number": number})

        elif name == "delete":
            ok.append({"op": name, "id": clip_id, "number": number})

        elif name == "merge":
            other = str(item.get("with") or "")
            if other not in by_id:
                rejected.append(f"Khối {number}: không có khối nào mã \"{other}\" để gộp vào.")
                continue
            if order.index(other) != order.index(clip_id) + 1:
                rejected.append(f"Khối {number}: chỉ gộp được với khối đứng ngay sau nó.")
                continue
            ok.append({"op": name, "id": clip_id, "with": other, "number": number})

        elif name == "split":
            words = len((clip.get("text") or "").split())
            try:
                at_word = int(item["at_word"])
            except (KeyError, TypeError, ValueError):
                rejected.append(f"Khối {number}: chỗ tách không phải số.")
                continue
            if not 1 <= at_word < max(2, words):
                rejected.append(f"Khối {number}: chỗ tách nằm ngoài câu ({words} từ).")
                continue
            ok.append({"op": name, "id": clip_id, "at_word": at_word, "number": number})

        elif name == "insert_after":
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                rejected.append(f"Sau khối {number}: khối mới không có nội dung.")
                continue
            new_op = {"op": name, "id": clip_id, "text": text.strip(), "number": number}
            if isinstance(item.get("source"), str):
                new_op["source"] = item["source"].strip()
            try:
                new_op["seconds"] = max(0.5, min(120.0, float(item["seconds"])))
            except (KeyError, TypeError, ValueError):
                pass
            ok.append(new_op)

        elif name == "move":
            try:
                to_index = int(item["to_index"])
            except (KeyError, TypeError, ValueError):
                rejected.append(f"Khối {number}: vị trí mới không phải số.")
                continue
            if not 0 <= to_index < len(clips):
                rejected.append(f"Khối {number}: vị trí mới nằm ngoài danh sách.")
                continue
            ok.append({"op": name, "id": clip_id, "to_index": to_index, "number": number})

    return ok, rejected


def describe_op(op: Dict, mode: str, names: Optional[Dict[str, str]] = None) -> str:
    """
    Một dòng tiếng Việt cho mỗi thao tác, để bạn duyệt trước khi áp dụng.

    Giọng và nhân vật hiện bằng TÊN chứ không phải mã. "Đổi sang spk_2" thì
    không ai biết là ai, còn "đổi sang Khách mời" thì đọc phát hiểu ngay.
    """
    names = names or {}
    unit = "Cảnh" if mode == "sequential" else "Câu"
    number = op.get("number", "?")
    name = op["op"]
    if name == "set_text":
        return f"{unit} {number}: đổi lời thoại thành “{op['value'][:90]}”"
    if name == "set_source":
        label = "tiêu đề" if mode == "sequential" else "câu gốc"
        return f"{unit} {number}: đổi {label} thành “{op['value'][:90]}”"
    if name == "set_time":
        return f"{unit} {number}: đổi mốc thành {op['start']:.1f}s đến {op['end']:.1f}s"
    if name == "set_duration":
        return f"{unit} {number}: chốt thời lượng {op['seconds']:.1f} giây"
    if name == "set_voice":
        return f"{unit} {number}: đổi sang giọng {names.get(op['voice'], op['voice'])}"
    if name == "set_speaker":
        return f"{unit} {number}: đổi sang nhân vật {names.get(op['speaker'], op['speaker'])}"
    if name == "set_field":
        return f"{unit} {number}: đặt {op['field']} thành “{op['value'][:60]}”"
    if name == "delete":
        return f"{unit} {number}: xoá"
    if name == "merge":
        return f"{unit} {number}: gộp với khối ngay sau"
    if name == "split":
        return f"{unit} {number}: tách làm hai sau từ thứ {op['at_word']}"
    if name == "insert_after":
        return f"Thêm một khối mới ngay sau {unit.lower()} {number}: “{op['text'][:70]}”"
    if name == "move":
        return f"{unit} {number}: chuyển tới vị trí thứ {op['to_index'] + 1}"
    return name


def edit_timeline(instruction: str, clips: List[Dict], mode: str,
                  lanes: List[Dict], voices: List[Dict],
                  base_url: str = "http://localhost:20128/v1",
                  model: str = "combo1",
                  api_key: str = "9router-key",
                  gemini_api_key: str = "",
                  gemini_model: str = "gemini-2.5-flash") -> Dict:
    """
    Đổi một câu nhờ bằng tiếng Việt thành danh sách thao tác đã kiểm tra.

    Trả về dict: success, ops, notes, rejected, summary, source, attempts, error.
    Hỏng cả 9Router lẫn Gemini thì success=False và ops rỗng, KHÔNG bịa thao tác.
    """
    prompt = build_prompt(instruction, clips, mode, lanes, voices)
    attempts: List[str] = []

    data, err = call_9router(prompt, base_url, model, api_key)
    source = "9router"
    if not data:
        attempts.append(f"9Router: {err}")
        data, err2 = call_gemini(prompt, gemini_api_key, gemini_model)
        source = "gemini"
        if not data:
            attempts.append(f"Gemini: {err2}")
            return {"success": False, "ops": [], "notes": [], "rejected": [],
                    "summary": "", "source": "", "attempts": attempts,
                    "error": "Cả 9Router và Gemini đều không trả lời được."}

    ops, rejected = validate_ops(data.get("ops") or [], clips, mode, lanes, voices)
    names = {str(l.get("id")): str(l.get("name") or l.get("id")) for l in lanes}
    names.update({str(v.get("value")): str(v.get("label") or v.get("value")) for v in voices})
    return {
        "success": True,
        "ops": ops,
        "notes": [describe_op(op, mode, names) for op in ops],
        "rejected": rejected,
        "summary": str(data.get("summary") or "").strip(),
        "source": source,
        "attempts": attempts,
        "error": "",
    }
