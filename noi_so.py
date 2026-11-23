"""
noi_so.py
─────────
Nối sổ dấu vân tay với số liệu thật trên YouTube.

Vì sao cần. Tool đã ghi sổ mỗi video dựng xong dùng giọng gì, kiểu hình gì, mở
đầu bao nhiêu chữ, ảnh bìa chữ gì màu gì. Đọc sổ ngày 21/09/2026 thấy 11 dòng,
phần "đã làm gì" ghi khá đầy đủ.

Nhưng KHÔNG dòng nào có mã video YouTube. Sổ ghi nguyên nhân mà không ghi kết
quả, nên không có cách nào nối "video này làm kiểu A" với "video này giữ chân
được bao lâu". Thiếu cái nối đó thì mọi thứ tool học được đều là bịa.

File này nối lại. Nó KHÔNG rút ra kết luận gì: với bảy video và lượt xem từ 4
tới 57, mọi quy luật tìm được đều là nhiễu. Việc của nó là dựng sẵn bộ dữ liệu
để khi kênh đủ lớn thì đã có sẵn mấy chục dòng đối chiếu, thay vì lúc ấy mới
bắt đầu từ số không.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

import channel_profile as cp


def _khong_dau(chuoi: str) -> str:
    """Bỏ dấu tiếng Việt để so tên không phụ thuộc cách gõ."""
    import unicodedata
    tach = unicodedata.normalize("NFD", chuoi or "")
    return "".join(c for c in tach if unicodedata.category(c) != "Mn").replace("đ", "d").replace("Đ", "D")


def khoa_ten(tieu_de: str) -> str:
    """
    Rút tên nhân vật khỏi tiêu đề để làm khoá so.

    KHÔNG so nguyên tiêu đề, vì người dùng thường sửa lại tiêu đề trước khi
    đăng. Đo thật: sổ ghi "KHUFU - VỊ PHARAOH ĐỂ LẠI CÔNG TRÌNH KHỔNG LỒ NHƯNG
    RẤT ÍT DẤU VẾT VỀ CHÍNH MÌNH", còn trên YouTube là "KHUFU - XÂY KỲ QUAN
    NHƯNG ÍT DẤU VẾT". So nguyên tiêu đề thì trượt, so tên nhân vật thì khớp.
    """
    t = _khong_dau(tieu_de or "").upper()
    # Bỏ nhãn cố định của kênh ở đầu.
    t = re.sub(r"^\s*TOM TAT CUOC DOI\s*", "", t)
    # Tên nằm trước dấu gạch ngang đầu tiên.
    t = re.split(r"\s+[-–—]\s+", t)[0]
    t = re.sub(r"#\w+", "", t)
    t = re.sub(r"[^A-Z0-9\s]", " ", t)
    return " ".join(t.split())


def ghep(so: List[Dict], videos: List[Dict]) -> Dict:
    """
    Ghép từng dòng sổ với một video trên kênh.

    Chỉ ghép khi khoá tên trùng khớp hoàn toàn. Gần đúng thì bỏ qua: ghép nhầm
    một dòng là dạy tool một điều sai, tệ hơn hẳn so với bỏ trống một dòng.
    """
    theo_khoa: Dict[str, List[Dict]] = {}
    for v in videos:
        k = khoa_ten(v.get("title") or "")
        if k:
            theo_khoa.setdefault(k, []).append(v)

    ghep_duoc, truot = [], []
    da_dung = set()
    for dong in so:
        k = khoa_ten(dong.get("title") or dong.get("name") or "")
        ung = [v for v in theo_khoa.get(k, []) if v.get("id") not in da_dung]
        if not k or not ung:
            truot.append({**dong, "khoa": k})
            continue
        # Nhiều video cùng tên nhân vật thì lấy cái đăng gần lượt dựng nhất.
        v = min(ung, key=lambda x: abs(_gio(x.get("at")) - (dong.get("at") or 0)))
        da_dung.add(v["id"])
        ghep_duoc.append({**dong, "video_id": v["id"], "video_title": v.get("title"),
                          "views": v.get("views", 0),
                          "avg_seconds": v.get("avg_seconds"),
                          "avg_percent": v.get("avg_percent"),
                          "url": v.get("url", "")})
    return {"ghep": ghep_duoc, "truot": truot,
            "chua_ghep_video": [v for v in videos if v.get("id") not in da_dung]}


def _gio(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def khoa_dong(dong: Dict) -> str:
    """
    Khoá duy nhất của một dòng sổ.

    KHÔNG dùng mã dự án. Đọc sổ thật ngày 21/09/2026: chín video khác nhau cùng
    mang mã dự án `87cbaadfa822`, vì người dùng dùng lại một dự án cho mọi
    video thay vì tạo dự án mới. Khoá theo dự án thì cả chín dòng nhận chung
    một mã video, và sổ hỏng sạch.

    Tên file thành phẩm thì mỗi lượt dựng một cái, vì nó mang mốc thời gian.
    """
    return str(dong.get("output") or "") or f"at:{dong.get('at') or 0}"


def ghi_lai(ghep_duoc: List[Dict]) -> int:
    """
    Ghi mã video ngược vào sổ, để lần sau khỏi phải dò lại.

    Viết lại cả file thay vì nối thêm dòng: sổ này mỗi video một dòng, nối thêm
    là có hai dòng cho cùng một video và mọi phép đếm sau đó đều sai.
    """
    duong = cp.FINGERPRINT_FILE
    ma_theo_khoa = {khoa_dong(d): d.get("video_id") for d in ghep_duoc
                    if d.get("video_id") and khoa_dong(d)}
    if not ma_theo_khoa:
        return 0
    try:
        with open(duong, encoding="utf-8") as fh:
            dong = [l for l in fh.read().splitlines() if l.strip()]
    except OSError:
        return 0
    moi, so = [], 0
    for l in dong:
        try:
            d = json.loads(l)
        except json.JSONDecodeError:
            moi.append(l)
            continue
        ma = ma_theo_khoa.get(khoa_dong(d))
        if ma and d.get("video_id") != ma:
            d["video_id"] = ma
            so += 1
        moi.append(json.dumps(d, ensure_ascii=False))
    tam = duong + ".tam"
    try:
        os.makedirs(os.path.dirname(duong), exist_ok=True)
        with open(tam, "w", encoding="utf-8") as fh:
            fh.write("\n".join(moi) + "\n")
        os.replace(tam, duong)
    except OSError:
        return 0
    return so


# Dưới mức này thì mọi so sánh đều là nhiễu. Bảy video với lượt xem 4 tới 57 đo
# giữ chân trên bốn người, tìm ra quy luật gì cũng là tự lừa mình.
DU_VIDEO = 10
DU_LUOT_MOI_VIDEO = 100


def du_de_ket_luan(ghep_duoc: List[Dict]) -> Dict:
    """Đã đủ dữ liệu để rút ra điều gì chưa, và còn thiếu bao nhiêu."""
    co_so = [d for d in ghep_duoc if (d.get("avg_percent") or 0) > 0]
    du_luot = [d for d in co_so if (d.get("views") or 0) >= DU_LUOT_MOI_VIDEO]
    xong = len(du_luot) >= DU_VIDEO
    return {
        "xong": xong,
        "co_so_lieu": len(co_so),
        "du_luot": len(du_luot),
        "can_them": max(0, DU_VIDEO - len(du_luot)),
        "cau": (f"Đã đủ {len(du_luot)} video để bắt đầu đối chiếu."
                if xong else
                f"Mới có {len(du_luot)} video đạt từ {DU_LUOT_MOI_VIDEO} lượt xem trở lên, "
                f"cần {DU_VIDEO}. Chưa đủ thì mọi quy luật tìm ra đều là nhiễu, "
                f"nên tool chưa kết luận gì."),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ĐỀ TÀI NÀO ĐÃ LÀM RỒI
# ─────────────────────────────────────────────────────────────────────────────
#
# Ô gợi ý đề tài có gần bốn chục nhân vật, và sau vài tuần thì người làm kênh
# không còn nhớ nổi đã làm ai chưa. Nhớ nhầm thì hoặc làm lại một nhân vật đã
# có, hoặc bỏ qua một nhân vật tưởng là làm rồi.
#
# Hai trạng thái khác nhau và không gộp được:
#   ĐÃ DỰNG  video đã render xong, nằm trong sổ dấu vân tay, có thể chưa đăng.
#   ĐÃ ĐĂNG  video đã lên YouTube.
#
# Phân biệt vì chúng dẫn tới hai việc khác nhau: đã dựng mà chưa đăng thì việc
# cần làm là đi đăng, chứ không phải làm lại từ đầu. Đọc sổ ngày 21/09/2026
# thấy đúng một video như vậy, là Lã Bất Vi.

DA_DUNG = "da_dung"
DA_DANG = "da_dang"


def da_lam(videos: Optional[List[Dict]] = None) -> Dict[str, Dict]:
    """
    Khoá tên của mọi đề tài đã làm, kèm trạng thái.

    Trả về {khoa: {"trang_thai", "tieu_de", "url", "views", "at"}}.

    `videos` là danh sách video trên YouTube; để trống thì chỉ đọc sổ. Tách ra
    để phần này chạy được cả khi chưa nối kênh hoặc mạng hỏng, vì sổ nằm ngay
    trên máy còn YouTube thì không phải lúc nào cũng gọi được.
    """
    ra: Dict[str, Dict] = {}
    for d in cp.read_fingerprints():
        k = khoa_ten(d.get("title") or d.get("name") or "")
        if not k:
            continue
        # Dựng lại nhiều lần thì giữ bản mới nhất.
        if k not in ra or (d.get("at") or 0) > (ra[k].get("at") or 0):
            ra[k] = {"trang_thai": DA_DUNG, "tieu_de": d.get("title") or "",
                     "url": "", "views": None, "at": d.get("at") or 0}
    for v in (videos or []):
        k = khoa_ten(v.get("title") or "")
        if not k:
            continue
        # Đã đăng đè lên đã dựng: đăng rồi là trạng thái xa hơn.
        ra[k] = {"trang_thai": DA_DANG, "tieu_de": v.get("title") or "",
                 "url": v.get("url") or "", "views": v.get("views", 0),
                 "at": ra.get(k, {}).get("at") or 0}
    return ra
