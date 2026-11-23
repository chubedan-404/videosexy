"""Context-aware translation through the OpenAI API (ChatGPT)."""
import os
import json
import re
import requests
from typing import List, Dict, Optional


class OpenAITranslator:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None):
        from gemini_translator import load_gemini_config
        cfg = load_gemini_config()
        self.api_key = (api_key or cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()

        m = (model or cfg.get("openai_model") or "gpt-4o-mini").strip()
        raw_base = (base_url or cfg.get("openai_base_url") or "").strip()

        # Tự động nhận diện 9Router:
        # Nếu có API Key chính chủ của OpenAI (sk-...) VÀ dùng model chuẩn OpenAI thì mới gọi api.openai.com.
        # Ngược lại, trong mọi trường hợp (dùng combo, 9router, tên model tùy chỉnh như qui-project),
        # luôn tự động điều hướng sang 9Router local: http://localhost:20128/v1
        is_standard_openai = any(m.startswith(p) for p in ("gpt-4", "gpt-3.5", "o1", "o3", "text-"))
        is_official_key = self.api_key.startswith("sk-") and len(self.api_key) > 20

        if not raw_base:
            if is_official_key and is_standard_openai:
                raw_base = "https://api.openai.com/v1"
            else:
                raw_base = "http://localhost:20128/v1"

        raw_base = raw_base.rstrip("/")
        # Đảm bảo endpoint kết thúc bằng /v1 nếu dùng localhost 9router
        if any(h in raw_base for h in ("20128", "localhost", "127.0.0.1")) and not raw_base.endswith("/v1"):
            raw_base = f"{raw_base}/v1"

        self.base_url = raw_base
        self.model = "gpt-4o-mini" if m in ("gpt-5-mini", "", None) else m
        self.last_error = ""

    def is_configured(self) -> bool:
        if any(h in self.base_url for h in ("localhost", "127.0.0.1", "0.0.0.0", "20128")):
            return True
        return len(self.api_key) > 10

    def translate_script(
        self,
        subtitles: List[Dict],
        video_title: str = "",
        target_lang: str = "vi",
        style: str = "natural"
    ) -> Optional[List[str]]:
        self.last_error = ""
        if not self.is_configured():
            self.last_error = "Chưa nhập OpenAI API Key và chưa kích hoạt 9Router local (http://localhost:20128/v1)."
            return None
        if not subtitles:
            return None

        payload = [
            {
                "id": i,
                "start": s.get("start", ""),
                "end": s.get("end", ""),
                "text": s.get("source_text", ""),
                "previous": subtitles[i - 1].get("source_text", "") if i > 0 else "",
                "next": subtitles[i + 1].get("source_text", "") if i + 1 < len(subtitles) else ""
            }
            for i, s in enumerate(subtitles)
        ]

        from gemini_translator import build_localization_system_prompt
        system_instruction = build_localization_system_prompt(
            target_lang=target_lang,
            style=style,
            video_title=video_title
        )

        user_prompt = (
            f"Tiêu đề video: {video_title}\n"
            f"Dữ liệu kịch bản:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        headers = {
            "Authorization": f"Bearer {self.api_key or '9router-key'}",
            "Content-Type": "application/json"
        }

        # 1. Thử gọi qua endpoint chuẩn quốc tế v1/chat/completions (OpenAI / 9Router / OpenRouter)
        try:
            req_body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.65,
                "stream": False
            }
            r = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=req_body,
                timeout=120
            )

            # Một số model qua 9Router không hỗ trợ response_format, thử lại không có trường này
            if r.status_code == 400 and "response_format" in r.text:
                del req_body["response_format"]
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=req_body,
                    timeout=120
                )

            if r.status_code == 200:
                content = ""
                try:
                    data = r.json()
                    content = data["choices"][0]["message"]["content"].strip()
                except Exception:
                    # Nếu proxy trả về dạng Server-Sent Events (SSE chunk)
                    chunks = []
                    for line in r.text.splitlines():
                        line = line.strip()
                        if line.startswith("data:") and not line.endswith("[DONE]"):
                            try:
                                cj = json.loads(line[5:].strip())
                                delta = cj.get("choices", [{}])[0].get("delta", {})
                                c_part = delta.get("content") or ""
                                if c_part:
                                    chunks.append(c_part)
                            except Exception:
                                pass
                    content = "".join(chunks).strip()

                if "```json" in content:
                    content = content.split("```json", 1)[1].split("```", 1)[0].strip()
                elif "```" in content:
                    content = content.split("```", 1)[1].split("```", 1)[0].strip()

                parsed = json.loads(content)
                items = parsed.get("translations") if isinstance(parsed, dict) else parsed
                if not items and isinstance(parsed, dict):
                    # fallback tìm mảng bất kỳ trong json
                    for v in parsed.values():
                        if isinstance(v, list):
                            items = v
                            break

                if items and isinstance(items, list):
                    mapped = {int(x["id"]): str(x.get("translated_text", "")) for x in items if isinstance(x, dict) and "id" in x}
                    if len(mapped) == len(subtitles):
                        return [mapped.get(i, "") for i in range(len(subtitles))]
                self.last_error = "Mô hình trả về định dạng kịch bản không khớp."

            elif r.status_code == 429:
                err_detail = "Rate limit / Hết hạn mức credit"
                try:
                    err_detail = r.json().get("error", {}).get("message", err_detail)
                except Exception:
                    pass
                self.last_error = f"Hết credit hoặc bị giới hạn lượt gọi (429): {err_detail}"
                print(f"[❌ OpenAI / 9Router 429] {self.last_error}")
                return None
            else:
                err_detail = r.text[:200]
                try:
                    ej = r.json()
                    err_detail = ej.get("error", {}).get("message") or ej.get("message") or ej.get("detail") or err_detail
                except Exception:
                    pass
                self.last_error = f"Lỗi từ 9Router/AI ({r.status_code}): {err_detail}"
                print(f"[⚠️ OpenAI / 9Router Chat Error {r.status_code}] {self.last_error}")

        except requests.exceptions.Timeout:
            self.last_error = (
                "Quá thời gian phản hồi (Read timeout > 120s): 9Router hoặc model AI phản hồi quá lâu. "
                "Gợi ý: Kiểm tra lại combo trong 9Router Dashboard (http://localhost:20128/dashboard), "
                "tránh để model 'thinking' nặng (như Claude Opus Thinking) ở đầu combo; "
                "nên ưu tiên Gemini Flash hoặc GPT-5.4-mini / GPT-4o-mini."
            )
            print(f"[⚠️ OpenAI / 9Router Timeout] {self.last_error}")
        except Exception as e:
            self.last_error = f"Lỗi kết nối AI: {e}"
            print(f"[⚠️ OpenAI / 9Router Chat Exception] {e}")

        # 2. Fallback thử endpoint v1/responses nếu là chính chủ OpenAI
        if "api.openai.com" in self.base_url:
            try:
                resp_prompt = system_instruction + "\n" + user_prompt
                r2 = requests.post(
                    f"{self.base_url}/responses",
                    headers=headers,
                    json={"model": self.model, "input": resp_prompt},
                    timeout=45
                )
                if r2.status_code == 200:
                    data = r2.json()
                    text = data.get("output_text", "").strip()
                    match = re.search(r"\[.*\]", text, re.S)
                    if match:
                        parsed = json.loads(match.group(0))
                        mapped = {int(x["id"]): str(x.get("translated_text", "")) for x in parsed if isinstance(x, dict) and "id" in x}
                        return [mapped.get(i, "") for i in range(len(subtitles))]
                elif r2.status_code == 429:
                    self.last_error = "Tài khoản OpenAI chính chủ đã hết credit."
                    print("[❌ OpenAI 429] Tài khoản OpenAI đã hết credit.")
            except Exception as e2:
                print(f"[⚠️ OpenAI Responses API Error] {e2}")

        return None
