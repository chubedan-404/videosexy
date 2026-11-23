"""
Video song đấu: hai nhân vật (hoặc hai đội quân) đứng hai bên màn hình, so từng chỉ
số bằng thanh ngang, bảng tỉ số ở trên, cuối phim chốt ai hơn. Xưởng riêng, phiên
riêng, không dùng chung trạng thái với xưởng nào khác.

Một lượt làm phim có hai bước, và người dùng đứng giữa hai bước:
1. AI lập bảng so sánh: các chỉ số ĐẾM ĐƯỢC, con số của mỗi bên, nguồn, lời đọc.
   Người dùng đọc và sửa bảng. Con số sai sẽ nằm to giữa màn hình, nên không có
   đường nào đi thẳng từ hai cái tên ra phim mà bỏ qua bước duyệt này.
2. Dựng: vẽ hai chân dung, đọc lời từng vòng, Remotion vẽ thanh, số và tỉ số.

Phiên lưu ở downloads/song_dau/phien.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable, Dict, List, Optional

import longform_studio as lf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "song_dau")
PHIEN = os.path.join(KHO, "phien.json")

FPS = 30
SO_VONG = (3, 8)
NGHI_SAU_LOI = 24      # khung hình đứng yên sau khi đọc xong một cảnh
VONG_TOI_THIEU = 150   # một vòng ít nhất 5 giây: thanh chạy xong ở khung 80, lộ bên thắng ở khung 92

CHAN_DUNG = (
    "Semi-realistic digital painting portrait, chest-up, facing the viewer, dramatic warm rim light, dark "
    "muted background, historically accurate clothing, armour and hairstyle for the era, serious "
    "expression, no text, no letters, no watermark. Subject: ")

_BANG_HE = (
    "Bạn là biên tập viên lịch sử lập bảng so sánh hai nhân vật (hoặc hai đội quân, hai triều đại) cho "
    "một video đối đầu. Chỉ chọn chỉ số ĐẾM ĐƯỢC và có ghi trong sử liệu: tuổi thọ, số năm cầm quyền, "
    "quân số một trận cụ thể, số chiến dịch, diện tích, số thành chiếm được. KHÔNG chấm điểm chủ quan "
    "(tài năng, mưu lược, uy tín). Con số sử liệu chép khác nhau thì lấy con số phổ biến nhất và ghi "
    "'khoảng' vào hien_a, hien_b. Không chắc một con số thì bỏ hẳn chỉ số đó, đừng đoán.\n"
    "Mỗi vòng: chi_so (tên ngắn, dưới 30 ký tự), don_vi, a và b là SỐ thuần (không chữ), hien_a và "
    "hien_b là chữ hiện trên màn hình (ví dụ 'khoảng 100.000'), thang là 'a', 'b' hoặc 'hoa' (bên có "
    "lợi thế ở chỉ số đó, không nhất thiết là số lớn hơn), nguon (tên sách sử hoặc tài liệu, ngắn), "
    "loi là lời đọc 35 tới 55 từ tiếng Việt giải thích hai con số, câu ngắn, số viết bằng chữ.\n"
    "mo_dau: lời mở 25 tới 40 từ, nêu câu hỏi ai hơn ai. ket: lời kết 30 tới 45 từ, KHÔNG nêu tỉ số "
    "(tool tự đếm), nhận xét chung rồi hỏi người xem chọn ai. chan_dung_a, chan_dung_b: tả ngoại hình "
    "bằng tiếng Anh để vẽ chân dung, đúng trang phục thời đại.\n"
    'JSON: {"title":"...","chan_dung_a":"...","chan_dung_b":"...","mo_dau":"...","ket":"...",'
    '"vong":[{"chi_so":"...","don_vi":"...","a":0,"b":0,"hien_a":"...","hien_b":"...","thang":"a",'
    '"nguon":"...","loi":"..."}]}')


# ─── Phiên ────────────────────────────────────────────────────────────────
def doc_phien() -> Dict:
    try:
        with open(PHIEN, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def luu_phien(d: Dict) -> None:
    os.makedirs(KHO, exist_ok=True)
    tam = PHIEN + ".tmp"
    with open(tam, "w", encoding="utf-8") as fh:
        json.dump({**d, "at": time.time()}, fh, ensure_ascii=False)
    os.replace(tam, PHIEN)


# ─── Bảng so sánh ────────────────────────────────────────────────────────
def _so(x) -> Optional[float]:
    """Đổi về số. AI hay trả '100.000' hoặc '1,5 triệu' dù đã dặn; chữ không đổi được thì bỏ vòng đó."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    chuoi = str(x or "").strip().replace(" ", "")
    if not chuoi:
        return None
    # "100.000" là một trăm nghìn kiểu Việt, "1.5" là số thập phân: phân biệt bằng nhóm ba chữ số.
    import re
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", chuoi):
        chuoi = re.sub(r"[.,]", "", chuoi)
    try:
        return float(chuoi.replace(",", "."))
    except ValueError:
        return None


def chuan_bang(tho: Dict, ten_a: str, ten_b: str) -> Dict:
    """Lọc bảng AI trả (hoặc bảng người dùng vừa sửa) về đúng khuôn; vòng thiếu số thì bỏ."""
    vong: List[Dict] = []
    for v in (tho.get("vong") or [])[:SO_VONG[1]]:
        if not isinstance(v, dict):
            continue
        a, b = _so(v.get("a")), _so(v.get("b"))
        chi_so = str(v.get("chi_so") or "").strip()[:40]
        if a is None or b is None or not chi_so:
            continue
        thang = str(v.get("thang") or "").strip().lower()
        if thang not in ("a", "b", "hoa"):
            thang = "a" if a > b else ("b" if b > a else "hoa")
        vong.append({"chi_so": chi_so, "don_vi": str(v.get("don_vi") or "").strip()[:24], "a": a, "b": b,
                     "hien_a": str(v.get("hien_a") or "").strip()[:22],
                     "hien_b": str(v.get("hien_b") or "").strip()[:22],
                     "thang": thang, "nguon": str(v.get("nguon") or "").strip()[:60],
                     "loi": " ".join(str(v.get("loi") or "").split())[:600]})
    return {"ten_a": ten_a.strip()[:40], "ten_b": ten_b.strip()[:40],
            "title": str(tho.get("title") or f"{ten_a} và {ten_b}").strip()[:90],
            "chan_dung_a": str(tho.get("chan_dung_a") or ten_a).strip()[:400],
            "chan_dung_b": str(tho.get("chan_dung_b") or ten_b).strip()[:400],
            "mo_dau": " ".join(str(tho.get("mo_dau") or "").split())[:500],
            "ket": " ".join(str(tho.get("ket") or "").split())[:500],
            "vong": vong}


def ti_so(bang: Dict) -> Dict:
    a = sum(1 for v in bang.get("vong") or [] if v.get("thang") == "a")
    b = sum(1 for v in bang.get("vong") or [] if v.get("thang") == "b")
    return {"a": a, "b": b}


def lap_bang_worker(ten_a: str, ten_b: str, so_vong: int, settings: Dict, ghi_chu: str = "") -> Callable[[Dict], None]:
    """AI lập bảng so sánh. Chưa vẽ, chưa đọc gì: kết quả là bảng để người dùng duyệt."""
    def worker(job: Dict) -> None:
        lf.update_job(job, 10, f"Đang tra số liệu của {ten_a} và {ten_b}")
        n = max(SO_VONG[0], min(SO_VONG[1], int(so_vong or 5)))
        user = (f"Bên A: {ten_a}\nBên B: {ten_b}\nSố vòng: {n}\n"
                + (f"Người làm video dặn thêm: {ghi_chu.strip()[:400]}\n" if ghi_chu.strip() else ""))
        got = lf.ask_json(_BANG_HE, user, settings, max_tokens=3500)
        if not got.get("success"):
            raise RuntimeError("AI không lập được bảng: " + str(got.get("error") or "")[:200])
        bang = chuan_bang(got.get("data") or {}, ten_a, ten_b)
        if len(bang["vong"]) < 2:
            raise RuntimeError("AI không tìm được đủ chỉ số đếm được cho cặp này. Thử một cặp cùng thời, "
                               "hoặc dặn thêm muốn so những gì.")
        phien = {"ma": hashlib.md5(f"{ten_a}{ten_b}{time.time()}".encode()).hexdigest()[:10], "bang": bang}
        luu_phien(phien)
        job["result"] = {"bang": bang, "ti_so": ti_so(bang)}
        # Vẽ luôn hai chân dung để lúc duyệt bảng đã thấy mặt nhân vật. Vẽ hỏng không làm hỏng bảng.
        for k, phia in enumerate(("a", "b")):
            if job.get("cancel"):
                return
            lf.update_job(job, 45 + 25 * k, f"Bảng đã xong. Đang vẽ chân dung {bang['ten_' + phia]}")
            kq = ve_chan_dung(phien, phia, settings)
            if not kq.get("success"):
                lf.update_job(job, log=f"Chưa vẽ được chân dung {bang['ten_' + phia]}: {str(kq.get('error') or '')[:120]}")
            luu_phien(phien)
        lf.update_job(job, 100, f"Đã lập bảng {len(bang['vong'])} vòng. Đọc và sửa số liệu rồi mới dựng.")
    return worker


# ─── Dựng ─────────────────────────────────────────────────────────────────
def _ten_anh(ma: str, phia: str) -> str:
    # Tiền tố product_ để đường /remotion_asset của tool cho phép xem thử trên giao diện.
    return f"product_sd{ma}_{phia}.png"


def ve_chan_dung(phien: Dict, phia: str, settings: Dict) -> Dict:
    """Vẽ chân dung một bên theo lời tả trong bảng, ghi tên ảnh vào phiên. Trả kết quả của máy vẽ."""
    import remotion_generator as rg
    bang = phien["bang"]
    # Tên ảnh mang mốc giờ: vẽ lại là ra tên mới, trình duyệt và Remotion không dùng nhầm bản cũ.
    ten = _ten_anh(f"{phien['ma']}{int(time.time()) % 100000}", phia)
    duong = os.path.join(rg.REMOTION_PUBLIC_DIR, ten)
    os.makedirs(rg.REMOTION_PUBLIC_DIR, exist_ok=True)
    fmt = {**lf.get_format("long"), "image_width": 1024, "image_height": 1024}
    kq = lf.generate_image_router(CHAN_DUNG + bang["chan_dung_" + phia], duong, settings, fmt=fmt)
    if not kq.get("success") and lf.local_cli_ready():
        kq = lf.generate_image(CHAN_DUNG + bang["chan_dung_" + phia], duong, fmt=fmt)
    if kq.get("success"):
        cu = (phien.get("anh") or {}).get(phia)
        phien.setdefault("anh", {})[phia] = ten
        phien.setdefault("ta_da_ve", {})[phia] = bang["chan_dung_" + phia]
        if cu and cu != ten:
            try:
                os.remove(os.path.join(rg.REMOTION_PUBLIC_DIR, cu))
            except OSError:
                pass
    return kq


def co_chan_dung(phien: Dict, phia: str) -> bool:
    """Bên này đã có chân dung vẽ đúng theo lời tả hiện tại chưa."""
    import remotion_generator as rg
    ten = (phien.get("anh") or {}).get(phia) or ""
    return (bool(ten) and os.path.isfile(os.path.join(rg.REMOTION_PUBLIC_DIR, ten))
            and (phien.get("ta_da_ve") or {}).get(phia) == phien["bang"]["chan_dung_" + phia])


def ve_chan_dung_worker(phia: str, mo_ta: str, settings: Dict) -> Callable[[Dict], None]:
    """Vẽ lại chân dung một bên (tuỳ chọn tả lại). Chưa dựng phim."""
    def worker(job: Dict) -> None:
        phien = doc_phien()
        if mo_ta.strip():
            phien["bang"]["chan_dung_" + phia] = mo_ta.strip()[:400]
        lf.update_job(job, 20, f"Đang vẽ lại chân dung {phien['bang']['ten_' + phia]}")
        kq = ve_chan_dung(phien, phia, settings)
        if not kq.get("success"):
            raise RuntimeError("Không vẽ được chân dung: " + str(kq.get("error") or "")[:150])
        phien.pop("ket_qua", None)
        luu_phien(phien)
        lf.update_job(job, 100, "Đã vẽ lại chân dung. Video chưa đổi cho tới khi bạn bấm Dựng video.")
    return worker


def cac_canh(bang: Dict) -> List[Dict]:
    """Bảng thành danh sách cảnh theo thứ tự phim: mở, từng vòng, kết."""
    canh: List[Dict] = []
    if bang.get("mo_dau"):
        canh.append({"loai": "mo", "text": bang["mo_dau"]})
    for v in bang["vong"]:
        canh.append({"loai": "vong", "text": v.get("loi") or f"{v['chi_so']}.",
                     **{k: v.get(k) for k in ("chi_so", "don_vi", "a", "b", "hien_a", "hien_b", "thang", "nguon")}})
    canh.append({"loai": "ket", "text": bang.get("ket") or "Bạn chọn ai?"})
    return canh


def dung_worker(bang: Dict, voice: Dict, settings: Dict, ve_lai_anh: bool = False) -> Callable[[Dict], None]:
    """Vẽ hai chân dung, đọc lời, dựng phim. Chân dung đã vẽ thì giữ, trừ khi bảo vẽ lại."""
    def worker(job: Dict) -> None:
        import remotion_generator as rg
        huy = lambda: bool(job.get("cancel"))  # noqa: E731
        phien = doc_phien()
        ma = phien.get("ma") or hashlib.md5(str(time.time()).encode()).hexdigest()[:10]
        # Đổi cặp đấu mà không lập bảng lại: chân dung cũ không còn đúng người.
        cu = phien.get("bang") or {}
        if (cu.get("ten_a"), cu.get("ten_b")) != (bang["ten_a"], bang["ten_b"]):
            phien.pop("anh", None)
            phien.pop("ta_da_ve", None)
        phien.update({"ma": ma, "bang": bang, "voice": voice})
        phien.pop("ket_qua", None)
        luu_phien(phien)
        for k, phia in enumerate(("a", "b")):
            if huy():
                return
            if co_chan_dung(phien, phia) and not ve_lai_anh:
                continue
            lf.update_job(job, 5 + 15 * k, f"Đang vẽ chân dung {bang['ten_' + phia]}")
            kq = ve_chan_dung(phien, phia, settings)
            if not kq.get("success"):
                # Thiếu chân dung thì phim vẫn dựng được, ô chân dung hiện chữ cái đầu của tên.
                (phien.get("anh") or {}).pop(phia, None)
                lf.update_job(job, log=f"Không vẽ được chân dung {bang['ten_' + phia]}: {str(kq.get('error') or '')[:120]}")
            luu_phien(phien)
        anh = phien.get("anh") or {}
        lf.update_job(job, 35, "Đang đọc lời")
        canh = cac_canh(bang)
        doc = lf.doc_loi_dan(canh, voice, "edge", settings, log=lambda m: lf.update_job(job, log=m))
        im_lang = lf.missing_voices(doc)
        if im_lang:
            raise RuntimeError(lf.missing_voices_message(im_lang))
        for c, v in zip(canh, doc):
            khung = int(v.get("voiceFrames") or 0)
            c.update({"audioFile": v.get("audioFile") or "", "voiceFrames": khung,
                      "durationInFrames": max(VONG_TOI_THIEU if c["loai"] == "vong" else 90, khung + NGHI_SAU_LOI)})
        if huy():
            return
        props = {"title": bang["title"],
                 "ben": [{"ten": bang["ten_a"], "anh": anh.get("a", "")}, {"ten": bang["ten_b"], "anh": anh.get("b", "")}],
                 "canh": canh}
        ten_phim = f"songdau_{ma}.mp4"
        lf.update_job(job, 45, "Đang dựng phim")
        kq = rg.render_remotion_video(
            "SongDau", ten_phim, props, timeout=3600,
            on_progress=lambda pt, *a: lf.update_job(job, 45 + int(54 * max(0.0, min(100.0, float(pt or 0))) / 100)))
        if not kq.get("success"):
            raise RuntimeError(kq.get("message") or "Dựng phim hỏng")
        giay = sum(c["durationInFrames"] for c in canh) / FPS
        phien["ket_qua"] = {"download_url": f"/downloads/{ten_phim}", "giay": round(giay)}
        luu_phien(phien)
        job["result"] = {**phien, "ti_so": ti_so(bang)}
        lf.update_job(job, 100, f"Xong video song đấu {len(bang['vong'])} vòng, dài {int(giay // 60)} phút {int(giay % 60)} giây")
    return worker
