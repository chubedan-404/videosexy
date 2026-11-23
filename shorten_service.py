"""
shorten_service.py
──────────────────
Gọi AI viết gọn những câu dịch bị dài hơn khung thời gian của chúng.

Dùng đúng thứ tự ưu tiên của cả dự án: 9Router trước, hỏng thì Gemini 2.5.
Hợp đồng dữ liệu theo mục 6.3 của KE_HOACH_DONG_BO_VOICE_PHU_DE.md: model chỉ
được trả về văn bản mới theo đúng cue_id được giao, tuyệt đối không tự đổi mốc
thời gian, không bỏ con số, tên riêng hay từ phủ định.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

import requests

# Mỗi lô là một lượt chờ mạng, các lô không liên quan gì đến nhau. Gửi cùng lúc
# thì tổng thời gian bằng lô chậm nhất thay vì cộng dồn tất cả các lô.
BATCH_LANES = 4

SYSTEM_PROMPT = (
    "Bạn là biên tập viên phụ đề tiếng Việt. Nhiệm vụ duy nhất của bạn là viết lại "
    "câu cho NGẮN HƠN mà vẫn giữ nguyên ý. "
    "Luôn trả lời bằng DUY NHẤT một JSON object hợp lệ, không kèm lời dẫn, không markdown."
)

RULES = (
    "QUY TẮC BẮT BUỘC:\n"
    "1. Giữ nguyên toàn bộ con số, đơn vị, tên riêng, từ phủ định (không, chưa, đừng, chẳng) "
    "và mọi mệnh đề điều kiện. Mất một chữ 'không' là câu đảo ngược nghĩa.\n"
    "2. Chỉ được cắt từ đệm, từ lặp, cách diễn đạt vòng vo. Không được bỏ thông tin.\n"
    "3. Giữ đúng văn nói tự nhiên, vì câu này sẽ được đọc thành tiếng.\n"
    "4. Không thêm nội dung mới. Không dịch sang ngôn ngữ khác.\n"
    "5. Nếu không thể rút ngắn mà vẫn giữ đủ ý, trả về đúng chuỗi \"needs_review\" cho câu đó.\n"
    "6. Chỉ trả về JSON dạng {\"cue_id\": \"câu đã rút gọn\", ...} theo đúng các cue_id được giao."
)


def _build_prompt(requests_list: List[Dict]) -> str:
    lines = [RULES, "", "DANH SÁCH CÂU CẦN RÚT GỌN:"]
    for r in requests_list:
        lines.append(
            f"- cue_id: {r['cue_id']}\n"
            f"  câu hiện tại: {r['current_text']}\n"
            f"  đọc mất: {r.get('measured_ms') or r.get('estimated_ms')} ms, "
            f"chỉ có: {r['budget_ms']} ms\n"
            f"  cần rút còn khoảng: {r.get('target_syllables')} chữ"
        )
    return "\n".join(lines)


def _extract_json_object(content: str) -> Dict:
    text = (content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_results(raw: Dict, requests_list: List[Dict]) -> Dict[str, str]:
    """
    Chỉ nhận kết quả hợp lệ: đúng cue_id được giao, có chữ, và thật sự ngắn hơn.

    Model trả về cue_id lạ hoặc câu dài hơn cũ đều bị loại, vì nhận vào là làm
    hỏng đúng thứ đang cố sửa.
    """
    wanted = {r["cue_id"]: r["current_text"] for r in requests_list}
    out: Dict[str, str] = {}
    for cue_id, value in (raw or {}).items():
        if cue_id not in wanted or not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text.lower() == "needs_review":
            continue
        if len(text.split()) >= len(wanted[cue_id].split()):
            continue
        out[cue_id] = text
    return out


def _call_9router(prompt: str, cfg: Dict, timeout: int = 90) -> Dict:
    base_url = (cfg.get("openai_base_url") or "http://localhost:20128/v1").rstrip("/")
    api_key = cfg.get("openai_api_key") or "9router-key"
    body = {
        "model": cfg.get("openai_model") or "combo1",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 2000,
        "stream": False,
    }
    res = requests.post(f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}",
                                 "Content-Type": "application/json"},
                        json=body, timeout=timeout)
    if res.status_code != 200:
        raise RuntimeError(f"9Router HTTP {res.status_code}: {res.text[:120]}")
    try:
        content = res.json()["choices"][0]["message"]["content"]
    except Exception:
        chunks = []
        for line in res.text.splitlines():
            line = line.strip()
            if line.startswith("data:") and not line.endswith("[DONE]"):
                try:
                    piece = json.loads(line[5:].strip())["choices"][0].get("delta", {}).get("content")
                    if piece:
                        chunks.append(piece)
                except Exception:
                    continue
        content = "".join(chunks)
    return _extract_json_object(content)


def _call_gemini(prompt: str, cfg: Dict, timeout: int = 60) -> Dict:
    key = cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Chưa cấu hình Gemini API Key")
    model = cfg.get("model") or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096,
                             "responseMimeType": "application/json",
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    res = requests.post(url, json=payload, timeout=timeout)
    if res.status_code == 400:
        payload["generationConfig"].pop("thinkingConfig", None)
        res = requests.post(url, json=payload, timeout=timeout)
    if res.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {res.status_code}: {res.text[:120]}")
    parts = ((res.json().get("candidates") or [{}])[0].get("content", {}) or {}).get("parts") or []
    return _extract_json_object("".join(p.get("text", "") for p in parts))


def make_shortener(load_config, progress=None, batch_size: int = 12):
    """
    Trả về hàm rewrite_fn dùng cho sync_engine.synthesize_anchored.

    Gộp nhiều câu vào một lần gọi để đỡ tốn lượt, nhưng chia lô để tránh prompt
    quá dài làm model trả về thiếu.
    """
    def say(msg):
        if progress:
            progress(msg)
        print(f"[✂️ Viết gọn] {msg}")

    def rewrite(requests_list: List[Dict]) -> Dict[str, str]:
        if not requests_list:
            return {}
        cfg = load_config() or {}
        merged: Dict[str, str] = {}
        batches = [requests_list[i:i + batch_size]
                   for i in range(0, len(requests_list), batch_size)]

        def _one_batch(index_batch):
            index, batch = index_batch
            prompt = _build_prompt(batch)
            raw, source, err = {}, "", ""
            try:
                raw = _call_9router(prompt, cfg)
                source = "9Router"
            except Exception as exc:
                err = str(exc)
                try:
                    raw = _call_gemini(prompt, cfg)
                    source = "Gemini"
                except Exception as exc2:
                    say(f"Không rút gọn được lô {index + 1}: 9Router {err} | Gemini {exc2}")
                    return {}
            cleaned = _clean_results(raw, batch)
            say(f"{source} rút gọn được {len(cleaned)}/{len(batch)} câu")
            return cleaned

        if len(batches) > 1 and BATCH_LANES > 1:
            with ThreadPoolExecutor(max_workers=min(BATCH_LANES, len(batches))) as pool:
                for cleaned in pool.map(_one_batch, enumerate(batches)):
                    merged.update(cleaned)
        else:
            for pair in enumerate(batches):
                merged.update(_one_batch(pair))

        return merged

    return rewrite
