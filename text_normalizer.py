"""
text_normalizer.py
──────────────────
Chuẩn hoá chữ TRƯỚC KHI đưa vào giọng đọc (TTS), để Edge TTS đọc đúng số, đơn vị,
phần trăm và tiền, thay vì đọc sai kiểu "5kg" thành "năm k g".

Nguyên tắc: chỉ dùng cho phần TIẾNG. Phụ đề hiển thị vẫn giữ nguyên "5kg", "30%",
để người đọc thấy con số gọn. Vì thời lượng được đo trên chính tiếng đã chuẩn hoá,
khâu đồng bộ cũng đo đúng hơn (đọc "ba mươi phần trăm" dài hơn nhìn "30%").

Chỉ đụng những trường hợp chắc chắn; chỗ nào không chắc thì để nguyên, không bao
giờ làm chữ tệ hơn. Chạy lại trên chữ đã chuẩn hoá cũng ra kết quả cũ (không còn
chữ số để kích hoạt luật), nên an toàn nếu lỡ gọi hai lần.
"""

from __future__ import annotations

import re

_ONES = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
_SCALES = ["", "nghìn", "triệu", "tỷ", "nghìn tỷ", "triệu tỷ"]


def _read_three(n: int, full: bool) -> str:
    """Đọc một cụm ba chữ số (0..999). full=True khi có cụm bậc cao hơn phía trước."""
    tram, chuc, donvi = n // 100, (n % 100) // 10, n % 10
    parts = []
    if tram > 0 or full:
        parts.append(f"{_ONES[tram]} trăm")
    if chuc == 0:
        if donvi > 0 and (tram > 0 or full):
            parts.append(f"linh {_ONES[donvi]}")
        elif donvi > 0:
            parts.append(_ONES[donvi])
    elif chuc == 1:
        parts.append("mười")
        if donvi == 5:
            parts.append("lăm")
        elif donvi > 0:
            parts.append(_ONES[donvi])
    else:
        parts.append(f"{_ONES[chuc]} mươi")
        if donvi == 1:
            parts.append("mốt")
        elif donvi == 5:
            parts.append("lăm")
        elif donvi > 0:
            parts.append(_ONES[donvi])
    return " ".join(parts).strip()


def read_int(n: int) -> str:
    """Đọc số nguyên thành chữ tiếng Việt (hỗ trợ tới hàng nghìn tỷ)."""
    if n < 0:
        return "âm " + read_int(-n)
    if n == 0:
        return "không"
    groups = []
    while n > 0:
        groups.append(n % 1000)
        n //= 1000
    if len(groups) > len(_SCALES):
        return None  # quá lớn, để nguyên chữ số
    parts = []
    top = len(groups) - 1
    for i in range(top, -1, -1):
        if groups[i] == 0:
            continue
        chunk = _read_three(groups[i], full=(i != top))
        parts.append(f"{chunk} {_SCALES[i]}".strip())
    return " ".join(parts).strip()


def _read_digits(s: str) -> str:
    return " ".join(_ONES[int(d)] for d in s if d.isdigit())


def read_number(token: str) -> str:
    """
    Đọc một cụm số có thể có dấu phân cách. Quy ước tiếng Việt: dấu chấm ngăn hàng
    nghìn, dấu phẩy là thập phân. Không chắc thì trả lại nguyên cụm.
    """
    s = token.strip()
    if not re.fullmatch(r"[\d.,]+", s):
        return token
    if "," in s:
        intp, dec = s.split(",", 1)
        whole = read_int(int(intp.replace(".", "") or "0"))
        if whole is None:
            return token
        return f"{whole} phẩy {_read_digits(dec)}".strip()
    # Một dấu chấm với phần đuôi khác 3 chữ số là thập phân; còn lại coi là ngăn nghìn.
    if s.count(".") == 1 and len(s.split(".")[1]) != 3:
        intp, dec = s.split(".", 1)
        whole = read_int(int(intp or "0"))
        if whole is None:
            return token
        return f"{whole} phẩy {_read_digits(dec)}".strip()
    digits = s.replace(".", "")
    if not digits.isdigit():
        return token
    whole = read_int(int(digits))
    return whole if whole is not None else token


# Đơn vị đọc thành chữ, xếp DÀI TRƯỚC để "km" không bị "m" nuốt mất.
_UNITS = [
    ("kg", "ki lô gam"), ("mg", "mi li gam"), ("km", "ki lô mét"),
    ("cm", "xen ti mét"), ("mm", "mi li mét"), ("ml", "mi li lít"),
    ("g", "gam"), ("m", "mét"), ("l", "lít"),
]
_UNIT_ALT = "|".join(re.escape(u) for u, _ in _UNITS)
# Số nguyên hoặc thập phân, KHÔNG kết thúc bằng dấu phân cách (tránh nuốt dấu câu
# cuối như trong "$10." hay "còn 5.").
_NUM = r"\d(?:[.,]?\d)*"


def normalize_for_speech(text: str, lang: str = "vi") -> str:
    """
    Chuẩn hoá chữ cho giọng đọc tiếng Việt. Ngôn ngữ khác trả lại nguyên văn
    (Edge TTS bản ngữ đọc số của tiếng đó ổn hơn cách phiên âm tay).
    """
    if not text or not text.strip():
        return text
    if lang and not str(lang).lower().startswith("vi"):
        return text

    s = text
    num_word = lambda m: read_number(m.group(1))

    # Phần trăm
    s = re.sub(rf"({_NUM})\s*%", lambda m: f"{read_number(m.group(1))} phần trăm", s)
    # Nhiệt độ
    s = re.sub(rf"({_NUM})\s*°\s*[cC]", lambda m: f"{read_number(m.group(1))} độ xê", s)
    s = re.sub(rf"({_NUM})\s*°", lambda m: f"{read_number(m.group(1))} độ", s)
    # Tiền: ký hiệu $ trước hoặc sau số, và đồng ₫ / đ sau số
    s = re.sub(rf"\$\s*({_NUM})", lambda m: f"{read_number(m.group(1))} đô la", s)
    s = re.sub(rf"({_NUM})\s*\$", lambda m: f"{read_number(m.group(1))} đô la", s)
    s = re.sub(rf"({_NUM})\s*(?:₫|đ)\b", lambda m: f"{read_number(m.group(1))} đồng", s)
    # Hậu tố nghìn / triệu quen dùng khi ghi giá: 100k, 5tr
    s = re.sub(rf"({_NUM})\s*tr\b", lambda m: f"{read_number(m.group(1))} triệu", s)
    s = re.sub(rf"({_NUM})\s*k\b", lambda m: f"{read_number(m.group(1))} nghìn", s)
    # Đơn vị đo dính ngay sau số
    s = re.sub(rf"({_NUM})\s*({_UNIT_ALT})\b",
               lambda m: f"{read_number(m.group(1))} {dict(_UNITS)[m.group(2)]}", s)
    # Số đứng một mình còn lại (không dính chữ cái hai bên; không nuốt dấu câu cuối)
    s = re.sub(r"(?<![^\W\d_])(\d[\d.,]*\d|\d)(?![^\W\d_])", num_word, s)

    # Vài ký hiệu đọc ra lời
    s = re.sub(r"\s*&\s*", " và ", s)
    return re.sub(r"\s{2,}", " ", s).strip()
