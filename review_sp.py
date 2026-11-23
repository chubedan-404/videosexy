"""
Xưởng Review sản phẩm (PlayAh): video dọc chỉ có bàn tay cầm sản phẩm, giọng nhân bản kể một chuyện drama lấy
từ Threads, một mẩu của bài đó được đính lên hình lúc dựng.

Trình tự: ảnh sản phẩm -> chọn bài Threads trong kho bài -> lời kể vừa số giây -> giọng -> các clip tay cầm
(máy làm clip nhận ảnh sản phẩm làm mẫu) -> dựng (Remotion: nền clip, giọng, thẻ bài đính, phụ đề).

Mỗi video là một thư mục trong downloads/review_sp/. Cách lấy bài Threads về kho CHƯA chốt: hiện kho nhận bài
dán tay, và đọc thử được một đường dẫn công khai (chỉ ra bài đầu, bài dài có thể bị cắt).
"""
import asyncio
import hashlib
import html as html_lib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import requests
from flask import Blueprint, jsonify, request, send_from_directory

import xuong_chung as pc

bp = Blueprint("review_sp", __name__)

XUONG = "review_sp"
KHO = os.path.join(pc.TAI_VE, "review_sp")
KHO_BAI = os.path.join(KHO, "kho_bai.json")
_MA = re.compile(r"^rv_\d{8}_\d{6}_[a-f0-9]{4}$")
_ANH = (".png", ".jpg", ".jpeg", ".webp")

SO_GIAY = 50
# Bài dài thì thẻ đính đổi lần lượt qua vài đoạn. Nhiều hơn số này thì mỗi thẻ hiện quá ngắn, không kịp đọc.
SO_THE_TOI_DA = 4
# Bố cục bản dựng. "khung": clip nằm ở giữa, trên và dưới là dải trắng chứa thẻ bài và phụ đề, nên chữ không đè lên sản phẩm
# và phần mép trên dưới của clip (kể cả dấu mờ ở góc dưới) nằm ngoài khung. "tran": clip phủ kín, chữ đè lên clip.
BO_CUC = {"khung": "Clip ở giữa, nền trắng", "tran": "Clip tràn khung"}
# Thẻ đính lên video: "anh" dán thẳng ảnh chụp bài gốc trên Threads (mặc định khi bài có ảnh chụp), "chu" là thẻ chữ vẽ lại.
THE_KIEU = {"anh": "Ảnh chụp bài gốc", "chu": "Thẻ chữ"}
GIAY_CLIP = (5, 10)
# Máy làm clip chọn được theo từng video. Muse là mặc định; Flow của Google ra clip 8 giây.
MAY_CLIP = {"muse": "Muse", "flow": "Google Flow", "gemini": "Gemini", "kling": "Kling"}
GIAY_FLOW = (4, 6, 8, 10)

# Cách quay = máy làm clip + model, chọn trong MỘT ô (07/11/2026, người dùng dặn "show cho chọn model và museai"). Mã là "muse"
# hoặc "flow:<tên model trên Flow>". UGC, Làm hàng loạt, Lịch tự động dùng chung bảng này. Credit đo thật ngày 06/11/2026.
CACH_QUAY = [
    {"id": "flow:Omni 1.2 Flash", "may": "flow", "model": "Omni 1.2 Flash", "ten": "Google Flow · Omni 1.2", "credit": 30, "giay": 10,
     "ghi": "Khuyên dùng. Viết đúng chữ trên bao bì. 30 credit một clip."},
    {"id": "flow:Omni 1.1 Flash", "may": "flow", "model": "Omni 1.1 Flash", "ten": "Google Flow · Omni 1.1", "credit": 15, "giay": 10,
     "ghi": "Rẻ hơn một nửa nhưng hay viết sai tên sản phẩm."},
    {"id": "flow:Veo 3.1 - Fast", "may": "flow", "model": "Veo 3.1 - Fast", "ten": "Google Flow · Veo 3.1 Fast", "credit": 20, "giay": 8,
     "ghi": "Clip 8 giây. Hay từ chối sản phẩm nhạy cảm (đã từ chối Pleasure Gel)."},
    {"id": "flow:Veo 3.1 - Lite", "may": "flow", "model": "Veo 3.1 - Lite", "ten": "Google Flow · Veo 3.1 Lite", "credit": 10, "giay": 8,
     "ghi": "Rẻ nhất, clip 8 giây, chất lượng thấp, hay từ chối sản phẩm nhạy cảm."},
    {"id": "flow:Veo 3.1 - Quality", "may": "flow", "model": "Veo 3.1 - Quality", "ten": "Google Flow · Veo 3.1 Quality", "credit": 100, "giay": 8,
     "ghi": "Đắt nhất (100 credit một clip), clip 8 giây, hay từ chối sản phẩm nhạy cảm."},
    # 07/11/2026: app Gemini của tài khoản Pro (gemini_may.py). Không trừ credit Flow; hạn mức riêng của app chưa rõ.
    {"id": "gemini", "may": "gemini", "model": "", "ten": "Gemini (tài khoản Pro)", "credit": 0, "giay": 10,
     "ghi": "Không tốn credit Flow, viết đúng chữ trên bao bì (thử 07/11). Mỗi clip khoảng 3 phút, quay lần lượt; app có hạn mức mỗi ngày."},
    # 09/11/2026: Kling (kling_may.py), một tài khoản đăng nhập trong cửa sổ Chrome của tool. Credit Kling (không phải Flow), đo thật
    # trên trang. Gói miễn phí 66 credit/ngày, có hình mờ, cấm dùng thương mại: clip quảng cáo phải từ gói trả phí.
    {"id": "kling:Kling 4.0", "may": "kling", "model": "Kling 4.0", "ten": "Kling 4.0", "credit": 50, "giay": 5,
     "ghi": "Giữ nhãn sản phẩm đúng nhất (thử 09/11). 50 credit Kling một clip 5 giây. Gói miễn phí phải xếp hàng 30 phút tới hơn 1 tiếng."},
    {"id": "kling:Kling 4.0 Flash", "may": "kling", "model": "Kling 4.0 Flash", "ten": "Kling 4.0 Flash", "credit": 30, "giay": 5,
     "ghi": "Rẻ hơn (30 credit Kling một clip 5 giây). Chưa thử chất lượng."},
    {"id": "kling:Kling O1", "may": "kling", "model": "Kling O1", "ten": "Kling O1", "credit": 30, "giay": 5,
     "ghi": "30 credit Kling một clip 5 giây. Chưa thử chất lượng."},
    {"id": "muse", "may": "muse", "model": "", "ten": "Muse AI", "credit": 0, "giay": 10,
     "ghi": "Không tốn credit Flow. Mỗi clip 3 tới 4 phút, đôi khi trả lại video cũ."},
]
CACH_MAC_DINH = "flow:Omni 1.2 Flash"
_CACH = {x["id"]: x for x in CACH_QUAY}


def cach_quay(da: Dict) -> str:
    """Mã cách quay đang dùng của video. Video cũ chưa chọn model thì tính theo model mặc định của Flow."""
    if (da.get("may_clip") or "muse") == "gemini":
        return "gemini"
    if da.get("may_clip") == "kling":
        return "kling:" + (da.get("kling_model") or "Kling 4.0")
    if (da.get("may_clip") or "muse") != "flow":
        return "muse"
    import flow_may
    return "flow:" + (da.get("flow_model") or flow_may.MODEL)


def tim_cach(ma: str) -> Dict:
    """Một dòng của CACH_QUAY. Mã cũ "flow" (trước 07/11) là Flow với model mặc định."""
    if ma == "flow":
        ma = CACH_MAC_DINH
    return _CACH.get(ma) or _CACH[CACH_MAC_DINH]


def ap_cach_quay(da: Dict, ma: str) -> Dict:
    """Đặt máy, model và số giây mỗi clip theo một cách quay. Veo chỉ có clip 8 giây."""
    c = tim_cach(ma)
    if c["may"] == "kling":
        da["may_clip"], da["kling_model"], da["giay_clip"] = "kling", c["model"], c["giay"]
        if kieu_dung(da) == "noi_canh":
            da["noi_canh"] = False
        return da
    da["may_clip"], da["flow_model"] = c["may"], c["model"]
    if c["may"] == "flow" and c["model"].startswith("Veo"):
        da["giay_clip"] = 8
    elif int(da.get("giay_clip") or 10) not in (GIAY_FLOW if c["may"] == "flow" else GIAY_CLIP) or c["model"] == "Omni 1.2 Flash":
        da["giay_clip"] = c["giay"]
    if c["may"] != "flow" and kieu_dung(da) == "noi_canh":
        da["noi_canh"] = False  # nối khung chỉ Flow làm được
    return da


# Kiểu dựng: một lựa chọn thay cho hai ô "Nối cảnh" và "Cắt nhanh" (hai ô đó loại trừ nhau mà bật được cùng lúc).
KIEU_DUNG = {"cat_nhanh": "Cắt nhanh kiểu Feelex", "noi_canh": "Liền một cảnh", "thuong": "Mỗi clip một cảnh"}
KIEU_DUNG_GHI = {
    "cat_nhanh": "Mỗi clip lấy vài đoạn khoảng 2 giây, cắt thẳng, đổi cảnh liên tục như quảng cáo Feelex. Đoạn lỗi được bỏ.",
    "noi_canh": "Clip sau mở đầu bằng khung cuối của clip trước, cả video liền một cảnh. Chỉ Google Flow, quay lần lượt nên lâu hơn.",
    "thuong": "Mỗi clip là một cảnh trọn vẹn, nối nhau bằng chuyển cảnh mềm.",
}


def kieu_dung(da: Dict) -> str:
    if da.get("cat_nhanh"):
        return "cat_nhanh"
    if da.get("may_clip") == "flow" and da.get("noi_canh", True):
        return "noi_canh"
    return "thuong"


def ap_kieu_dung(da: Dict, kieu: str) -> Dict:
    if kieu not in KIEU_DUNG or (kieu == "noi_canh" and da.get("may_clip") != "flow"):
        raise ValueError("Liền một cảnh chỉ làm được với Google Flow." if kieu == "noi_canh" else "Kiểu dựng không hợp lệ.")
    da["cat_nhanh"], da["noi_canh"] = kieu == "cat_nhanh", kieu == "noi_canh"
    return da
# Clip có ảnh mẫu hay mở đầu bằng chính ảnh mẫu đứng yên; cắt bỏ đoạn đó (cùng mức với xưởng Tiên hiệp).
CAT_DAU = 0.4
CHO_CLIP = 420
MUSE_CUNG_LUC = 3          # số cảnh Muse quay cùng lúc (mỗi cảnh một cửa sổ), đo 15/11/2026
MUSE_CUNG_LUC_TOI_DA = 4
MUSE_GIAN_CACH = 4         # giây giữa lúc các cửa sổ bắt đầu

# Kiểu quay theo 5 video mẫu Heer Feel của khách (13/10/2026): máy cố định nhìn thẳng từ trên xuống mặt bàn, chỉ hai bàn tay
# làm việc với sản phẩm, quay chậm rãi. Sản phẩm thì đổi theo ảnh mẫu của từng video, không dính gì tới hộp Heer Feel.
# Bối cảnh đầu tiên là mặc định của video mới; lịch tự động xoay vòng qua cả bảng để các video không giống hệt nhau.
BOI_CANH = {
    "ban_go": ("Bàn gỗ sáng", "a clean light-oak wooden tabletop that fills the whole frame, one small pastel prop at a top corner"),
    "ban_lam_viec": ("Bàn làm việc", "a tidy wooden desk seen from above, a laptop edge and a coffee cup at the corners of the frame"),
    "phong_ngu": ("Phòng ngủ", "a neatly made bed with smooth cream sheets seen from above"),
    "quan_ca_phe": ("Quán cà phê", "a small round cafe table seen from above, an iced drink at one corner of the frame"),
    "ban_trang_diem": ("Bàn trang điểm", "a clean white vanity tabletop seen from above, a few cosmetics at the corners of the frame"),
    "sofa": ("Ghế sofa", "a knitted cream blanket spread flat on a sofa seat, seen from above"),
}
TAY = {
    "nu": ("Tay nữ", "the two hands and forearms of a young Vietnamese woman, cream long sleeves, neat dark-polished nails"),
    "nam": ("Tay nam", "the two hands and forearms of a young Vietnamese man, grey long sleeves, clean short nails, a simple watch"),
}
# Thứ tự này là thứ tự các clip của một video, đi theo trình tự của video mẫu: cầm lên khoe, xoay, mở, bày ra, khoe một món,
# chỉ, xếp lại. Video cần nhiều clip hơn thì quay vòng lại từ đầu. KHÔNG đổi mã của động tác: video đã làm và phần Học từ số
# liệu đang nhớ theo mã.
HANH_DONG = [
    ("cam_len", "Cầm lên khoe mặt trước",
     "Both hands come into frame holding the product, lift it a little above the surface and tilt its front side up toward the camera, then hold it still."),
    ("xoay", "Xoay cho thấy các mặt",
     "The hands slowly turn the product around above the surface to show the front, one side, the back, then the front again."),
    ("mo_hop", "Mở nắp, mở hộp",
     "One hand steadies the product on the surface while the other slowly opens its lid or packaging and sets the lid down beside it. If the product cannot be opened, the hands turn it over to show the underside instead."),
    ("bay_ra", "Bày thứ bên trong ra bàn",
     "The hands take out what is inside the product and lay the pieces out one by one in a loose row on the surface next to it. If nothing is inside, the hands lay the product flat and smooth the surface around it."),
    ("khoe_mot", "Cầm một món lên khoe",
     "Two fingers pick one single piece up from the surface and both hands hold it close under the camera, turning it gently between the fingertips."),
    ("chi_logo", "Chỉ vào mặt trước",
     "One hand holds the product flat in the palm while the index finger of the other hand taps and points at its front."),
    ("dat_xuong", "Đặt xuống giữa bàn",
     "A hand places the product upright in the centre of the surface and nudges it into place with two fingers."),
    ("hai_tay", "Hai tay nâng",
     "Both hands cradle the product just above the surface and present it up toward the camera, thumbs resting on its edges."),
    ("rut_tay", "Xếp gọn rồi rút tay",
     "The hands tidy everything into a neat arrangement around the product, then slide slowly out of frame, leaving the product resting still."),
    # Ba động tác riêng của sản phẩm dạng gel, kem (xem TRINH_TU): nặn ra, bôi lên tay, khoe chất.
    ("nan_ra", "Nặn sản phẩm ra tay",
     "One hand holds the product tilted over the other hand, which rests palm-up on the surface, and gently squeezes or pumps a small pearl of clear gel onto its fingertips. The gel visibly comes out of the product's own opening."),
    ("boi_tay", "Bôi lên mu bàn tay",
     "The product rests on the surface beside the hands. The fingertips of one hand spread a small amount of clear gel over the back of the other hand in slow circular strokes, leaving a thin glossy layer on the skin."),
    ("khoe_chat", "Khoe chất gel giữa hai ngón tay",
     "With the product standing on the surface beside the hand, the thumb and index finger of one hand press together on a drop of clear gel and slowly pull apart right under the camera, showing its smooth, slightly stretchy texture."),
    ("lac_nhe", "Lắc nhẹ",
     "One hand holds the product above the surface and gives it a small playful shake, then holds it still."),
    ("bo_tui", "Cho vào túi nhỏ",
     "A hand slips the product into a small pouch lying on the surface, then pulls it halfway out again."),
    ("gio_len", "Nâng lên gần ống kính",
     "A hand lifts the product straight up toward the camera until it is large in the frame, holds it steady, then lowers it back."),
    ("thich", "Giơ ngón cái",
     "One hand holds the product on the surface while the other hand gives a clear thumbs-up right next to it."),
]
_HD = {k: (vn, en) for k, vn, en in HANH_DONG}
# Loại sản phẩm quyết định TRÌNH TỰ động tác của một video (người dùng dặn 13/10/2026: gel thì phải có cảnh nặn ra rồi bôi lên
# tay, không phải mở hộp đổ gói như Heer Feel). Thêm loại mới: thêm một dòng ở đây và ở LOAI_SP, động tác mới thì thêm vào
# HANH_DONG. Trình tự là một câu chuyện có trước có sau (mở rồi mới nặn, nặn rồi mới bôi), nên không xoay thứ tự.
LOAI_SP = {"hop": "Hộp, gói (mở ra bày lên bàn)", "gel": "Gel, kem (nặn ra bôi lên tay)"}
TRINH_TU = {
    "hop": ["cam_len", "xoay", "mo_hop", "bay_ra", "khoe_mot", "chi_logo", "dat_xuong", "hai_tay", "rut_tay"],
    "gel": ["cam_len", "mo_hop", "nan_ra", "boi_tay", "khoe_chat", "xoay", "chi_logo", "dat_xuong", "rut_tay"],
}
_TU_GEL = ("gel", "kem", "cream", "serum", "lotion", "tinh chat", "dau massage", "boi tron", "lubricant", "tuyp", "sua duong")


def doan_loai(*chu: str) -> str:
    """Đoán loại sản phẩm từ TÊN khi người dùng chưa chọn: có chữ gel, kem, serum... thì là "gel", còn lại là "hop"."""
    import unicodedata
    gop = unicodedata.normalize("NFD", " ".join(str(c or "") for c in chu).replace("đ", "d").replace("Đ", "D"))
    gop = " " + re.sub(r"[^a-z0-9]+", " ", "".join(c for c in gop if unicodedata.category(c) != "Mn").lower()) + " "
    return "gel" if any(f" {t} " in gop for t in _TU_GEL) else "hop"


def trinh_tu(da: Dict) -> List[str]:
    return TRINH_TU.get(da.get("loai_sp") or "") or TRINH_TU["hop"]
BOI_CANH_MAC_DINH = next(iter(BOI_CANH))

_VIET_HE = (
    "Bạn viết lời kể cho video ngắn TikTok tiếng Việt. Người kể là một người trẻ, kể chuyện drama đời thường cho "
    "bạn bè nghe: câu ngắn, lời nói thường ngày, không văn vẻ, không chào hỏi, không kêu gọi mua hàng, không nhắc "
    "tới sản phẩm nào. Câu đầu phải gây tò mò ngay. Giữ đúng sự việc của bài gốc, không bịa thêm tình tiết, không "
    "nêu tên thật hay tài khoản của ai. Không dùng từ tục, không tả cảnh nhạy cảm. Chỉ trả về JSON."
)


# ── Dự án trên đĩa ───────────────────────────────────────────────────────────

def _thu_muc(pid: str) -> str:
    if not _MA.match(pid or ""):
        raise ValueError("Mã video không hợp lệ.")
    return os.path.join(KHO, pid)


def _tep(pid: str) -> str:
    return os.path.join(_thu_muc(pid), "du_an.json")


def tao(ten: str = "") -> Dict:
    pid = time.strftime("rv_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    da = {"id": pid, "ten": (ten or "").strip()[:80] or "Video chưa đặt tên", "tao_luc": time.time(),
          "san_pham": {"ten": "", "anh": []}, "bai": None, "loi_ke": "", "the": {"tac_gia": "", "chu": ""},
          "giong": "", "so_giay": SO_GIAY, "giay_clip": 10, "may_clip": "muse", "boi_canh": BOI_CANH_MAC_DINH, "tay": "nu",
          "phu_de": True, "bo_cuc": "khung", "canh": [], "giong_doc": None, "phim": []}
    # 07/11/2026: video mới mặc định quay Flow Omni 1.2 (đúng chữ trên bao bì) và dựng cắt nhanh kiểu Feelex.
    ap_cach_quay(da, CACH_MAC_DINH)
    ap_kieu_dung(da, "cat_nhanh")
    luu(da)
    pc.ghi_nhat_ky(XUONG, "tao", pid, ten=da["ten"])
    return da


def mo(pid: str) -> Dict:
    da = pc.doc_json(_tep(pid))
    if not da:
        raise FileNotFoundError("Không tìm thấy video này.")
    return da


def luu(da: Dict) -> Dict:
    da["sua_luc"] = time.time()
    pc.giu_du_an(da["id"])  # ghi từ việc nền: nhớ là việc đó đang giữ video này (nút Lưu của trang sẽ chờ)
    pc.ghi_json(_tep(da["id"]), da)
    return da


def ds() -> List[Dict]:
    ra = []
    for ten in sorted(os.listdir(KHO), reverse=True) if os.path.isdir(KHO) else []:
        if not _MA.match(ten):
            continue
        da = pc.doc_json(os.path.join(KHO, ten, "du_an.json"))
        if da:
            ra.append({"id": da["id"], "ten": da.get("ten"), "sua_luc": da.get("sua_luc"),
                       "anh": (da.get("san_pham", {}).get("anh") or [None])[0],
                       "phim": (da.get("phim") or [{}])[-1].get("file", ""),
                       "xong": sum(1 for c in da.get("canh") or [] if c.get("trang_thai") == "xong"),
                       "tong": len(da.get("canh") or [])})
    return ra


# ── Kho bài Threads ──────────────────────────────────────────────────────────

def kho_bai() -> List[Dict]:
    return pc.doc_json(KHO_BAI, []) or []


def them_bai(tac_gia: str, noi_dung: str, url: str = "", ten_hien: str = "", anh: bytes = b"", so_phan: int = 1,
             anh_them: Optional[List[bytes]] = None) -> Dict:
    noi_dung = (noi_dung or "").strip()
    if len(noi_dung) < 40:
        raise ValueError("Bài quá ngắn để kể thành một video. Cần ít nhất vài câu.")
    tac_gia = "@" + (tac_gia or "").strip().lstrip("@")[:40] if (tac_gia or "").strip() else ""
    ma = hashlib.sha1(noi_dung.encode("utf-8")).hexdigest()[:10]
    bai = kho_bai()
    cu = next((b for b in bai if b["id"] == ma), None)
    if not cu or (anh and not cu.get("anh")):
        # Ảnh chụp ô bài gốc trên Threads (và từng phần nối tiếp của tác giả): video dán thẳng các ảnh này lên hình.
        cac_anh = []
        for i, tep in enumerate([anh] + list(anh_them or [])):
            if not tep:
                continue
            os.makedirs(os.path.join(KHO, "anh_bai"), exist_ok=True)
            ten = ma + (".png" if i == 0 else f"_{i + 1}.png")
            with open(os.path.join(KHO, "anh_bai", ten), "wb") as f:
                f.write(tep)
            cac_anh.append(ten)
        moi = {"id": ma, "tac_gia": tac_gia, "ten_hien": (ten_hien or "").strip()[:60], "noi_dung": noi_dung[:6000],
               "url": (url or "").strip()[:300], "luc": time.time(), "anh": cac_anh[0] if cac_anh else "",
               "anh_phan": cac_anh, "so_phan": int(so_phan or 1)}
        bai = [moi] + [b for b in bai if b["id"] != ma]
        pc.ghi_json(KHO_BAI, bai)
    return next(b for b in bai if b["id"] == ma)


def them_bai_tu_link(doc: Dict) -> Dict:
    """Cất bài đọc được từ link (kết quả của `doc_duong_dan_threads`) vào kho, kèm mọi ảnh chụp."""
    return them_bai(doc["tac_gia"], doc["noi_dung"], doc["url"], doc["ten_hien"], doc.get("anh") or b"",
                    doc.get("so_phan") or 1, doc.get("anh_phan") or [])


def anh_bai(bai: Optional[Dict]) -> List[str]:
    """Đường dẫn các ảnh chụp bài gốc còn trên đĩa, theo thứ tự các phần."""
    ten = (bai or {}).get("anh_phan") or ([bai["anh"]] if (bai or {}).get("anh") else [])
    return [os.path.join(KHO, "anh_bai", t) for t in ten[:SO_THE_TOI_DA] if os.path.isfile(os.path.join(KHO, "anh_bai", t))]


def xoa_bai(ma: str) -> None:
    pc.ghi_json(KHO_BAI, [b for b in kho_bai() if b["id"] != ma])


_SO_DEM = re.compile(r"^[\d.,]+\s*[KkMmNnTt]?[RrBb]?$")
_BO_DONG = {"dịch", "translate", "xem bản dịch", "đã chỉnh sửa", "edited", "tác giả", "author", "·"}
_JS_BAI = """() => [...document.querySelectorAll('div[data-pressable-container="true"]')].map((e, i) => ({
  thu: i, chu: e.innerText || '', link: [...e.querySelectorAll('a[href*="/post/"]')].map(a => a.getAttribute('href'))[0] || '',
  ai: ([...e.querySelectorAll('a[href^="/@"]')].map(a => a.getAttribute('href'))[0] || '').replace('/@', '').split('/')[0] }))"""


def _loc_chu(chu: str, ai: str) -> str:
    """Lấy phần nội dung của một ô bài: bỏ tên tài khoản, ngày đăng ở đầu và các con số thích, trả lời ở cuối."""
    dong = [d.strip() for d in (chu or "").split("\n") if d.strip()]
    if dong and dong[0].lstrip("@") == ai:
        dong = dong[1:]
    def la_luc(x):
        return re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", x) or re.match(r"^\d+\s*(giây|phút|giờ|ngày|tuần|s|m|h|d|w)\b", x)
    # Bài gắn chủ đề (ví dụ "Công ty") thì dòng chủ đề đứng trước dòng giờ đăng: bỏ cả hai.
    if len(dong) > 1 and len(dong[0]) <= 40 and la_luc(dong[1]):
        dong = dong[2:]
    elif dong and la_luc(dong[0]):
        dong = dong[1:]
    while dong and (_SO_DEM.match(dong[-1]) or dong[-1].lower() in _BO_DONG):
        dong.pop()
    return "\n".join(d for d in dong if d.lower() not in _BO_DONG).strip()


def _doc_threads_trinh_duyet(url: str) -> Dict:
    """
    Mở bài Threads trong một trình duyệt ẩn (không đăng nhập) rồi đọc thẳng trên trang: lấy đủ chữ của bài, cộng các bài
    nối tiếp của CHÍNH TÁC GIẢ ngay bên dưới (chuỗi nhiều phần), và chụp ảnh ô bài gốc.
    """
    from playwright.sync_api import sync_playwright
    ma = re.search(r"/post/([\w-]+)", url).group(1)
    with sync_playwright() as pw:
        trinh = pw.chromium.launch(channel="chrome", headless=True)
        try:
            trang = trinh.new_context(viewport={"width": 520, "height": 1600}, device_scale_factor=2, locale="vi-VN", user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")).new_page()
            trang.goto(url, wait_until="domcontentloaded", timeout=45000)
            trang.wait_for_selector('div[data-pressable-container="true"]', timeout=20000)
            trang.wait_for_timeout(1500)
            o = trang.evaluate(_JS_BAI)
            goc = next((x for x in o if f"/post/{ma}" in (x.get("link") or "")), o[0] if o else None)
            if not goc:
                raise RuntimeError("Không thấy bài trên trang (bài riêng tư hoặc đã xoá).")
            phan, thu_phan = [_loc_chu(goc["chu"], goc["ai"])], [goc["thu"]]
            for x in o[goc["thu"] + 1:]:
                if x["ai"] != goc["ai"]:
                    break  # tới bình luận của người khác thì hết chuỗi của tác giả
                phan.append(_loc_chu(x["chu"], x["ai"]))
                thu_phan.append(x["thu"])
            # Chụp từng ô bài (bài gốc và các phần nối tiếp) ở độ nét gấp đôi, để dán thẳng lên video.
            cac_anh = []
            for thu in thu_phan[:SO_THE_TOI_DA]:
                try:
                    cac_anh.append(trang.locator('div[data-pressable-container="true"]').nth(thu).screenshot(type="png"))
                except Exception:  # noqa: BLE001 - không chụp được ảnh thì vẫn dùng được chữ
                    break
            anh, anh_phan = (cac_anh[0] if cac_anh else b""), cac_anh[1:]
            ten_hien = (trang.title() or "").split("(@")[0].strip() if "(@" in (trang.title() or "") else ""
        finally:
            trinh.close()
    noi_dung = "\n\n".join(p for p in phan if p)
    if not noi_dung:
        raise RuntimeError("Bài này không có chữ (chỉ có ảnh hoặc video).")
    return {"tac_gia": "@" + goc["ai"], "ten_hien": ten_hien, "noi_dung": noi_dung, "url": url, "so_phan": len([p for p in phan if p]), "anh": anh,
            "anh_phan": anh_phan}


# ── Lấy nhiều bài từ một trang Threads (07/11/2026, người dùng bảo làm) ────────────────────────────────────────────────────
# Mở trang cá nhân công khai (threads.com/@ten) bằng trình duyệt ẩn KHÔNG đăng nhập, cuộn vài lượt, gom link bài của chính trang
# đó, rồi đọc từng bài bằng cách đọc một link như cũ. Chỉ giữ bài đủ dài để kể (`it_nhat_tieng`), bỏ bài đã có trong kho.
# Không lấy theo từ khoá: tìm kiếm trên Threads phải đăng nhập.
def link_bai_cua_trang(url_trang: str, toi_da: int = 20) -> List[str]:
    from playwright.sync_api import sync_playwright
    m = re.search(r"threads\.(?:com|net)/@([\w.]+)", url_trang or "")
    if not m:
        raise ValueError("Dán link trang cá nhân Threads, dạng https://www.threads.com/@ten.tai.khoan")
    ten = m.group(1)
    with sync_playwright() as pw:
        trinh = pw.chromium.launch(channel="chrome", headless=True)
        try:
            trang = trinh.new_context(viewport={"width": 520, "height": 1600}, locale="vi-VN", user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")).new_page()
            trang.goto(f"https://www.threads.com/@{ten}", wait_until="domcontentloaded", timeout=45000)
            trang.wait_for_selector('a[href*="/post/"]', timeout=20000)
            link: List[str] = []
            for _ in range(8):
                for h in trang.evaluate("() => [...document.querySelectorAll('a[href*=\"/post/\"]')].map(a => a.getAttribute('href'))"):
                    mm = re.match(rf"^/@{re.escape(ten)}/post/([\w-]+)$", h or "")
                    if mm:
                        u = f"https://www.threads.com/@{ten}/post/{mm.group(1)}"
                        if u not in link:
                            link.append(u)
                if len(link) >= toi_da:
                    break
                trang.mouse.wheel(0, 2400)
                trang.wait_for_timeout(1500)
        finally:
            trinh.close()
    return link[:toi_da]


def lay_nhieu_bai(url_trang: str, toi_da: int = 10, it_nhat_tieng: int = 60, viec: Optional[Dict] = None) -> Dict:
    """Cất tối đa `toi_da` bài MỚI đủ dài của một trang vào kho bài. Trả số bài đã cất, đã có, quá ngắn, đọc hỏng."""
    co = {(b.get("url") or "").split("?")[0] for b in kho_bai()}
    cac = [u for u in link_bai_cua_trang(url_trang, toi_da * 3) if u not in co]
    dem = {"cat": 0, "da_co": 0, "ngan": 0, "hong": 0, "bai": []}
    for i, u in enumerate(cac):
        if dem["cat"] >= toi_da or (viec and viec.get("huy")):
            break
        if viec:
            pc.buoc(viec, round(100 * i / max(1, len(cac))), f"Đang đọc bài {i + 1}/{len(cac)} ({dem['cat']} bài đã cất)")
        try:
            doc = doc_duong_dan_threads(u)
        except Exception:  # noqa: BLE001 - bài riêng tư, chỉ có ảnh... bỏ qua bài đó
            dem["hong"] += 1
            continue
        if pc.so_tieng(doc["noi_dung"]) < it_nhat_tieng:
            dem["ngan"] += 1
            continue
        dem["bai"].append(them_bai_tu_link(doc)["id"])
        dem["cat"] += 1
    return dem


def _doc_threads_the_xem_truoc(url: str) -> Dict:
    """Cách dự phòng khi trình duyệt ẩn không chạy được: đọc thẻ xem trước của trang. Chỉ ra bài đầu, bài dài có thể bị cắt."""
    res = requests.get(url, timeout=25, headers={"User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"})
    if res.status_code != 200:
        raise RuntimeError(f"Threads trả mã {res.status_code}, chưa đọc được bài này.")

    def the(ten):
        m = re.search(r'<meta property="og:%s" content="([^"]*)"' % ten, res.text)
        return html_lib.unescape(m.group(1)).strip() if m else ""
    noi_dung, tieu_de = the("description"), the("title")
    m = re.search(r"^(.*?)\s*\((@[\w.]+)\)", tieu_de)
    if not noi_dung:
        raise RuntimeError("Không thấy nội dung trong bài này (có thể bài riêng tư hoặc đã xoá).")
    return {"tac_gia": m.group(2) if m else "", "ten_hien": m.group(1) if m else "", "noi_dung": noi_dung, "url": url, "so_phan": 1, "anh": b""}


def doc_duong_dan_threads(url: str) -> Dict:
    """Lấy một bài Threads công khai từ đường dẫn: tác giả, đủ nội dung (cả chuỗi nhiều phần của tác giả) và ảnh chụp bài."""
    url = (url or "").strip().split("?")[0]
    if re.match(r"^https://(www\.)?threads\.(net|com)/share/[\w-]+", url):
        # Link "Chia sẻ" của app Threads chuyển hướng tới link bài thật.
        try:
            url = requests.head(url, allow_redirects=False, timeout=20, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/126"}
                                ).headers.get("location", url).split("?")[0]
        except requests.RequestException:
            pass
    if not re.match(r"^https://(www\.)?threads\.(net|com)/@[\w.]+/post/[\w-]+", url):
        raise ValueError("Đường dẫn phải là một bài Threads, dạng https://www.threads.com/@ten/post/...")
    try:
        return _doc_threads_trinh_duyet(url)
    except (ValueError, RuntimeError):
        raise
    except Exception:  # noqa: BLE001 - trình duyệt ẩn hỏng (chưa cài Chrome, trang đổi cấu trúc): thử cách dự phòng
        return _doc_threads_the_xem_truoc(url)


# ── Câu lệnh quay tay ────────────────────────────────────────────────────────

def cau_lenh(hanh_dong: str, boi_canh: str, tay: str) -> str:
    """Câu lệnh cố định cho một clip: máy cố định nhìn từ trên xuống, chỉ bàn tay và sản phẩm đúng như ảnh mẫu, không mặt người, không chữ."""
    _, canh = BOI_CANH.get(boi_canh) or BOI_CANH[BOI_CANH_MAC_DINH]
    _, ban_tay = TAY.get(tay) or TAY["nu"]
    _, viec = _HD.get(hanh_dong) or _HD["cam_len"]
    return (
        f"Vertical 9:16 top-down video from a fixed overhead camera on a tripod, looking straight down at {canh}. "
        "The camera never moves, tilts or zooms for the whole clip. "
        f"Only {ban_tay} are in frame, holding the exact product shown in the attached reference image. {viec} "
        "The product must match the reference image exactly: same shape, same colours, same logo and printed text. "
        "Do not redesign it, do not add or change any text on it. "
        "HANDS ONLY: no face, no head, no torso, no other people, no mirror and no reflection of a person. "
        "Clean real footage, soft diffused even daylight with no harsh shadows, calm unhurried hand movement, "
        "realistic skin texture, the product stays sharp and fully inside the frame for the whole clip. "
        # Phần tư trên dành cho thẻ bài Threads, phần năm dưới dành cho phụ đề: sản phẩm phải nằm ở dải giữa khung hình.
        "Framing: the product stays in the middle band of the frame, not too close to the lens; "
        "the top quarter and the bottom fifth of the frame show only background or hands, never the product. "
        "No on-screen text, no subtitles, no captions, no watermark, no talking, no voice-over, quiet room sound only."
    )


def lap_canh(da: Dict) -> List[Dict]:
    """Chia số giây của video thành các clip. Clip đã quay xong và không đổi gì thì giữ lại."""
    giay = int(da.get("giay_clip") or 10)
    giay = giay if giay in (GIAY_FLOW if da.get("may_clip") == "flow" else GIAY_CLIP) else 10
    so = max(1, -(-int(da.get("so_giay") or SO_GIAY) // giay))
    cu = {c["ma"]: c for c in da.get("canh") or []}
    ra = []
    for i in range(so):
        ma = f"c{i + 1:02d}"
        truoc = cu.get(ma) or {}
        # Động tác đi theo trình tự của loại sản phẩm. `lech` (video cũ còn ghi) xoay điểm bắt đầu của trình tự.
        cac = trinh_tu(da)
        hd = truoc.get("hanh_dong") if truoc.get("hanh_dong") in _HD else cac[(i + int(da.get("lech") or 0)) % len(cac)]
        lenh = truoc["prompt"] if truoc.get("tu_sua") and truoc.get("prompt") else cau_lenh(hd, da.get("boi_canh"), da.get("tay"))
        # Clip đã quay được giữ khi các LỰA CHỌN của nó không đổi (động tác, bối cảnh, bàn tay, số giây, máy). Không so chữ
        # của câu lệnh: mẫu câu lệnh được sửa dần, so chữ thì mỗi lần sửa mẫu là mọi clip cũ bị coi như chưa quay.
        giu = (truoc.get("trang_thai") == "xong" and truoc.get("hanh_dong") == hd and truoc.get("giay") == giay
               and truoc.get("bc", da.get("boi_canh")) == da.get("boi_canh") and truoc.get("ty", da.get("tay")) == da.get("tay")
               and truoc.get("may", "muse") == (da.get("may_clip") or "muse")
               and (not truoc.get("tu_sua") or truoc.get("prompt") == lenh))
        if giu:
            lenh = truoc.get("prompt") or lenh  # giữ đúng câu lệnh đã dùng để quay clip này
        ra.append({"ma": ma, "hanh_dong": hd, "prompt": lenh, "giay": giay, "tu_sua": bool(truoc.get("tu_sua")), **({"lenh_tho": True} if truoc.get("lenh_tho") else {}), **({"khong_sp": True} if truoc.get("khong_sp") else {}), **({"moi": True} if truoc.get("moi") else {}), **({"anh_ep": truoc["anh_ep"]} if truoc.get("anh_ep") else {}), **({"co_chat": True} if truoc.get("co_chat") else {}), "may": da.get("may_clip") or "muse",
                   "cc": truoc.get("cc", ""), "flow_model": truoc.get("flow_model", ""),
                   "bc": da.get("boi_canh"), "ty": da.get("tay"),
                   "trang_thai": "xong" if giu else "cho", "file": truoc.get("file", "") if giu else "",
                   "loi": "", "ban": int(truoc.get("ban") or 0), "tranh": truoc.get("tranh", ""),
                   # Clip giữ nguyên thì giữ cả kết quả AI soát, ảnh mẫu đã dùng, và dấu nối khung (trước đây mất sau mỗi lần lưu).
                   **({k: truoc[k] for k in ("soat", "quay_lai", "anh_mau", "noi_tu", "doan_tot", "doan_tu_dong", "tu_kho", "luu_kho") if k in truoc} if giu else {})})
    da["canh"] = ra
    return ra


# ── Lời kể ───────────────────────────────────────────────────────────────────

def so_tieng_can(so_giay: int, giong: str = "") -> int:
    """Số tiếng lời kể vừa `so_giay`, theo tốc độ đọc đã đo của giọng (chưa đo thì 4,2 tiếng/giây)."""
    return int((so_giay - 1.5) * pc.toc_do(giong))


def viet_loi(da: Dict) -> Dict:
    bai = da.get("bai") or {}
    if not (bai.get("noi_dung") or "").strip():
        raise ValueError("Chưa chọn bài Threads nào để kể.")
    can = so_tieng_can(int(da.get("so_giay") or SO_GIAY), da.get("giong") or "")
    de = (f"BÀI GỐC:\n{bai['noi_dung']}\n\n"
          f"Viết lại thành lời kể dài đúng khoảng {can} tiếng (đếm theo từng tiếng tiếng Việt, cho phép lệch 8%), "
          "đọc vừa một video " + str(da.get("so_giay") or SO_GIAY) + " giây. Bài gốc dài hơn thì chọn phần gay cấn nhất và kể trọn, "
          "bài gốc ngắn hơn thì kể chậm lại, thêm cảm nghĩ của người kể chứ không thêm sự việc.\n"
          "Chọn thêm các đoạn chép nguyên văn từ bài gốc để đính lên hình, mỗi đoạn dài 10 tới 24 tiếng, xếp theo đúng thứ tự "
          "câu chuyện: bài ngắn thì MỘT đoạn gây sốc nhất; bài dài thì 2 tới 4 đoạn, mỗi đoạn là một bước ngoặt, để người xem "
          "đọc thẻ này xong thì thẻ sau hiện ra.\n"
          'Trả về: {"loi_ke": "...", "cac_doan": ["...", "..."]}')
    import hoc_so_lieu
    de += hoc_so_lieu.loi_dan(XUONG)
    # Lời kể và các đoạn đính lên hình đều phải qua bộ soát luật nói của nhãn hàng (luat_nhan_hang).
    import kho_anh
    import luat_nhan_hang
    de += "\n" + kho_anh.luat_nhan_hang()
    tot, nhac, vi_pham = None, "", []
    for luot in range(4):
        them = nhac if nhac else ("" if not tot else f"\nLượt trước dài {pc.so_tieng(tot['loi_ke'])} tiếng, phải gần {can} tiếng hơn.")
        got = pc.hoi_ai(_VIET_HE, de + them)
        loi = str(got.get("loi_ke") or "").strip()
        if not loi:
            continue
        vi_pham = luat_nhan_hang.soat(loi + "\n" + "\n".join(str(x) for x in (got.get("cac_doan") or [got.get("doan_dinh")]) if x), ke_chuyen=True)
        nhac = luat_nhan_hang.loi_nhac(vi_pham) if vi_pham else ""
        if vi_pham:
            continue
        if tot is None or abs(pc.so_tieng(loi) - can) < abs(pc.so_tieng(tot["loi_ke"]) - can):
            doan = [str(x).strip()[:220] for x in (got.get("cac_doan") or [got.get("doan_dinh")]) if str(x or "").strip()][:SO_THE_TOI_DA]
            tot = {"loi_ke": loi, "cac_doan": doan}
        if abs(pc.so_tieng(tot["loi_ke"]) - can) <= can * 0.1:
            break
    if not tot:
        if vi_pham:
            raise RuntimeError("Lời AI viết vẫn sai luật nói sau 4 lượt: " + "; ".join(vi_pham[:3]))
        raise RuntimeError("AI trả về lời kể rỗng.")
    da["loi_ke"] = tot["loi_ke"]
    doan = tot["cac_doan"] or [bai["noi_dung"][:180]]
    da["the"] = {"tac_gia": bai.get("tac_gia") or "", "chu": doan[0], "cac_doan": doan}
    da["giong_doc"] = None
    return da


# ── Giọng, clip, dựng ────────────────────────────────────────────────────────

def _dau_loi(da: Dict) -> str:
    return hashlib.sha1(f"{da.get('giong')}|{da.get('so_giay')}|{da.get('loi_ke')}".encode("utf-8")).hexdigest()[:12]


def doc_loi(da: Dict, viec: Optional[Dict] = None) -> Dict:
    if not (da.get("loi_ke") or "").strip():
        raise ValueError("Chưa có lời kể.")
    dich = os.path.join(_thu_muc(da["id"]), "giong.wav")
    if (da.get("giong_doc") or {}).get("dau") == _dau_loi(da) and os.path.isfile(dich):
        return da
    if viec:
        pc.buoc(viec, None, "Đang đọc lời kể bằng giọng nhân bản")
    got = pc.doc_giong(da.get("giong") or "", da["loi_ke"], dich, toi_da=float(da.get("so_giay") or SO_GIAY) - 0.5)
    da["giong_doc"] = {**got, "dau": _dau_loi(da), "file": "giong.wav"}
    return luu(da)


def _anh_mau(da: Dict) -> List[str]:
    goc = os.path.join(_thu_muc(da["id"]), "anh")
    return [os.path.join(goc, a) for a in (da.get("san_pham", {}).get("anh") or [])[:3] if os.path.isfile(os.path.join(goc, a))]


# Ảnh mẫu theo cảnh (sản phẩm có bộ ảnh chuẩn, xem bo_anh_sp.py): động tác nào thì ưu tiên ảnh mang tag cảnh nào.
CANH_CUA_DONG_TAC = {
    "cam_len": ("cầm hộp", "mặt hộp"), "xoay": ("mặt hộp", "cầm hộp"), "mo_hop": ("mở hộp", "hộp và sản phẩm"),
    "bay_ra": ("bên trong", "hộp và sản phẩm"), "khoe_mot": ("sản phẩm", "bên trong"), "chi_logo": ("mặt hộp", "cầm hộp"),
    "dat_xuong": ("hộp và sản phẩm", "mặt hộp"), "hai_tay": ("cầm hộp", "sản phẩm"), "rut_tay": ("hộp và sản phẩm", "bên trong"),
    "nan_ra": ("nặn ra", "mở hộp"), "boi_tay": ("chất sản phẩm", "nặn ra"), "khoe_chat": ("chất sản phẩm", "nặn ra"),
    "lac_nhe": ("sản phẩm", "cầm hộp"), "bo_tui": ("sản phẩm", "cầm hộp"), "gio_len": ("cầm hộp", "sản phẩm"),
    "thich": ("cầm hộp", "mặt hộp"),
}
# Clip có câu lệnh viết sẵn (theo mẫu đã soi) thì mã động tác không nói đúng việc trong cảnh: đọc chính câu lệnh.
_CANH_THEO_CHU = (
    (re.compile(r"squeez|pump|dispens", re.I), "nặn ra"),
    (re.compile(r"spread|rub|smear|texture|between (the )?fingers", re.I), "chất sản phẩm"),
    (re.compile(r"pour|lays? out|tips? out|spill|pieces|pods|sachets|packets|in a row", re.I), "bên trong"),
    (re.compile(r"\bopen|\blid\b|unbox|\bcap\b", re.I), "mở hộp"),
)


def _ho_so(da: Dict) -> Dict:
    """Thông tin của sản phẩm trong Kho ảnh (đoạn tả `nhan_dien`, chất `chat`) nếu video lấy ảnh từ kho."""
    ten = (da.get("san_pham") or {}).get("kho")
    if not ten:
        return {}
    import kho_anh
    return kho_anh.thong_tin(ten)


def _anh_ep(da: Dict, canh: Dict) -> List[str]:
    """
    Ảnh mẫu clip tự soạn chọn sẵn (`canh["anh_ep"]`: một tên hoặc danh sách tên trong `anh/` của video). 10/11/2026 người dùng dặn KHÔNG
    gửi ảnh chụp chung tuýp + hộp (máy vẽ tuýp nằm sẵn ngoài hộp, hộp đổi màu): cảnh cần hai vật thì gửi hai ảnh riêng, mỗi ảnh một vật.
    """
    ten = canh.get("anh_ep") or []
    ten = [ten] if isinstance(ten, str) else list(ten)
    goc = os.path.join(_thu_muc(da["id"]), "anh")
    return [os.path.join(goc, t) for t in ten if t and os.path.isfile(os.path.join(goc, t))]


def _anh_cho_canh(da: Dict, canh: Dict) -> Optional[str]:
    """Ảnh mẫu đúng cảnh của một clip, trong các ảnh của video. Không có ảnh gắn tag cảnh thì trả None (dùng ảnh chính)."""
    cac = [a for a in (da.get("san_pham") or {}).get("anh") or [] if os.path.isfile(os.path.join(_thu_muc(da["id"]), "anh", a))]
    ep = _anh_ep(da, canh)
    if ep:
        return ep[0]
    if da.get("anh_mau_co_dinh") in cac:
        # 06/11/2026: ảnh tag "sản phẩm" của Pleasure Gel là MẶT SAU tuýp (mã vạch, "100 ml"), ảnh "chất sản phẩm" không có
        # tuýp: máy làm clip chép mặt sau lên mặt trước hoặc tự bịa tuýp. Video chọn một ảnh mẫu cố định cho mọi clip.
        return os.path.join(_thu_muc(da["id"]), "anh", da["anh_mau_co_dinh"])
    if not (da.get("san_pham") or {}).get("kho") or not cac:
        return None
    import kho_anh
    tag = kho_anh.tag_cua_tep(cac)
    cac = [a for a in cac if kho_anh.TAG_MAT_SAU not in tag.get(a, [])]  # ảnh mặt sau không bao giờ làm mẫu
    can = list(CANH_CUA_DONG_TAC.get(canh.get("hanh_dong"), ()))
    if canh.get("tu_sua"):
        can = [t for m, t in _CANH_THEO_CHU if m.search(canh.get("prompt") or "")][:1] + can
    if da.get("may_clip") in ("gemini", "kling"):
        # 08/11/2026: Gemini chép chữ trên bao bì theo ẢNH. Ảnh cảnh nặn gel không thấy nhãn tuýp nên nó tự bịa chữ ("GEL" thừa,
        # "PLASURE"); cảnh dùng ảnh tuýp mặt trước đứng cạnh hộp thì chữ đều đúng. Gemini luôn nhận ảnh thấy rõ nhãn.
        can = ["hộp và sản phẩm", "mặt hộp"]
    elif "PRODUCT ONLY" in (canh.get("prompt") or ""):
        # 07/11/2026: cảnh chỉ quay sản phẩm (concept, "SP:"): ảnh sản phẩm đứng cạnh hộp, không có tay trong ảnh mẫu.
        can = ["hộp và sản phẩm", "mặt hộp"]
    for t in can + ["cầm hộp", "mặt hộp"]:
        tep = next((a for a in cac if t in tag.get(a, [])), None)
        if tep:
            return os.path.join(_thu_muc(da["id"]), "anh", tep)
    return None


# Cảnh có chất sản phẩm lộ ra (nặn, bơm, bôi, khoe chất). Các cảnh khác sản phẩm phải đóng kín.
DONG_TAC_CO_CHAT = ("nan_ra", "boi_tay", "khoe_chat")
_CO_CHAT = re.compile(r"squeez|pump|dispens|spread|rub|smear|lather|massag|texture|coated|drip|dollop|blob|"
                      r"onto (?:the |her |a )?(?:palm|hand|finger)|between (?:the )?fingers", re.I)
# Mẫu quảng cáo hay quay nhiều chai cùng lúc; PlayAh chỉ có MỘT chai, chữ số nhiều làm máy vẽ thêm chai.
_SO_NHIEU = re.compile(r"\b(?:the |these |several |multiple |two |three )?(bottles|tubes|jars|containers|boxes|packs)\b", re.I)


def co_chat(canh: Dict) -> bool:
    """Clip này có cho thấy chất bên trong (gel, bọt) không."""
    if canh.get("co_chat") is True:
        return True
    if canh.get("tu_sua"):
        return bool(_CO_CHAT.search(canh.get("prompt") or ""))
    return canh.get("hanh_dong") in DONG_TAC_CO_CHAT or bool(_CO_CHAT.search(canh.get("prompt") or ""))


_TAY_CAM = re.compile(r"\bhands?\b[^.]{0,80}\b(holds?|holding|squeez\w*|picks? up|lifts?)\b", re.I)
GIU_TAY = " The hand is in frame from the first to the last frame and never leaves it; the product never stands alone without the hand."


def _giu_tay(lenh: str) -> str:
    """10/11/2026: 3 trên 7 video Muse ra cảnh sản phẩm đứng một mình dù câu lệnh bảo tay cầm. Câu lệnh có tay cầm thì dặn tay ở suốt clip."""
    if (_TAY_CAM.search(lenh) and "leaves the frame" not in lenh and GIU_TAY.strip() not in lenh
            and not re.search(r"\bno hands?\b|\bwithout (a |any )?hands?\b", lenh, re.I)):
        return lenh.rstrip() + GIU_TAY
    return lenh


def lenh_gui(da: Dict, canh: Dict) -> str:
    """
    Câu lệnh thật gửi máy làm clip: câu lệnh của clip, cộng đoạn tả cố định của sản phẩm nếu kho có (bộ ảnh chuẩn).

    Sản phẩm có chất bên trong (`chat`: gel, bọt). 05/11/2026 máy làm clip vẽ gel tự chảy ra từ GIỮA THÂN tuýp ở một cảnh chỉ
    quay tuýp đứng yên: đoạn tả gel được nối vào mọi clip nên máy cố cho gel xuất hiện, mà không biết gel ra từ đâu. Nên:
    cảnh không có chất thì bỏ câu tả chất và dặn sản phẩm đóng kín; cảnh có chất thì dặn chất CHỈ ra từ miệng dưới nắp.
    """
    if canh.get("lenh_tho"):
        # 08/11/2026: câu lệnh ngắn người dùng đã duyệt, gửi NGUYÊN VĂN (không nối đoạn tả bao bì, lệnh cấm dài ~2.000 ký tự).
        return _giu_tay(canh.get("prompt") or "")
    lenh, hs = canh.get("prompt") or "", _ho_so(da)
    if hs.get("chat"):
        lenh = re.sub(r"\bclear gel\b", hs["chat"], lenh)
        lenh = _SO_NHIEU.sub("the product", lenh)
    if hs.get("nhan_dien"):
        ta = hs["nhan_dien"].strip()
        if hs.get("chat"):
            ten_chat = hs["chat"].split()[-1]  # "perfectly clear glossy gel" -> gel
            cau = [c for c in re.split(r"(?<=\.)\s+", ta) if c]
            chi_chat = [c for c in cau if re.match(rf"(?:The|Its) {re.escape(ten_chat)}\b", c)]
            if co_chat(canh):
                ta += (f" The {ten_chat} comes out ONLY from the nozzle at the cap end (the flip-top opening or the pump spout), with the cap open or removed. "
                       f"The container body is sealed: no {ten_chat} ever seeps from, leaks through or appears on the side of "
                       f"the tube or bottle. There is exactly one container in the shot.")
            else:
                ta = " ".join(c for c in cau if c not in chi_chat)
                ta += (f" In this shot the product stays closed with the cap on: no {ten_chat}, no liquid and no drops "
                       f"anywhere, not on the container, not on the hands. There is exactly one container in the shot.")
        lenh += " PRODUCT DETAILS, keep exactly as described and as in the reference image: " + ta
        # 05/11/2026: cảnh chỉ quay hộp mà đoạn tả có cả hộp lẫn tuýp, máy cho tuýp tự hiện ra giữa clip. Nói rõ cảnh này
        # quay vật nào, và cấm vật tự biến hình, tự xuất hiện.
        if _chi_hop(canh):
            lenh += (" In this shot only the closed outer pack (the box) is shown: it stays closed and nothing else is in the "
                     "frame; the item inside stays inside, unseen.")
        lenh += (" OBJECT CONTINUITY: every object keeps its identity for the whole shot. Nothing morphs into another object, "
                 "nothing appears out of thin air, nothing duplicates; an item only comes out of the pack if the hands "
                 "visibly open it and take it out on camera.")
        # 06/11/2026: các lỗi máy làm clip lặp đi lặp lại trong 30 clip Pleasure Gel (AI soát và người dùng bắt được). Dặn cấm
        # cho mọi sản phẩm có đoạn tả; lỗi riêng của một bao bì thì nằm trong chính đoạn tả `nhan_dien` đó.
        lenh += LOI_CAM_CHUNG
    if da.get("an_chu_nhan"):
        # 06/11/2026: Veo 3.1 từ chối mọi clip Pleasure Gel ("Audio generation failed"), kể cả cảnh không có gel. Thử bỏ chữ in
        # trên nhãn ('PLEASURE GEL', 'PLAYAH!'...) khỏi câu lệnh; ảnh mẫu vẫn giữ đúng nhãn.
        lenh = re.sub(r"\ba (?:black |white |gold |red )?(?<![A-Za-z])'[^']{2,40}' (logo|tag)\b", r"a small \1", lenh)
        lenh = re.sub(r"(?:black |white |gold |red )?(?<![A-Za-z])'[^']{2,40}'", "the product name", lenh)
    if (canh.get("tranh") or "").strip():
        # Lần quay lại sau khi AI soát báo lỗi: dặn tránh đúng lỗi đó.
        lenh += " MUST AVOID (a previous take had these mistakes): " + canh["tranh"].strip()
    return lenh


LOI_CAM_CHUNG = (
    " STRICT RULES: the packaging shows ONLY the text described above, spelled exactly, at the described size; never add "
    "volume text like '100 ml', never repeat a word, never enlarge the product name; never show the back of the pack. "
    "Hands keep five normal fingers, never merge, bend unnaturally or vanish; no extra hand or arm ever enters. Every object "
    "stays the same: nothing vanishes, reappears, duplicates or changes shape (the cap, the gel, a tissue, the box); an open "
    "flip-top cap stays open unless a finger closes it on camera; the gel never disappears from a hand by itself.")


def _chi_hop(canh: Dict) -> bool:
    """Clip chỉ quay cái hộp ngoài: ảnh mẫu là ảnh mặt hộp / cầm hộp và câu lệnh không có cảnh mở, lấy ra hay chất."""
    if co_chat(canh) or _CANH_THEO_CHU[2][0].search(canh.get("prompt") or "") or _CANH_THEO_CHU[3][0].search(canh.get("prompt") or ""):
        return False
    if not canh.get("tu_sua") and canh.get("hanh_dong") not in ("cam_len", "xoay", "chi_logo", "thich", "gio_len"):
        return False
    import kho_anh
    tag = kho_anh.tag_cua_tep([canh.get("anh_mau") or ""]).get(canh.get("anh_mau") or "", [])
    return bool({"mặt hộp", "cầm hộp"} & set(tag))


def loi_dan_muse(da: Dict, canh: Dict) -> str:
    """
    Muse là trợ lý có trí nhớ: 05/11/2026 nó nhận ra câu lệnh giống lần làm ngày 13/10 và GỬI LẠI FILE CŨ thay vì quay mới
    ("làm xong từ tối 13/10 rồi nên mình không render lại"); trong cùng một video nó cũng gửi lại clip vừa làm cho clip sau.
    Nên mỗi lần gửi kèm một mã riêng và dặn thẳng là phải quay mới.
    """
    # Mã chỉ là chuỗi ngẫu nhiên: mã có tên video và số clip thì Muse dùng chính nó để nhận ra "clip đã từ chối" (05/11).
    ma = uuid.uuid4().hex[:10]
    return (f" NEW RENDER REQUIRED (request {ma}): generate a brand-new video for this exact request now. Do not reuse, resend "
            f"or link any video made earlier, even if an earlier request looks the same or similar.")


# AI soát clip (soat_clip.py): clip lỗi rõ thì quay lại tối đa chừng này lần, mỗi lần kèm lời dặn tránh đúng lỗi đó.
# 08/11/2026: người dùng dặn soát và tự quay lại tốn credit thật (06/11 mất cả đêm và ~1050 credit), phải siết câu lệnh thay vì
# soát rồi quay lại. Mặc định: KHÔNG tự soát, KHÔNG tự quay lại. Video nào muốn thì tự bật (`tu_soat`, `so_lan_quay_lai`).
SO_LAN_QUAY_LAI = 0
TU_SOAT_MAC_DINH = False


def _soat_mot(da: Dict, canh: Dict) -> Dict:
    import soat_clip
    goc = os.path.join(_thu_muc(da["id"]), "anh")
    that = [os.path.join(goc, canh["anh_mau"])] if canh.get("anh_mau") else []
    that += [a for a in _anh_mau(da)[:1] if a not in that]
    kq = soat_clip.soat(os.path.join(_thu_muc(da["id"]), "clip", canh["file"]), that, _ho_so(da).get("nhan_dien") or "",
                        canh.get("prompt") or "")
    return {**kq, "file": canh["file"], "luc": time.time()}


# Dựng cắt nhanh chỉ lấy vài đoạn khoảng 2 giây mỗi clip. Clip AI soát báo lỗi mà vẫn còn từng này giây sạch thì dùng các đoạn
# sạch, không quay lại (đỡ credit và thời gian; trước 07/11/2026 việc chọn đoạn sạch phải gõ tay từng clip).
GIAY_SACH_DU = 3.0


def _gan_doan_sach(da: Dict, canh: Dict, kq: Dict) -> None:
    """Ghi các đoạn sạch tự tính vào clip (`doan_tot`, gắn với đúng file). Đoạn người dùng tự đặt thì để nguyên."""
    if canh.get("doan_tot") and not canh.get("doan_tu_dong"):
        return
    canh.pop("doan_tot", None)
    canh.pop("doan_tu_dong", None)
    if kq.get("dat") is not False or not da.get("cat_nhanh"):
        return
    sach = kq.get("doan_sach") or []
    if sum(b - a for a, b in sach) >= GIAY_SACH_DU and any(b - a >= 1.6 for a, b in sach):
        canh.update(doan_tot=sach, doan_tu_dong=canh.get("file"))
        kq["dung_doan_sach"] = True


def quay(da: Dict, viec: Dict, chi: Optional[str] = None) -> Dict:
    """
    Quay các clip chưa xong (hoặc đúng một clip `chi`), rồi cho AI soát từng clip vừa quay (`tu_soat`, mặc định bật). Clip
    lỗi rõ thì quay lại, tối đa `SO_LAN_QUAY_LAI` lần; vẫn lỗi thì giữ bản cuối và ghi lỗi vào `canh["soat"]` để trang báo.
    """
    if da.get("may_clip") == "flow" and da.get("noi_canh", True):
        return _quay_noi(da, viec, chi)
    _quay_mot_luot(da, viec, [chi] if chi else None)
    if not da.get("tu_soat", TU_SOAT_MAC_DINH) or viec.get("huy"):
        return da
    vua = {c["ma"] for c in da.get("canh") or [] if (not chi or c["ma"] == chi)}
    toi_da = int(da.get("so_lan_quay_lai", SO_LAN_QUAY_LAI))  # video có thể đặt ít lần quay lại hơn cho đỡ tốn credit
    for lan in range(toi_da + 1):
        hong = []
        for canh in da["canh"]:
            if canh["ma"] not in vua or canh.get("trang_thai") != "xong" or (canh.get("soat") or {}).get("file") == canh.get("file"):
                continue
            pc.buoc(viec, None, f"AI đang soát clip {canh['ma'][1:]}")
            kq = _soat_mot(da, canh)
            canh["soat"] = kq
            _gan_doan_sach(da, canh, kq)
            if kq["dat"] is False and not canh.get("doan_tot"):
                canh.setdefault("quay_lai", []).append({"file": canh["file"], "loi": kq["loi"]})
                hong.append(canh)
            luu(da)
        if not hong or lan == toi_da or viec.get("huy"):
            break
        for canh in hong:
            canh.update(trang_thai="cho", tranh=kq_sua(canh))
        luu(da)
        pc.buoc(viec, None, f"Quay lại {len(hong)} clip AI báo lỗi (lần {lan + 1}/{toi_da})")
        _quay_mot_luot(da, viec, [c["ma"] for c in hong])
    return da


# ── Nối cảnh (06/11/2026) ────────────────────────────────────────────────────
# Người dùng: "chuyển cảnh không mượt, chỉ là gắn hiệu ứng giữa clip 1 và clip 2". Mỗi clip quay riêng nên chỗ nối nhảy hẳn cảnh
# (tay dính gel → hộp còn đóng đứng một mình). Nay với Flow: quay LẦN LƯỢT, clip sau dùng khung cuối của clip trước làm khung
# mở đầu (chế độ Frames của Flow), nên cả video là một cảnh liền. Clip đầu vẫn quay theo ảnh mẫu (Ingredients). Clip nào AI
# soát báo lỗi thì quay lại NGAY trước khi quay clip sau, để clip sau nối vào bản cuối cùng. Chậm hơn quay song song (mỗi
# clip phải chờ clip trước). Tắt bằng `noi_canh=False`.
LOI_NOI = ("CONTINUITY: this clip continues the previous shot. It starts exactly on the provided start frame (same hands, same "
           "sleeves, same product in the same position and state, same surface, same camera angle and lighting) and continues the "
           "motion smoothly from there, with no cut and no jump. Do not reset the product to an earlier state.")
LOI_NOI_CUOI = ("CONTINUITY: the next shot is already filmed. This clip must END exactly on the provided end frame (same hands, same "
                "product position and state, same surface, camera angle and lighting), reaching it through natural motion.")


def khung_dau(clip: str, dich: str) -> str:
    """Khung hình đầu của một clip, lưu thành PNG."""
    pc.chay([pc.ffmpeg(), "-y", "-ss", "0", "-i", clip, "-frames:v", "1", "-update", "1", dich])
    if not os.path.isfile(dich):
        raise RuntimeError("Không cắt được khung đầu của clip sau.")
    return dich


# 08/11/2026: bản đầu gửi khung cuối làm ảnh THỨ NHẤT: Muse lấy cái tuýp nhỏ, mờ trong khung đó làm mẫu và vẽ lại nhãn sai từ cảnh 2
# trở đi (4/4 video). Nay ảnh sản phẩm thật đứng đầu, khung cuối đứng sau chỉ để giữ bối cảnh.
LOI_NOI_MUSE = ("Image 1 shows the exact product: the tube must match image 1 exactly (same label, same colour, same shape). Image 2 is "
                "the last frame of the previous shot: continue exactly from it, same room, same surface, same light, same objects in the "
                "same places. ")


LOI_NOI_KHONG_SP = ("The attached image is the last frame of the previous shot: continue exactly from it, same room, same surface, same "
                    "light, same objects in the same places. ")


def khung_cuoi(clip: str, dich: str) -> str:
    """Khung hình cuối của một clip, lưu thành PNG."""
    pc.chay([pc.ffmpeg(), "-y", "-sseof", "-0.12", "-i", clip, "-frames:v", "1", "-update", "1", dich])
    if not os.path.isfile(dich):
        raise RuntimeError("Không cắt được khung cuối của clip trước.")
    return dich


def _quay_noi(da: Dict, viec: Dict, chi: Optional[str] = None) -> Dict:
    mau = _anh_mau(da)
    if not mau:
        raise ValueError("Chưa có ảnh sản phẩm. Clip quay tay cần ảnh sản phẩm làm mẫu.")
    if not da.get("canh"):
        lap_canh(da)
        luu(da)
    thu_muc = os.path.join(_thu_muc(da["id"]), "clip")
    truoc = None  # clip đã xong đứng ngay trước
    for i, canh in enumerate(da["canh"]):
        lam = canh["ma"] == chi if chi else canh.get("trang_thai") != "xong"
        if not lam:
            truoc = canh if canh.get("trang_thai") == "xong" else None
            continue
        if viec.get("huy"):
            break
        # Clip ngay sau đã quay xong (quay lại một clip giữa chừng): clip này phải KẾT THÚC đúng ở khung đầu của clip sau.
        sau = da["canh"][i + 1] if i + 1 < len(da["canh"]) else None
        sau = sau if sau and sau.get("trang_thai") == "xong" and sau.get("file") else None
        toi_da = int(da.get("so_lan_quay_lai", SO_LAN_QUAY_LAI))  # trước 08/11 chỗ này bỏ qua số lần của video, luôn quay lại 2 lần
        for lan in range(toi_da + 1):
            dau = cuoi = None
            if truoc and truoc.get("file"):
                dau = khung_cuoi(os.path.join(thu_muc, truoc["file"]), os.path.join(thu_muc, f"{truoc['file'][:-4]}_cuoi.png"))
            if sau:
                cuoi = khung_dau(os.path.join(thu_muc, sau["file"]), os.path.join(thu_muc, f"{sau['file'][:-4]}_dau.png"))
            pc.buoc(viec, round(100 * i / len(da["canh"])),
                    f"Đang quay clip {canh['ma'][1:]}/{len(da['canh'])}" + (" nối tiếp clip trước" if dau else "")
                    + (" khớp vào clip sau" if cuoi else "") + (f" (lần {lan + 1})" if lan else ""))
            anh = _anh_cho_canh(da, canh) or mau[0]
            canh["anh_mau"] = os.path.basename(anh)
            canh["noi_tu"] = truoc["file"] if dau else ""
            _quay_flow(da, viec, [canh], [anh], dau=[dau], cuoi=[cuoi])
            if viec.get("huy"):
                break
            if canh.get("trang_thai") != "xong":
                # Flow hỏng ngẫu nhiên ("Audio generation failed"...): thử lại ngay, kẻo clip sau mất chỗ nối.
                cung = any(t in (canh.get("loi") or "").lower() for t in ("credit", "đăng nhập", "login"))
                if lan < toi_da and not cung:  # hết credit, chưa đăng nhập thì thử lại cũng vô ích
                    pc.buoc(viec, None, f"Clip {canh['ma'][1:]} hỏng phía Flow, thử lại")
                    continue
                break
            if sau:
                sau["noi_tu"] = canh["file"]
                luu(da)
            if not da.get("tu_soat", TU_SOAT_MAC_DINH):
                break
            pc.buoc(viec, None, f"AI đang soát clip {canh['ma'][1:]}")
            kq = _soat_mot(da, canh)
            canh["soat"] = kq
            if kq["dat"] is not False or lan >= toi_da:
                luu(da)
                break
            canh.setdefault("quay_lai", []).append({"file": canh["file"], "loi": kq["loi"]})
            canh.update(trang_thai="cho", tranh=kq_sua(canh))
            luu(da)
        if canh.get("trang_thai") != "xong":
            truoc = None  # clip này hỏng: clip sau quay theo ảnh mẫu, không nối
        else:
            truoc = canh
        if chi:
            break
    return da


def kq_sua(canh: Dict) -> str:
    """Lời dặn tránh lỗi cho lần quay lại: gộp lời sửa AI soát đưa ra qua các lần."""
    cu = (canh.get("tranh") or "").strip()
    moi = ((canh.get("soat") or {}).get("sua") or "").strip()
    return (cu + " " + moi).strip()[-900:] if moi and moi not in cu else cu


def _quay_mot_luot(da: Dict, viec: Dict, cac_ma: Optional[List[str]] = None) -> Dict:
    """Một lượt quay bằng máy làm clip. Lưu sau từng clip nên dừng giữa chừng không mất."""
    mau = _anh_mau(da)
    if not mau:
        raise ValueError("Chưa có ảnh sản phẩm. Clip quay tay cần ảnh sản phẩm làm mẫu.")
    if not da.get("canh"):
        lap_canh(da)
        luu(da)
    can = [c for c in da["canh"] if (c["ma"] in cac_ma if cac_ma else c.get("trang_thai") != "xong")]
    if not can:
        return da
    # Mỗi clip một ảnh mẫu đúng cảnh của nó (có bộ ảnh chuẩn), không thì ảnh chính như cũ.
    anh_canh = {c["ma"]: _anh_cho_canh(da, c) or mau[0] for c in can}
    for c in can:
        c["anh_mau"] = os.path.basename(anh_canh[c["ma"]])
        c.pop("tu_kho", None)  # quay mới thì không còn là cảnh lấy từ kho, cũng chưa được cất
        c.pop("luu_kho", None)
    if da.get("may_clip") == "flow":
        # Cảnh mở hộp của đề cương cần cả ảnh hộp và ảnh tuýp, mỗi ảnh chụp một vật.
        # Các cảnh cũ vẫn truyền một đường dẫn như trước.
        return _quay_flow(da, viec, can, [ep if len(ep := _anh_ep(da, c)) > 1 else anh_canh[c["ma"]] for c in can])
    if da.get("may_clip") == "gemini":
        return _quay_gemini(da, viec, can, anh_canh)
    if da.get("may_clip") == "kling":
        return _quay_kling(da, viec, can, anh_canh)
    muse_dir = str(Path(pc.BASE_DIR) / "muse-controller")
    if muse_dir not in sys.path:
        sys.path.insert(0, muse_dir)
    thu_muc = os.path.join(_thu_muc(da["id"]), "clip")
    os.makedirs(thu_muc, exist_ok=True)

    # Quay cùng lúc (15/11/2026, người dùng: "chạy 3 lệnh khác nhau cho 1 clip hoàn chỉnh", mục đích tốc độ): mỗi cảnh một CỬA SỔ
    # Chrome riêng của cùng hồ sơ Muse, mỗi cửa sổ một chat. Đo 15/11: 2 clip cùng lúc về ở giây 87 và 99, đúng lệnh, không lẫn.
    # Cửa sổ riêng chứ không phải tab: Chrome làm chậm tab nền (ý từ muse2api). Cảnh nối khung cuối của cảnh trước thì vẫn lần lượt.
    noi_canh = bool(da.get("noi_canh_muse")) and any(not c.get("moi") for c in can)
    so_cua = 1 if noi_canh else max(1, min(int(da.get("muse_cung_luc") or MUSE_CUNG_LUC), MUSE_CUNG_LUC_TOI_DA, len(can)))

    async def go():
        from muse import MuseAIStudio, VideoJob
        studio = MuseAIStudio()

        async def quay_canh(studio, i, canh):
            if viec.get("huy"):
                return
            # Clip tự soạn không có mã động tác; clip `lenh_tho` (câu lệnh gửi nguyên văn, ví dụ kich_ban_aff) có mã điền cho đủ
            # nhưng không dùng: 14/11/2026 nhãn ghi "Cầm lên khoe mặt trước" trong khi cảnh không có tay.
            ten = "cảnh tự soạn" if canh.get("lenh_tho") else (_HD.get(canh.get("hanh_dong") or "") or ("cảnh tự soạn",))[0]
            if so_cua > 1:
                pc.buoc(viec, round(100 * sum(c.get("trang_thai") in ("xong", "loi") for c in can) / len(can)),
                        f"Đang quay {so_cua} clip cùng lúc, clip {canh['ma'][1:]} ({ten}) vừa bắt đầu")
            else:
                pc.buoc(viec, round(100 * i / len(can)), f"Đang quay clip {canh['ma'][1:]} ({ten}), còn {len(can) - i} clip")
            canh.update(trang_thai="dang", loi="")
            luu(da)
            try:
                rieng = _anh_ep(da, canh) or ([anh_canh[canh["ma"]]] + [a for a in mau if a != anh_canh[canh["ma"]]])
                noi = ""
                if canh.get("khong_sp") and canh.get("moi"):
                    rieng = []  # cảnh mở đầu không có sản phẩm: không gửi ảnh sản phẩm, kẻo Muse vẽ thêm tuýp
                if da.get("noi_canh_muse") and not canh.get("moi"):
                    # canh["moi"]: cảnh có sản phẩm quay MỚI từ ảnh sản phẩm thật (không nối khung cuối): 08/11/2026 mọi cảnh quay kiểu
                    # này đều đúng nhãn, còn cảnh nối từ khung cuối thì Muse vẽ lại nhãn.
                    # 08/11/2026: mỗi clip Muse quay riêng nên mỗi cảnh một phòng khác, cảnh sau không biết cảnh trước có gì
                    # (đĩa, gel). Nối cảnh: gửi kèm KHUNG CUỐI của clip đứng trước làm ảnh thứ nhất, dặn bắt đầu đúng từ đó.
                    vt = next(k for k, c in enumerate(da["canh"]) if c["ma"] == canh["ma"])
                    truoc = da["canh"][vt - 1] if vt else None
                    if truoc and not (truoc.get("trang_thai") == "xong" and truoc.get("file")):
                        raise RuntimeError("Chờ clip trước quay xong (nối cảnh cần khung cuối của clip trước).")
                    if truoc and canh.get("khong_sp"):
                        # Cảnh không có sản phẩm: gửi ảnh sản phẩm là Muse tự vẽ thêm một cái tuýp (08/11/2026, video Chuyến đi).
                        goc = os.path.join(thu_muc, truoc["file"])
                        rieng = [khung_cuoi(goc, os.path.join(thu_muc, f"{truoc['file'][:-4]}_cuoi.png"))]
                        noi = LOI_NOI_KHONG_SP
                    elif truoc:
                        goc = os.path.join(thu_muc, truoc["file"])
                        rieng = [anh_canh[canh["ma"]], khung_cuoi(goc, os.path.join(thu_muc, f"{truoc['file'][:-4]}_cuoi.png"))]
                        noi = LOI_NOI_MUSE
                for lan in range(2):
                    # Muse có trí nhớ giữa các cuộc chat và đôi khi gửi lại đúng file cũ (muse.py bắt được). 07/11/2026: gửi lại
                    # MỘT lần trong cuộc chat mới, kèm mã mới và lời dặn rõ, thay vì báo hỏng ngay.
                    them = "" if not lan else (" IMPORTANT: this is a NEW, separate request. Do NOT resend, reuse or reference any "
                                               "video you made before. Render a brand-new video from scratch now.")
                    job = VideoJob(prompt=noi + lenh_gui(da, canh) + loi_dan_muse(da, canh) + them, duration=str(int(canh["giay"])), aspect_ratio="9:16",
                                   other_settings=da.get("muse_cai_dat") or "hands-only product b-roll, no face, no text", reference_images=rieng[:3])
                    try:
                        await studio.create_video(job)
                        await studio.wait_for_completion(job, timeout=CHO_CLIP, cancelled=lambda: bool(viec.get("huy")))
                        tai = await studio.download_video(job)
                        break
                    except RuntimeError as loi_muse:
                        if lan or "video cũ" not in str(loi_muse) or viec.get("huy"):
                            raise
                        pc.buoc(viec, None, f"Muse gửi lại clip cũ cho clip {canh['ma'][1:]}, đang gửi lại yêu cầu quay mới")
                canh["ban"] = int(canh.get("ban") or 0) + 1
                ten_tep = f"{canh['ma']}_v{canh['ban']:02d}.mp4"
                pc.chay([pc.ffmpeg(), "-y", "-ss", str(CAT_DAU), "-i", str(tai), "-c:v", "libx264", "-preset", "fast",
                         "-crf", "18", *GIU_TIENG, os.path.join(thu_muc, ten_tep)])
                canh.update(trang_thai="xong", file=ten_tep)
                ghi_nhat_ky_quay(da, canh, "muse", job.prompt, rieng[:3], "xong", "", ten_tep)
            except Exception as exc:  # noqa: BLE001 - một clip hỏng không kéo cả video
                canh.update(trang_thai="cho" if viec.get("huy") else "loi", loi="" if viec.get("huy") else _loi_de_hieu(str(exc)))
                if not viec.get("huy"):
                    ghi_nhat_ky_quay(da, canh, "muse", locals().get("job").prompt if locals().get("job") else "", rieng[:3],
                                     "tu_choi" if _la_tu_choi(str(exc)) else "loi", str(exc)[:600], "")
            luu(da)

        async def mo_cua_so(chinh):
            """Một MuseAIStudio dùng chung Chrome của `chinh`, trên một cửa sổ mới."""
            trang = await chinh.launch_browser()
            cdp = await chinh._context.new_cdp_session(trang)
            async with chinh._context.expect_page() as moi:
                await cdp.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
            p = await moi.value
            p.set_default_timeout(30000)
            phu = MuseAIStudio()
            phu._playwright, phu._context, phu._page = chinh._playwright, chinh._context, p
            return phu

        try:
            if so_cua == 1:
                for i, canh in enumerate(can):
                    if viec.get("huy"):
                        break
                    await quay_canh(studio, i, canh)
                return
            hang = list(enumerate(can))
            cua = [studio]
            for _ in range(so_cua - 1):
                try:
                    cua.append(await mo_cua_so(studio))
                except Exception as exc:  # noqa: BLE001 - không mở được cửa sổ thêm thì quay bằng số cửa sổ đang có
                    logging.warning("[review_sp] Không mở thêm cửa sổ Muse: %s", exc)
                    break

            async def tho(k, st):
                await asyncio.sleep(k * MUSE_GIAN_CACH)  # các cửa sổ không bấm "New side chat" cùng một lúc
                while hang and not viec.get("huy"):
                    i, canh = hang.pop(0)
                    await quay_canh(st, i, canh)
            await asyncio.gather(*[tho(k, st) for k, st in enumerate(cua)])
        finally:
            trang = getattr(studio, "_page", None)
            if trang is not None and not trang.is_closed():
                import muse_han_muc  # quay xong thì đọc luôn hạn mức Muse trên cửa sổ đang mở (15/11/2026)
                await muse_han_muc.cap_nhat_sau_khi_quay(trang)
            await studio.close()
    asyncio.run(go())
    return da


# Nhật ký mọi lần quay (08/11/2026, người dùng dặn: "ghi lại toàn bộ audit log để biết reject ở đâu, lỗi đâu còn điều chỉnh").
NHAT_KY_QUAY = os.path.join(pc.TAI_VE, "review_sp", "nhat_ky_quay.jsonl")
_TU_CHOI = re.compile(r"can'?t (?:do|render|generate|make)|cannot|decline|parked|no generation|not able|unable to|won'?t", re.I)


def _la_tu_choi(loi: str) -> bool:
    """Máy làm clip từ chối (Muse trả lời "I can't do this one", "parked beat"...) khác với lỗi kỹ thuật (mạng, tải hỏng)."""
    return bool(_TU_CHOI.search(loi or ""))


def ghi_nhat_ky_quay(da: Dict, canh: Dict, may: str, lenh: str, anh: List[str], ket_qua: str, noi: str, tep: str) -> None:
    """Thêm một dòng vào nhật ký quay: giờ, video, concept, cảnh, máy, câu lệnh gửi thật, ảnh mẫu, kết quả, máy trả lời gì."""
    try:
        os.makedirs(os.path.dirname(NHAT_KY_QUAY), exist_ok=True)
        dong = {"luc": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": da.get("id"), "video": da.get("ten") or "",
                "concept": da.get("concept_demo") or da.get("concept_chon") or "", "canh": canh.get("ma"), "may": may,
                "kieu": "moi" if canh.get("moi") else ("noi_khong_sp" if canh.get("khong_sp") else "noi"),
                "ket_qua": ket_qua, "may_noi": noi, "file": tep, "anh": [os.path.basename(a) for a in anh or []], "lenh": lenh}
        with open(NHAT_KY_QUAY, "a", encoding="utf-8") as f:
            f.write(json.dumps(dong, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - nhật ký hỏng không được làm hỏng việc quay
        pass


def _quay_gemini(da: Dict, viec: Dict, can: List[Dict], anh_canh: Dict) -> Dict:
    """Quay bằng app Gemini (gemini_may.py): lần lượt từng clip, mỗi clip một cuộc chat mới. Hết lượt thì tự chờ mở lại rồi quay tiếp."""
    import gemini_may
    thu_muc = os.path.join(_thu_muc(da["id"]), "clip")
    het, da_cho = "", 0.0
    for i, canh in enumerate(can):
        if viec.get("huy"):
            break
        if het:
            canh.update(trang_thai="loi", loi=_loi_de_hieu(het))
            continue
        pc.buoc(viec, round(100 * i / len(can)), f"Gemini đang làm clip {canh['ma'][1:]}, còn {len(can) - i} clip")
        canh.update(trang_thai="dang", loi="")
        luu(da)
        try:
            while True:
                try:
                    tho = gemini_may.quay(re.sub(r"(?:attached )?reference image", "attached photo", lenh_gui(da, canh)),
                                          anh_canh[canh["ma"]], os.path.join(thu_muc, "gemini", f"{canh['ma']}_{int(time.time())}.mp4"),
                                          bao=lambda chu: pc.buoc(viec, None, chu), huy=lambda: bool(viec.get("huy")))
                    break
                except gemini_may.HetLuotGemini as exc:
                    # 07/11/2026: hết lượt thì tự chờ tới giờ mở lại rồi quay tiếp (mẻ hàng loạt, lịch chạy đêm không ai bấm lại).
                    # `cho_gemini=False` thì báo hỏng ngay như cũ. Chờ quá CHO_TOI_DA_KHOA thì cho_mo_lai ném lại lỗi.
                    if da.get("cho_gemini", True) is False:
                        raise
                    canh.update(trang_thai="cho_luot", loi="")
                    luu(da)
                    da_cho = gemini_may.cho_mo_lai(str(exc), lambda: bool(viec.get("huy")), lambda chu: pc.buoc(viec, None, chu), da_cho)
                    canh.update(trang_thai="dang")
                    luu(da)
            canh["ban"] = int(canh.get("ban") or 0) + 1
            ten_tep = f"{canh['ma']}_v{canh['ban']:02d}.mp4"
            pc.chay([pc.ffmpeg(), "-y", "-i", tho, "-c:v", "libx264", "-preset", "fast", "-crf", "18", *GIU_TIENG, os.path.join(thu_muc, ten_tep)])
            canh.update(trang_thai="xong", file=ten_tep)
        except gemini_may.HetLuotGemini as exc:
            het = str(exc)
            canh.update(trang_thai="loi", loi=_loi_de_hieu(het))
        except Exception as exc:  # noqa: BLE001 - một clip hỏng không kéo cả video
            canh.update(trang_thai="cho" if viec.get("huy") else "loi", loi="" if viec.get("huy") else _loi_de_hieu(str(exc)),
                        loi_goc=str(exc)[:400])  # lỗi thô: câu dễ hiểu có thể che mất nguyên nhân thật (07/11)
        luu(da)
    return luu(da)


def _quay_kling(da: Dict, viec: Dict, can: List[Dict], anh_canh: Dict) -> Dict:
    """Quay bằng Kling (kling_may.py): lần lượt từng clip trên một tài khoản. Không đủ credit thì các clip còn lại báo hỏng, không gửi."""
    import kling_may
    thu_muc = os.path.join(_thu_muc(da["id"]), "clip")
    model = da.get("kling_model") or kling_may.MODEL
    het = ""
    for i, canh in enumerate(can):
        if viec.get("huy"):
            break
        if het:
            canh.update(trang_thai="loi", loi=_loi_de_hieu(het))
            continue
        pc.buoc(viec, round(100 * i / len(can)), f"Kling đang làm clip {canh['ma'][1:]}, còn {len(can) - i} clip")
        canh.update(trang_thai="dang", loi="")
        luu(da)
        lenh = re.sub(r"(?:attached )?reference image|image 1", "reference image", lenh_gui(da, canh))
        try:
            kq = kling_may.quay(lenh, anh_canh[canh["ma"]], os.path.join(thu_muc, "kling", f"{canh['ma']}_{int(time.time())}.mp4"),
                                giay=int(canh.get("giay") or da.get("giay_clip") or 5), model=model,
                                bao=lambda chu: pc.buoc(viec, None, chu), huy=lambda: bool(viec.get("huy")))
            ghi_nhat_ky_quay(da, canh, "kling", lenh, [anh_canh[canh["ma"]]], "xong", "", "")
            canh["ban"] = int(canh.get("ban") or 0) + 1
            ten_tep = f"{canh['ma']}_v{canh['ban']:02d}.mp4"
            pc.chay([pc.ffmpeg(), "-y", "-i", kq["file"], "-c:v", "libx264", "-preset", "fast", "-crf", "18", *GIU_TIENG, os.path.join(thu_muc, ten_tep)])
            canh.update(trang_thai="xong", file=ten_tep)
        except kling_may.HetCreditKling as exc:
            het = str(exc)
            canh.update(trang_thai="loi", loi=_loi_de_hieu(het))
            ghi_nhat_ky_quay(da, canh, "kling", lenh, [anh_canh[canh["ma"]]], "loi", het, "")
        except Exception as exc:  # noqa: BLE001 - một clip hỏng không kéo cả video
            canh.update(trang_thai="cho" if viec.get("huy") else "loi", loi="" if viec.get("huy") else _loi_de_hieu(str(exc)),
                        loi_goc=str(exc)[:400])
            ghi_nhat_ky_quay(da, canh, "kling", lenh, [anh_canh[canh["ma"]]], "tu_choi" if _la_tu_choi(str(exc)) else "loi", str(exc)[:300], "")
        luu(da)
    return luu(da)


def _project_cua_tk(da: Dict, tk: str) -> str:
    """Project Flow cũ của video nếu nó thuộc đúng tài khoản đang dùng, không thì rỗng (tool tạo project mới)."""
    du_an = da.get("flow_project") or ""
    m = re.search(r"flow\.google\.com/u/(\d+)/", du_an)
    cua = da.get("flow_tk") or (m.group(1) if m else "")
    return du_an if du_an and (not tk or not cua or cua == tk) else ""


def _quay_flow(da: Dict, viec: Dict, can: List[Dict], anh, dau=None, cuoi=None) -> Dict:
    """Quay bằng Google Flow: gửi mọi câu lệnh một lượt, Flow làm song song, xong thì tải về. `dau`: khung mở đầu từng clip."""
    import flow_may
    thu_muc = os.path.join(_thu_muc(da["id"]), "clip")
    for canh in can:
        canh.update(trang_thai="dang", loi="")
    luu(da)
    can_credit = len(can) * tim_cach("flow:" + (can[0].get("flow_model") or da.get("flow_model") or flow_may.MODEL))["credit"]
    try:
        # 07/11/2026: đọc credit trước khi gửi lệnh; tài khoản đang dùng không đủ thì tự chuyển sang tài khoản đang đăng nhập còn đủ.
        doi = flow_may.dam_bao_credit(can_credit)
        if doi.get("doi"):
            pc.buoc(viec, None, f"Tài khoản {doi.get('tu') or 'đang dùng'} không đủ credit, chuyển sang {doi.get('email')} ({doi.get('credit')} credit)")
    except flow_may.HetCredit as exc:
        for canh in can:
            canh.update(trang_thai="loi", loi=_loi_de_hieu(str(exc)))
        return luu(da)
    except Exception:  # noqa: BLE001 - không đọc được credit thì vẫn quay như cũ
        pass
    # Project Flow thuộc một tài khoản Google: đổi tài khoản (hết lượt, đăng nhập tài khoản khác) thì tạo project mới.
    tk = flow_may._tk_dang_dung()
    try:
        # Mỗi video có project Flow riêng, tool tự tạo ở lượt quay đầu và nhớ lại cho các lượt sau.
        dau, cuoi = dau or [None] * len(can), cuoi or [None] * len(can)
        lenh = [lenh_gui(da, c) + (" " + LOI_NOI if k else "") + (" " + LOI_NOI_CUOI if e else "") for c, k, e in zip(can, dau, cuoi)]
        tra = flow_may.quay_sp(lenh, anh, "9:16", int(can[0]["giay"]),
                            os.path.join(thu_muc, "flow"), bao=lambda chu: pc.buoc(viec, None, chu), huy=lambda: bool(viec.get("huy")),
                            du_an=_project_cua_tk(da, tk), ten=da.get("ten") or "", dau=dau, cuoi=cuoi,
                            model=can[0].get("flow_model") or da.get("flow_model") or "")  # clip có model riêng thì theo clip
        da["flow_project"], da["flow_tk"], got = tra["project"], tk, tra["clips"]
        flow_may.tru_credit(can_credit)
    except Exception as exc:  # noqa: BLE001
        got = [{"ok": False, "loi": str(exc)[:200]}] * len(can)
    for canh, kq in zip(can, got):
        if not kq.get("ok"):
            canh.update(trang_thai="loi", loi=_loi_de_hieu(kq.get("loi") or "Flow chưa trả clip."))
            continue
        canh["ban"] = int(canh.get("ban") or 0) + 1
        ten_tep = f"{canh['ma']}_v{canh['ban']:02d}.mp4"
        pc.chay([pc.ffmpeg(), "-y", "-i", kq["file"], "-c:v", "libx264", "-preset", "fast", "-crf", "18", *GIU_TIENG, os.path.join(thu_muc, ten_tep)])
        canh.update(trang_thai="xong", file=ten_tep)
    return luu(da)


def _loi_de_hieu(loi: str) -> str:
    """
    Đổi lỗi thô của máy làm clip (tiếng Anh, tên file, dấu vết code) thành một câu tiếng Việt kèm cách xử lý. Người dùng là
    người của brand: đọc xong phải biết bấm gì (07/11/2026, trước đó trang hiện nguyên "Insufficient credits: ...").
    """
    loi = str(loi or "")
    thuong = loi.lower()
    if "gemini" in thuong and ("hết lượt" in thuong or "upgrade" in thuong or "gói pro" in thuong):
        import re as _re
        gio = _re.search(r"mở lại lúc ([0-9:]+ ?[AP]M)", loi)
        return (f"Gemini đã hết lượt tạo video{', mở lại lúc ' + gio.group(1) if gio else ''} (hoặc tài khoản không có gói Pro). "
                "Tới giờ đó bấm Quay lại clip này, hoặc đổi máy làm clip.")
    if "kling" in thuong and ("credit" in thuong or "login" in thuong or "đăng nhập" in thuong):
        return loi.split("[ô trước")[0].strip()[:300]
    if "không tài khoản nào đủ" in thuong:
        return loi.split("Insufficient credits:", 1)[-1].strip()[:300]
    if "credit" in thuong or "insufficient" in thuong or "hết lượt" in thuong:
        return ("Tài khoản Google Flow đang dùng đã hết credit. Nạp thêm credit, hoặc đăng nhập tài khoản khác trong cửa sổ Flow "
                "rồi chọn ở ô Tài khoản Flow đầu trang, sau đó bấm Quay lại clip này.")
    if "audio generation failed" in thuong or "generation failed" in thuong:
        return ("Flow từ chối làm clip này (thường do ảnh sản phẩm nhạy cảm, hay gặp ở model Veo). Chọn Google Flow · Omni 1.2 "
                "hoặc Muse AI ở ô Máy làm clip rồi quay lại.")
    if "policy" in thuong or "safety" in thuong or "từ chối" in thuong:
        return "Máy làm clip từ chối câu lệnh này vì chính sách nội dung. Thử đổi động tác, ảnh sản phẩm, hoặc đổi máy làm clip."
    if "login" in thuong or "session" in thuong or "logged" in thuong or "đăng nhập" in thuong:
        return "Máy làm clip chưa đăng nhập hoặc đã hết phiên. Bấm Mở cửa sổ đăng nhập ở đầu trang UGC Video Studio, đăng nhập rồi thử lại."
    if "video cũ" in thuong or "không phải video mới" in thuong:
        return "Muse gửi lại một clip đã làm từ trước thay vì quay mới. Bấm Quay lại clip này, hoặc đổi sang Google Flow."
    if "timeout" in thuong or "timed out" in thuong or "quá lâu" in thuong:
        return "Chờ quá lâu mà máy làm clip chưa trả clip. Bấm Quay lại clip này."
    if "không nhận lệnh" in thuong or "chưa trả clip" in thuong:
        return "Flow chưa nhận hoặc chưa trả clip. Mở cửa sổ Flow xem có hộp thông báo nào che không, rồi bấm Quay lại clip này."
    # Lỗi lạ: bỏ tên file và đường dẫn cho dễ đọc
    import re as _re
    gon = _re.sub(r"\(?[\w./-]*\.(mp4|png|jpg|json)\)?", "", loi).strip()
    return (gon or loi)[:200]


# Tiếng thật của clip (08/11/2026): clip Gemini/Flow có sẵn tiếng (nắp bật, gel chảy, nước vòi) mà trước đây bị bỏ ("-an").
# Nay giữ tiếng khi chép clip về, nền ghép có tiếng, bản dựng phát tiếng đó nhỏ dưới giọng đọc (`tieng_clip`). Clip cũ không có
# tiếng thì chèn im lặng cho đủ dài, để mọi đoạn nối được với nhau.
GIU_TIENG = ["-c:a", "aac", "-b:a", "128k"]
TIENG_CLIP = {"nho": ("Nhỏ dưới giọng đọc", 0.25), "tat": ("Tắt", 0.0)}
_TIENG_RA = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]


def am_luong_clip(da: Dict) -> float:
    return TIENG_CLIP.get(da.get("tieng_clip") or "nho", TIENG_CLIP["nho"])[1]


def _co_tieng(duong: str) -> bool:
    import subprocess
    try:
        r = subprocess.run([pc.ffprobe(), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", duong],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _chuan_hoa(vao: str, ra: str, khung: str) -> None:
    """Một clip về đúng khung hình + LUÔN có một luồng tiếng (tiếng thật, hoặc im lặng cùng độ dài)."""
    if _co_tieng(vao):
        pc.chay([pc.ffmpeg(), "-y", "-i", vao, "-vf", khung, "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "fast",
                 "-crf", "18", "-pix_fmt", "yuv420p", *_TIENG_RA, "-shortest", ra])
    else:
        pc.chay([pc.ffmpeg(), "-y", "-i", vao, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", khung, "-map", "0:v:0",
                 "-map", "1:a:0", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", *_TIENG_RA, "-shortest", ra])


def khung_bien_the(k: int) -> str:
    """Khung ffmpeg của bản thứ `k` (0 = bản gốc). Nuôi trang (16/11/2026): mỗi Fanpage một bản nhìn khác, cùng clip: phóng to
    một chút, lệch khung trái/phải, đổi nhẹ sáng và màu. KHÔNG lật ngang (chữ trên bao bì sản phẩm sẽ ngược)."""
    goc = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if k <= 0:
        return goc + ",fps=30,setsar=1"
    z = 1 + 0.05 * (1 + (k - 1) % 3)
    w, h = int(1080 * z) // 2 * 2, int(1920 * z) // 2 * 2
    dx = (0.15, 0.85, 0.5)[(k - 1) % 3]
    dy = (0.4, 0.6, 0.35)[(k - 1) % 3]
    sang = (0.025, -0.02, 0.015)[(k - 1) % 3]
    mau = (1.08, 0.93, 1.04)[(k - 1) % 3]
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop=1080:1920:(iw-1080)*{dx}:(ih-1920)*{dy},"
            f"eq=brightness={sang}:saturation={mau},fps=30,setsar=1")


def ghep_nen(da: Dict) -> str:
    """Ghép các clip thành một nền 1080x1920 dài đúng số giây của video, kèm tiếng thật của clip. Thiếu giây thì quay vòng."""
    goc = _thu_muc(da["id"])
    clip = [os.path.join(goc, "clip", c["file"]) for c in da.get("canh") or [] if c.get("trang_thai") == "xong" and c.get("file")]
    clip = [c for c in clip if os.path.isfile(c)]
    if not clip:
        raise ValueError("Chưa có clip nào quay xong.")
    tam = os.path.join(goc, "tam")
    shutil.rmtree(tam, ignore_errors=True)
    os.makedirs(tam)
    chuan = []
    for i, c in enumerate(clip):
        ra = os.path.join(tam, f"p{i:02d}.mp4")
        _chuan_hoa(c, ra, khung_bien_the(int(da.get("bien_the") or 0)))
        chuan.append((ra, pc.so_giay(ra)))
    can = float(da.get("so_giay") or SO_GIAY)
    nen = os.path.join(goc, "nen.mp4")
    xong = [c for c in da.get("canh") or [] if c.get("trang_thai") == "xong" and c.get("file")]
    xong = [c for c in xong if os.path.isfile(os.path.join(goc, "clip", c["file"]))]
    if da.get("cat_nhanh"):
        _ghep_nhanh(da, xong, chuan, can, nen, tam)
    elif len(xong) > 1 and all(c.get("noi_tu") for c in xong[1:]):
        # Clip đã nối khung (clip sau mở đầu bằng khung cuối của clip trước): nối thẳng là liền, mờ chồng lại thành nhoè đôi.
        _ghep_cung(chuan, can, nen, tam)
    else:
        try:
            _ghep_mo_chuyen(chuan, can, nen, giu_cuoi=bool(da.get("giu_cuoi")))
        except Exception:  # noqa: BLE001 - chuyển cảnh mềm hỏng thì nối cứng như cũ, không làm hỏng video
            _ghep_cung(chuan, can, nen, tam)
    shutil.rmtree(tam, ignore_errors=True)
    return nen


# Chuyển cảnh mềm giữa hai clip (giây), thêm 05/11/2026 thay cho cắt cứng.
GIAY_CHUYEN = 0.35


def _thu_tu_clip(chuan: List, can: float, chong: float) -> List:
    """Danh sách clip (quay vòng nếu thiếu) đủ dài để sau khi trừ phần chồng ở mỗi chỗ chuyển vẫn còn `can` giây."""
    ds, i = [], 0
    while (sum(d for _, d in ds) - chong * max(0, len(ds) - 1)) < can + 0.5 and i < 200:
        ra, dai = chuan[i % len(chuan)]
        ds.append((ra, max(dai, chong * 2 + 0.2)))
        i += 1
    return ds


def _ghep_mo_chuyen(chuan: List, can: float, nen: str, giu_cuoi: bool = False) -> None:
    # `giu_cuoi` (video review affiliate, 14/11/2026): mỗi clip chạy MỘT lần theo đúng thứ tự, thiếu giây thì đứng ở khung cuối
    # (cảnh thành quả) thay vì quay vòng về clip đầu (gà sống hiện lại sau gà chín).
    ds = list(chuan) if giu_cuoi else _thu_tu_clip(chuan, can, GIAY_CHUYEN)
    lenh = [pc.ffmpeg(), "-y"]
    for ra, _ in ds:
        lenh += ["-i", ra]
    if len(ds) == 1:
        loc = "[0:v]null[v];[0:a]anull[a]"
    else:
        phan, truoc, tr_a, moc = [], "[0:v]", "[0:a]", 0.0
        for k in range(1, len(ds)):
            moc += ds[k - 1][1] - GIAY_CHUYEN
            ra = f"[x{k}]" if k < len(ds) - 1 else "[v]"
            ra_a = f"[y{k}]" if k < len(ds) - 1 else "[a]"
            phan.append(f"{truoc}[{k}:v]xfade=transition=fade:duration={GIAY_CHUYEN}:offset={moc:.3f}{ra}")
            phan.append(f"{tr_a}[{k}:a]acrossfade=d={GIAY_CHUYEN}{ra_a}")
            truoc, tr_a = ra, ra_a
        loc = ";".join(phan)
    if giu_cuoi:
        loc = loc.replace("[v]", "[vg]").replace("[a]", "[ag]") + f";[vg]tpad=stop_mode=clone:stop_duration={can:.2f}[v];[ag]apad[a]"
    pc.chay(lenh + ["-filter_complex", loc, "-map", "[v]", "-map", "[a]", "-t", f"{can:.2f}", *_TIENG_RA, "-c:v", "libx264", "-preset", "fast",
                    "-crf", "18", "-pix_fmt", "yuv420p", nen])


# Dựng kiểu cắt nhanh của quảng cáo Feelex (06/11/2026, người dùng chê video "quá chậm", "không giống Feelex"): Feelex đổi cảnh
# 1 tới 3 giây một lần, cắt thẳng, mở đầu bằng cảnh gel. Mỗi clip chỉ góp vài đoạn ngắn; các đoạn xếp xen kẽ giữa các clip để
# hai đoạn liền nhau luôn là hai cảnh khác nhau. `canh["doan_tot"]` ([[từ, tới], ...] giây) là các đoạn đã soi là sạch; không có
# thì tool tự chia clip (bỏ nửa giây đầu và cuối).
GIAY_DOAN = 2.4
TOC_NHANH = 1.1  # tay trong clip AI thường chậm hơn tay thật một chút


def _doan_cua_clip(canh: Dict, dai: float) -> List:
    # Đoạn tự tính chỉ đúng với đúng file đã soát; clip đã quay lại (file khác) thì bỏ.
    tot = canh.get("doan_tot") if not canh.get("doan_tu_dong") or canh["doan_tu_dong"] == canh.get("file") else None
    sach = ([(float(a), min(float(b), dai)) for a, b in tot] if tot else [(0.5, dai - 0.3)])
    ra = []
    for a, b in sach:  # mỗi đoạn sạch dài được chia thành các đoạn khoảng GIAY_DOAN giây, cách nhau một chút
        t = a
        while b - t >= 0.8 and len(ra) < 3:
            ra.append((round(t, 2), round(min(b, t + GIAY_DOAN), 2)))
            t += GIAY_DOAN + 0.6
    return ra or [(0.0, min(dai, GIAY_DOAN))]


def _ghep_nhanh(da: Dict, xong: List[Dict], chuan: List, can: float, nen: str, tam: str) -> None:
    toc = float(da.get("toc_nhanh") or TOC_NHANH)
    cac = [_doan_cua_clip(c, dai) for c, (_, dai) in zip(xong, chuan)]
    thu_tu = []
    for k in range(max(len(x) for x in cac)):  # xen kẽ: đoạn 1 của mọi clip, rồi đoạn 2...
        thu_tu += [(i, cac[i][k]) for i in range(len(cac)) if k < len(cac[i])]
    dong, tong, n = [], 0.0, 0
    while tong < can + 0.3 and n < 200:
        i, (a, b) = thu_tu[n % len(thu_tu)]
        ra = os.path.join(tam, f"d{n:03d}.mp4")
        pc.chay([pc.ffmpeg(), "-y", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", chuan[i][0], "-vf",
                 f"setpts=PTS/{toc}", "-af", f"atempo={toc}", "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                 "-pix_fmt", "yuv420p", *_TIENG_RA, ra])
        dong.append(f"file '{ra}'")
        tong += (b - a) / toc
        n += 1
    ds_tep = os.path.join(tam, "ds_nhanh.txt")
    with open(ds_tep, "w", encoding="utf-8") as f:
        f.write("\n".join(dong))
    pc.chay([pc.ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", ds_tep, "-t", f"{can:.2f}", *_TIENG_RA,
             "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", nen])


def _ghep_cung(chuan: List, can: float, nen: str, tam: str) -> None:
    dong, tong, i = [], 0.0, 0
    while tong < can + 0.5 and i < 200:
        ra, dai = chuan[i % len(chuan)]
        dong.append(f"file '{ra}'")
        tong += max(dai, 0.1)
        i += 1
    ds_tep = os.path.join(tam, "ds.txt")
    with open(ds_tep, "w", encoding="utf-8") as f:
        f.write("\n".join(dong))
    pc.chay([pc.ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", ds_tep, "-t", f"{can:.2f}", *_TIENG_RA,
             "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", nen])


def lap_props(da: Dict, cho: str) -> Dict:
    """Dữ liệu gửi sang bản dựng hình ReviewSP. `cho` là thư mục con trong remotion/public chứa nền và giọng."""
    giay = float(da.get("so_giay") or SO_GIAY)
    the = da.get("the") or {}
    doc = da.get("giong_doc") or {}
    # Video affiliate (kich_ban_aff.chia_the) chia lời thành nhiều thẻ ngắn từng câu: cho tới 12 thẻ, kẻo mất cú twist cuối truyện
    # (14/11/2026 video drama: thẻ dừng ở câu 4/6). Product Video của PlayAh giữ SO_THE_TOI_DA.
    doan = [d.strip() for d in (the.get("cac_doan") or [the.get("chu") or ""]) if (d or "").strip()][:12 if da.get("dang_aff") else SO_THE_TOI_DA]
    nhan = da.get("nhan_kenh") or {}
    anh_the = []
    if da.get("the_kieu", "anh") == "anh":
        from PIL import Image
        for i, duong in enumerate(anh_bai(da.get("bai"))):
            with Image.open(duong) as im:
                anh_the.append({"src": f"{cho}/bai_{i + 1}.png", "w": im.width, "h": im.height})
    return {"nen": f"{cho}/nen.mp4", "giong": f"{cho}/giong.wav", "giay": giay, "tiengClip": am_luong_clip(da),
            # Nhãn kênh: tên (và ảnh đại diện nếu có) của kênh sẽ đăng video này. Kênh vệ tinh không gắn tên thương hiệu.
            "kenh": {"ten": (nhan.get("ten") or "").strip()[:40], "anh": f"{cho}/{nhan['anh']}" if nhan.get("anh") else ""},
            # Dán ảnh chụp bài thì theo mẫu người dùng gửi: clip phủ kín, thẻ bài ở phần trên (05/11/2026).
            "boCuc": "tran" if anh_the else (da.get("bo_cuc") if da.get("bo_cuc") in BO_CUC else "tran"),
            # Các thẻ chia đều thời lượng của LỜI KỂ (không phải cả video), để thẻ cuối không đứng trơ lúc đã hết lời.
            "the": {"tacGia": the.get("tac_gia") or "", "chu": doan[0] if doan else "", "cacDoan": doan,
                    "hetLuc": float(doc.get("seconds") or giay),
                    "anh": anh_the},
            # Concept (concept.py): câu móc chữ to giây đầu, kiểu chữ ("cum": chữ chạy từng cụm), ô bình luận của người xem.
            "moc": (da.get("moc") or "").strip(), "kieuChu": da.get("kieu_chu") if da.get("kieu_chu") in ("phu_de", "cum") else "phu_de",
            "binhLuan": {"ten": ((da.get("binh_luan") or {}).get("ten") or "").strip(), "chu": ((da.get("binh_luan") or {}).get("chu") or "").strip()},
            "khoanh": gio_khoanh(da.get("khoanh") or [], giay),
            # Video affiliate (kich_ban_aff): biển tên hãng + nhãn giá. Dự án PlayAh không có khoá này nên không đổi gì.
            **({"nhanAff": da["nhan_aff"]} if isinstance(da.get("nhan_aff"), dict) else {}),
            "phuDe": pc.chia_phu_de(da.get("loi_ke") or "", float(doc.get("seconds") or giay)) if da.get("phu_de", True) else [],
            # 11/11/2026: phụ đề kiểu TikTok như Alex Studio, giờ từng chữ đo từ giọng thật; chỉ dùng khi đúng lượt giọng đang có.
            "tuPhuDe": (da.get("tu_phu_de") or {}).get("tu") or [] if da.get("phu_de", True)
            and (da.get("tu_phu_de") or {}).get("dau") == doc.get("dau") else []}


def gio_khoanh(cac: List[str], giay: float) -> List[Dict]:
    """
    Giờ hiện của các vòng khoanh (tối đa 3): sau câu móc 3 giây đầu, rải đều tới trước 3 giây cuối, mỗi vòng 2,4 giây.
    """
    cac = [str(x).strip()[:40] for x in cac if str(x).strip()][:3]
    if not cac or giay < 10:
        return []
    buoc = (giay - 6.5) / len(cac)
    return [{"chu": c, "tu": round(3.5 + i * buoc, 2), "den": round(3.5 + i * buoc + 2.4, 2)} for i, c in enumerate(cac)]


def _dung_mot(da: Dict, ten: str, viec: Dict, chu: str = "Đang dựng video (đính thẻ bài, giọng, phụ đề)", pct: int = 25) -> str:
    """Ghép nền từ clip của `da` (theo đúng thứ tự `da["canh"]`) rồi dựng hình ra `ten`. Trả đường dẫn file đã dựng."""
    from remotion_generator import render_remotion_video
    nen = ghep_nen(da)
    cho = f"review_sp/{da['id']}"
    dich = os.path.join(pc.PUBLIC, cho)
    os.makedirs(dich, exist_ok=True)
    try:
        shutil.copy2(nen, os.path.join(dich, "nen.mp4"))
        shutil.copy2(os.path.join(_thu_muc(da["id"]), "giong.wav"), os.path.join(dich, "giong.wav"))
        for i, duong in enumerate(anh_bai(da.get("bai"))):
            shutil.copy2(duong, os.path.join(dich, f"bai_{i + 1}.png"))
        anh_kenh = (da.get("nhan_kenh") or {}).get("anh") or ""
        if anh_kenh and os.path.isfile(os.path.join(_thu_muc(da["id"]), anh_kenh)):
            shutil.copy2(os.path.join(_thu_muc(da["id"]), anh_kenh), os.path.join(dich, anh_kenh))
        elif anh_kenh:
            da["nhan_kenh"]["anh"] = ""
        pc.buoc(viec, pct, chu)
        got = render_remotion_video("ReviewSP", ten, props=lap_props(da, cho), timeout=1500)
        if not got.get("success"):
            raise RuntimeError(got.get("message") or "Dựng hình lỗi.")
    finally:
        shutil.rmtree(dich, ignore_errors=True)
    return got["output_path"]


def dung(da: Dict, viec: Dict) -> Dict:
    doc_loi(da, viec)
    pc.buoc(viec, 10, "Đang ghép các clip thành nền")
    ban = len(da.get("phim") or []) + 1
    ten = f"review_{da['id'][3:]}_v{ban:02d}.mp4"
    got = {"output_path": _dung_mot(da, ten, viec)}
    da.setdefault("phim", []).append({"file": ten, "luc": time.time(), "seconds": round(pc.so_giay(got["output_path"]), 1)})
    luu(da)
    # Gửi brand duyệt: tự đưa lên Drive (duyet_drive.py). Drive chưa nối hay lỗi thì video vẫn xong.
    import duyet_drive
    if duyet_drive.cau_hinh()["tu_dong"] and duyet_drive.da_noi():
        pc.buoc(viec, 96, "Đang đưa video lên Drive cho brand xem")
        duyet_drive.thu_dua_len(da, da["phim"][-1], got["output_path"])
        luu(da)
    viec["result"] = {"file": ten, "url": f"/downloads/{ten}"}
    return da


# ── Nhiều câu mở đầu (08/11/2026) ────────────────────────────────────────────
# Cùng thân video (clip, giọng, phụ đề), chỉ đổi câu móc chữ to ở giây đầu và clip mở đầu, để thử câu nào giữ người xem lâu hơn.
# Không quay thêm gì: chỉ AI viết câu móc (qua bộ soát luật nói) và dựng lại.
SO_MO_DAU = 3


def viet_cau_moc(da: Dict, so: int = SO_MO_DAU) -> List[str]:
    import kho_anh
    import luat_nhan_hang
    luu_y = luat_nhan_hang.phan_luu_y(kho_anh.loi_dan_viet((da.get("san_pham") or {}).get("kho") or "") or "")
    cu = [(da.get("moc") or "").strip()] + [m.get("moc") or "" for m in da.get("mo_dau") or []]
    de = (f"LỜI ĐỌC CỦA VIDEO:\n{da.get('loi_ke') or ''}\n\nViết {so + 2} câu móc KHÁC NHAU để hiện chữ to ở giây đầu video, mỗi câu 2 tới 6 "
          "chữ, viết như người nói, gợi tò mò, đúng nội dung lời đọc, mỗi câu một kiểu mở khác nhau (câu hỏi, khẳng định, so sánh, "
          f"bất ngờ...). Không trùng các câu đã dùng: {'; '.join(x for x in cu if x) or 'chưa có'}. Tiếng Việt, gọi bao cao su là áo mưa, "
          "gel bôi trơn là gel dưỡng ẩm, không nhắc chuyện chăn gối, không nêu giá, không hứa số phút, không nói về sức khoẻ.\n"
          'Trả về: {"moc": ["...", "..."]}')
    tot: List[str] = []
    for _ in range(2):
        got = pc.hoi_ai("Bạn viết câu móc mở đầu cho video ngắn bán hàng. Chỉ trả về JSON.", de, max_tokens=800)
        for m in got.get("moc") or []:
            m = str(m).strip().strip('"')[:60]
            if m and m not in tot and m not in cu and not luat_nhan_hang.soat(m, luu_y):
                tot.append(m)
        if len(tot) >= so:
            break
    if not tot:
        raise RuntimeError("AI chưa viết được câu móc nào qua bộ soát luật nói. Thử lại.")
    return tot[:so]


def lam_mo_dau(da: Dict, viec: Dict, so: int = SO_MO_DAU) -> Dict:
    """Dựng `so` bản: mỗi bản một câu móc mới và mở bằng một clip khác (xoay thứ tự clip). Lưu `da["mo_dau"]`."""
    import copy
    xong = [c for c in da.get("canh") or [] if c.get("trang_thai") == "xong" and c.get("file")]
    if len(xong) != len(da.get("canh") or []) or not xong:
        raise RuntimeError("Clip chưa quay đủ. Làm video trước rồi mới làm bản mở đầu khác.")
    doc_loi(da, viec)
    pc.buoc(viec, 8, "AI đang viết các câu mở đầu khác")
    cac = viet_cau_moc(da, so)
    for k, moc in enumerate(cac):
        if viec.get("huy"):
            break
        ban = copy.deepcopy(da)
        lech = (k + 1) % len(xong)
        ban["canh"] = xong[lech:] + xong[:lech]
        ban["moc"] = moc
        ten = f"review_{da['id'][3:]}_md{len(da.get('mo_dau') or []) + 1:02d}.mp4"
        duong = _dung_mot(ban, ten, viec, f"Đang dựng bản mở đầu {k + 1}/{len(cac)}: {moc}", 15 + round(80 * k / len(cac)))
        moi = {"file": ten, "moc": moc, "mo_bang": ban["canh"][0]["ma"], "luc": time.time(), "seconds": round(pc.so_giay(duong), 1)}
        import duyet_drive
        if duyet_drive.cau_hinh()["tu_dong"] and duyet_drive.da_noi():
            try:
                moi["drive"] = duyet_drive.dua_len_tep(da, duong, f"mở đầu {len(da.get('mo_dau') or []) + 1} - {moc}")
            except Exception as exc:  # noqa: BLE001
                moi["drive_loi"] = duyet_drive._loi(exc)
        da.setdefault("mo_dau", []).append(moi)
        luu(da)
    return da


def thieu_gi(da: Dict) -> List[str]:
    """Những thứ còn thiếu để làm được video, bằng lời người dùng đọc hiểu."""
    ra = []
    if not (da.get("san_pham", {}).get("anh")):
        ra.append("ảnh sản phẩm (chọn sản phẩm trong Kho ảnh ở bước 1)")
    if not (da.get("loi_ke") or "").strip():
        # 07/11/2026: concept cũng phải có lời trước khi quay, để người dùng đọc duyệt lời rồi mới tiêu credit làm clip.
        ra.append("lời kể (bấm Viết lời ở bước 2 rồi đọc lại)" if da.get("concept_chon") else
                  "lời kể (chọn một concept, hoặc chọn bài Threads, rồi bấm Viết lời ở bước 2)")
    if not da.get("giong"):
        ra.append("giọng đọc")
    return ra


# ── Concept (concept.py) cho một video ───────────────────────────────────────
# Dùng chung thư viện concept của Làm hàng loạt (thêm 06/11/2026: người dùng muốn "chọn ảnh + chọn concept => gen video"
# ngay ở UGC Video Studio và từ Soi video mẫu). `concept_chon` là concept đang chọn; `concept` là concept ĐÃ viết lời.

def ap_concept(da: Dict, ma: str) -> Dict:
    """Chọn concept cho video: concept có cảnh riêng thì clip đổi theo các cảnh đó (mượn phong cách của mẫu nếu video theo mẫu);
    bỏ concept hay đổi sang concept không có cảnh riêng thì trả lại các clip cũ. Đổi concept là lời cũ của concept trước mất."""
    import concept as cc_
    ma = str(ma or "")
    cc = cc_.lay(ma) if ma else None
    if ma and not cc:
        raise ValueError("Không tìm thấy concept này.")
    loai = da.get("loai_sp") or "hop"
    if cc and loai not in cc["loai"]:
        raise ValueError(f"Concept \"{cc['ten']}\" không hợp sản phẩm loại {cc_.LOAI.get(loai, loai).lower()}.")
    cu = da.get("concept_chon") or ""
    if cu == ma:
        return da
    da["concept_chon"] = ma
    if da.get("concept") or (ma and da.get("bai")):
        # Lời của concept trước, hay lời kể bài Threads, không còn đúng với concept mới.
        da["loi_ke"], da["giong_doc"], da["concept"] = "", None, None
    if ma and da.get("bai"):
        da["bai"], da["the"] = None, {"tac_gia": "", "chu": "", "cac_doan": []}
    da["moc"], da["binh_luan"], da["kieu_chu"], da["khoanh"] = "", {}, (cc or {}).get("kieu_chu") or "phu_de", []
    co_cc = any(c.get("cc") for c in da.get("canh") or [])
    if cc and cc.get("canh"):
        if not co_cc:
            da["canh_goc"] = da.get("canh") or []
        _canh_concept(da, cc)
    elif co_cc:
        # Clip cũ (theo mẫu hoặc mặc định) quay lại từ đầu: file clip cùng tên đã bị clip của concept đè.
        da["canh"] = [{**c, "trang_thai": "cho", "file": "", "tranh": ""} for c in da.pop("canh_goc", None) or []]
    lap_canh(da)
    return da


def _canh_concept(da: Dict, cc: Dict) -> None:
    """Mỗi clip 10 giây theo một dòng cảnh của concept (quay vòng). Clip đã có đúng câu lệnh thì `lap_canh` giữ nguyên."""
    import concept as cc_
    import soi_video
    khoa = (da.get("mau") or {}).get("khoa") or ""
    if not khoa and (da.get("mau") or {}).get("id"):
        try:
            khoa = soi_video.mo(da["mau"]["id"]).get("khoa") or ""
        except (ValueError, FileNotFoundError):
            khoa = ""
    so = max(1, -(-int(da.get("so_giay") or SO_GIAY) // 10))
    cu = {c["ma"]: c for c in da.get("canh") or []}
    da["giay_clip"] = 10
    ra = []
    # 07/11/2026: mỗi video đổi qua vài nơi quay và xen cảnh chỉ có sản phẩm (concept.lap_canh_quay), như video Feelex.
    dong = cc_.lap_canh_quay(cc, so, da.get("id") or "", mot_noi=bool(da.get("noi_canh")))
    for i in range(so):
        ma = f"c{i + 1:02d}"
        lenh = soi_video.ghep_lenh(khoa or cc_.KHOA_MAC_DINH, dong[i]["viec"], dong[i]["kieu"])[:3000]
        truoc = cu.get(ma) or {}
        giu = truoc.get("cc") == cc["id"] and truoc.get("prompt") == lenh  # câu lệnh khác là clip cũ không còn đúng
        ra.append({**(truoc if giu else {}), "ma": ma, "hanh_dong": HANH_DONG[i % len(HANH_DONG)][0], "cc": cc["id"], "tu_sua": True,
                   "prompt": lenh, **({} if giu else {"trang_thai": "cho", "file": "", "tranh": ""})})
    da["canh"] = ra


def viet_theo_concept(da: Dict) -> Dict:
    """AI viết lời theo concept đang chọn, chỉ dựa trên thông tin sản phẩm (Kho ảnh, hoặc mô tả trên trang sản phẩm)."""
    import concept as cc_
    import kho_anh
    import lam_hang_loat as hl
    import random
    cc = cc_.lay(da.get("concept_chon") or "")
    if not cc:
        raise ValueError("Chưa chọn concept.")
    sp = da.get("san_pham") or {}
    mo_ta = kho_anh.loi_dan_viet(sp["kho"]) if sp.get("kho") else (kho_anh.mo_ta_cua_link(sp["link"]) if sp.get("link") else "")
    goc = "" if cc.get("khung_loi") else random.choice(hl.GOC_NOI)[0]
    return hl.viet_gioi_thieu(da, goc, mo_ta, cc)


# ── Đường vào ────────────────────────────────────────────────────────────────

def _loi(exc, ma=400):
    return jsonify({"ok": False, "error": str(exc)}), ma


def _loi_hien(loi: str) -> str:
    """Lỗi đã lưu từ trước 07/11 còn là chữ thô: đổi lúc trả cho trang. Câu đã là tiếng Việt dễ hiểu thì để nguyên."""
    return loi if not loi or not _re_tho.search(loi) else _loi_de_hieu(loi)


import re as _re_mod  # noqa: E402
_re_tho = _re_mod.compile(r"[A-Za-z]{4,} [A-Za-z]{3,}|\.mp4|\.png|Traceback|Error", _re_mod.I)


def _ra(da: Dict) -> Dict:
    if any(c.get("loi") for c in da.get("canh") or []):
        da = {**da, "canh": [{**c, "loi": _loi_hien(c.get("loi") or "")} for c in da["canh"]]}
    return {**da, "cach_quay": cach_quay(da), "kieu_dung": kieu_dung(da), "thieu": thieu_gi(da), "so_tieng": pc.so_tieng(da.get("loi_ke")), "so_tieng_can": so_tieng_can(int(da.get("so_giay") or SO_GIAY), da.get("giong") or ""),
            "tieng_moi_giay": pc.toc_do(da.get("giong") or "")}


@bp.route("/api/review_sp/phien", methods=["GET"])
def api_phien():
    return jsonify({"ok": True, "job_id": pc.viec_dang_chay(XUONG), "projects": ds(), "threads": kho_bai(),
                    "voice": pc.ds_giong(),
                    "presets": {"boi_canh": {k: v[0] for k, v in BOI_CANH.items()}, "tay": {k: v[0] for k, v in TAY.items()},
                                "hanh_dong": [{"id": k, "ten": vn} for k, vn, _ in HANH_DONG], "giay_clip": list(GIAY_CLIP),
                                "may_clip": MAY_CLIP, "giay_flow": list(GIAY_FLOW), "bo_cuc": BO_CUC, "loai_sp": LOAI_SP,
                                "cach_quay": CACH_QUAY, "kieu_dung": KIEU_DUNG, "kieu_dung_ghi": KIEU_DUNG_GHI,
                                "tieng_clip": {k: v[0] for k, v in TIENG_CLIP.items()}}})


@bp.route("/api/review_sp/tao", methods=["POST"])
def api_tao():
    return jsonify({"ok": True, "project": _ra(tao((request.get_json(silent=True) or {}).get("ten") or ""))})


@bp.route("/api/review_sp/du_an/<pid>", methods=["GET", "DELETE"])
def api_du_an(pid):
    try:
        if request.method == "DELETE":
            if pc.viec_dang_chay(XUONG):
                return _loi("Đang có việc chạy, chờ xong rồi xoá.")
            mo(pid)
            ten_xoa = mo(pid).get("ten") or ""
            shutil.rmtree(_thu_muc(pid))
            pc.ghi_nhat_ky(XUONG, "xoa", pid, ten=ten_xoa)
            return jsonify({"ok": True})
        return jsonify({"ok": True, "project": _ra(mo(pid))})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc, 404)


@bp.route("/api/review_sp/du_an/<pid>/luu", methods=["POST"])
def api_luu(pid):
    # Việc nền giữ bản dự án trong bộ nhớ và ghi đè cả bản đó sau mỗi bước: lưu lúc này là chỗ sửa mất âm thầm (13/11/2026).
    if pc.du_an_dang_lam(pid):
        return jsonify({"ok": False, "error": pc.DANG_LAM_LOI, "ma_loi": "dang_lam"}), 409
    try:
        da, moi = mo(pid), request.get_json(silent=True) or {}
        if moi.get("may_clip") in MAY_CLIP:
            da["may_clip"] = moi["may_clip"]
        if moi.get("cach_quay"):
            ap_cach_quay(da, str(moi["cach_quay"]))
        if moi.get("kieu_dung"):
            ap_kieu_dung(da, str(moi["kieu_dung"]))
        for khoa in ("ten", "loi_ke", "giong", "boi_canh", "tay"):
            if khoa in moi:
                da[khoa] = str(moi[khoa] or "")[:6000]
        if "so_giay" in moi:
            da["so_giay"] = max(15, min(90, int(moi["so_giay"] or SO_GIAY)))
        if "giay_clip" in moi:
            da["giay_clip"] = int(moi["giay_clip"] or 10)
        if "moc" in moi:
            da["moc"] = str(moi["moc"] or "").strip()[:60]
        if "khoanh" in moi:
            da["khoanh"] = [d.strip()[:40] for d in str(moi["khoanh"] or "").split("\n") if d.strip()][:3]
        if moi.get("kieu_chu") in ("phu_de", "cum"):
            da["kieu_chu"] = moi["kieu_chu"]
        if isinstance(moi.get("binh_luan"), dict):
            da["binh_luan"] = {"ten": str(moi["binh_luan"].get("ten") or "").strip().lstrip("@")[:30],
                               "chu": str(moi["binh_luan"].get("chu") or "").strip()[:160]}
        if "tu_soat" in moi:
            da["tu_soat"] = bool(moi["tu_soat"])
        if "noi_canh" in moi:
            da["noi_canh"] = bool(moi["noi_canh"])
        if "cat_nhanh" in moi:
            da["cat_nhanh"] = bool(moi["cat_nhanh"])
        if "flow_model" in moi:
            import flow_may
            da["flow_model"] = moi["flow_model"] if moi["flow_model"] in flow_may.CAC_MODEL else ""
        if moi.get("tieng_clip") in TIENG_CLIP:
            da["tieng_clip"] = moi["tieng_clip"]
        if "phu_de" in moi:
            da["phu_de"] = bool(moi["phu_de"])
        if moi.get("loai_sp") in LOAI_SP and moi["loai_sp"] != (da.get("loai_sp") or "hop"):
            # Đổi loại sản phẩm là đổi cả trình tự động tác: clip chưa sửa tay được xếp lại và phải quay lại.
            da["loai_sp"] = moi["loai_sp"]
            for canh in da.get("canh") or []:
                if not canh.get("tu_sua"):
                    canh.update(hanh_dong="", trang_thai="cho", file="", tranh="")
        if moi.get("bo_cuc") in BO_CUC:
            da["bo_cuc"] = moi["bo_cuc"]
        if moi.get("the_kieu") in THE_KIEU:
            da["the_kieu"] = moi["the_kieu"]
        if "nhan_kenh" in moi:
            da["nhan_kenh"] = {"ten": str(moi["nhan_kenh"] or "").strip()[:40], "anh": ""}
        if "ten_sp" in moi:
            da["san_pham"]["ten"] = str(moi["ten_sp"] or "")[:80]
        if moi.get("anh_chinh") in da["san_pham"]["anh"] and da["san_pham"]["anh"][0] != moi["anh_chinh"]:
            # Ảnh đứng đầu là ảnh gửi làm mẫu chính.
            da["san_pham"]["anh"].remove(moi["anh_chinh"])
            da["san_pham"]["anh"].insert(0, moi["anh_chinh"])
            _doi_san_pham(da)
        if isinstance(moi.get("the"), dict):
            # Ô "mẩu chuyện đính" nhận nhiều dòng: mỗi dòng là một thẻ, hiện lần lượt theo lời kể.
            doan = [d.strip()[:220] for d in str(moi["the"].get("chu") or "").split("\n") if d.strip()][:SO_THE_TOI_DA]
            da["the"] = {"tac_gia": str(moi["the"].get("tac_gia") or "")[:40], "chu": doan[0] if doan else "", "cac_doan": doan}
        if "concept_chon" in moi:
            ap_concept(da, moi["concept_chon"])
        elif "so_giay" in moi and any(c.get("cc") for c in da.get("canh") or []):
            import concept as cc_
            if cc_.lay(da.get("concept_chon") or ""):
                _canh_concept(da, cc_.lay(da["concept_chon"]))  # đổi số giây: đủ clip theo cảnh của concept
        if "bai" in moi:
            if moi["bai"]:
                ap_concept(da, "")  # kể chuyện Threads thì bỏ concept
            bai = next((b for b in kho_bai() if b["id"] == moi["bai"]), None)
            if (bai or {}).get("id") != (da.get("bai") or {}).get("id"):
                # Đổi bài thì lời kể và thẻ đính của bài cũ không còn đúng nữa.
                da["loi_ke"], da["giong_doc"] = "", None
                dau = ((bai or {}).get("noi_dung") or "")[:180]
                da["the"] = {"tac_gia": (bai or {}).get("tac_gia") or "", "chu": dau, "cac_doan": [dau] if dau else []}
            da["bai"] = bai
        for sua in moi.get("canh") or []:
            canh = next((c for c in da.get("canh") or [] if c["ma"] == sua.get("ma")), None)
            if not canh:
                continue
            if sua.get("hanh_dong") in _HD and sua["hanh_dong"] != canh["hanh_dong"]:
                canh.update(hanh_dong=sua["hanh_dong"], tu_sua=False, trang_thai="cho", file="", tranh="")  # đổi động tác thì phải quay lại
                canh.pop("co_chat", None)  # cờ của đề cương cũ không còn đúng khi chọn động tác khác
            if "prompt" in sua and str(sua["prompt"]).strip() and str(sua["prompt"]).strip() != canh["prompt"]:
                canh.update(prompt=str(sua["prompt"]).strip()[:3000], tu_sua=True, trang_thai="cho", file="", tranh="")  # sửa câu lệnh cũng vậy
        lap_canh(da)
        return jsonify({"ok": True, "project": _ra(luu(da))})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


@bp.route("/api/review_sp/du_an/<pid>/anh", methods=["POST"])
def api_anh(pid):
    try:
        da = mo(pid)
        goc = os.path.join(_thu_muc(pid), "anh")
        os.makedirs(goc, exist_ok=True)
        for f in request.files.getlist("files"):
            duoi = os.path.splitext(f.filename or "")[1].lower()
            if duoi not in _ANH:
                continue
            ten = uuid.uuid4().hex[:10] + duoi
            f.save(os.path.join(goc, ten))
            da["san_pham"]["anh"].append(ten)
        da["san_pham"]["anh"] = da["san_pham"]["anh"][:6]
        return jsonify({"ok": True, "project": _ra(luu(da))})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


TEN_TAM = ("Video review mới", "Video review đầu tiên", "Video chưa đặt tên")


def _doi_san_pham(da: Dict) -> None:
    """Ảnh mẫu đổi thì clip đã quay không còn đúng sản phẩm: đưa hết về chưa quay. Video còn tên tạm thì đặt tên theo sản phẩm,
    để danh sách video không đầy "Video review mới" (07/11/2026)."""
    ten_sp = ((da.get("san_pham") or {}).get("ten") or "").strip()
    if ten_sp and (da.get("ten") or "") in TEN_TAM:
        da["ten"] = f"{ten_sp[:50]} · {time.strftime('%d/%m %H:%M')}"
    for canh in da.get("canh") or []:
        if canh.get("trang_thai") == "xong":
            canh.update(trang_thai="cho", file="", tranh="")


@bp.route("/api/review_sp/du_an/<pid>/san_pham", methods=["POST"])
def api_san_pham(pid):
    """Dán link sản phẩm: tool mở trang, lấy tên và ảnh sản phẩm về làm ảnh mẫu cho video này."""
    try:
        da = mo(pid)
        import lich_tu_dong as lt
        sp = lt.san_pham_cua_link((request.get_json(silent=True) or {}).get("url") or "")
        goc = os.path.join(_thu_muc(pid), "anh")
        shutil.rmtree(goc, ignore_errors=True)
        os.makedirs(goc)
        for ten in sp["anh"][:6]:
            shutil.copy2(lt._tep_sp(ten), os.path.join(goc, ten))
        da["san_pham"] = {"ten": sp["ten"], "anh": sp["anh"][:6], "link": sp["link"]}
        if da.get("ten", "").startswith("Video ") and sp["ten"]:
            da["ten"] = sp["ten"][:80]
        _doi_san_pham(da)
        return jsonify({"ok": True, "project": _ra(luu(da))})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


@bp.route("/api/review_sp/du_an/<pid>/anh/<ten>", methods=["DELETE"])
def api_xoa_anh(pid, ten):
    try:
        da = mo(pid)
        if ten in da["san_pham"]["anh"]:
            da["san_pham"]["anh"].remove(ten)
            duong = os.path.join(_thu_muc(pid), "anh", ten)
            if os.path.isfile(duong):
                os.remove(duong)
        return jsonify({"ok": True, "project": _ra(luu(da))})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


# ── Bản thô cho brand duyệt (07/11/2026) ─────────────────────────────────────
# Người dùng: "Không cần ghép giọng đọc hay thread gì, chủ yếu cho brand họ coi clip đạt yêu cầu không". Bản thô = các clip nối thẳng
# theo thứ tự, không giọng, không chữ, không thẻ. Drive chỉ nhận video final. Kèm "công thức" (câu lệnh gửi thật, ảnh mẫu từng clip,
# kết quả AI soát, máy làm clip) để brand duyệt thì làm hàng loạt đúng như vậy.

def cong_thuc(da: Dict) -> Dict:
    canh = []
    for i, c in enumerate(da.get("canh") or [], 1):
        anh = _anh_cho_canh(da, c) or ""
        lenh = lenh_gui(da, c)
        if da.get("may_clip") == "gemini":
            lenh = lenh.replace("attached reference image", "attached photo").replace("reference image", "attached photo")
        canh.append({"scene": i, "file": c.get("file") or "", "anh_mau": os.path.basename(anh), "cau_lenh_gui": lenh,
                     "tranh": c.get("tranh") or "", "ai_soat": c.get("soat") or None})
    return {"luc": time.strftime("%Y-%m-%d %H:%M"), "du_an": da["id"], "ten": da.get("ten"), "san_pham": (da.get("san_pham") or {}).get("kho") or "",
            "loai_sp": da.get("loai_sp"), "concept": da.get("concept_chon") or "", "cach_quay": cach_quay(da), "scene": canh}


def ghep_tho(da: Dict, viec: Dict, len_drive: bool = True) -> Dict:
    """Nối thẳng các clip đã quay thành một video 720x1280, lưu tên mới mỗi lần; đưa lên Drive nếu đã nối; ghi công thức."""
    cac = [c for c in da.get("canh") or [] if c.get("trang_thai") == "xong" and c.get("file")]
    if not cac or len(cac) != len(da.get("canh") or []):
        con = [c["ma"][1:] for c in da.get("canh") or [] if c.get("trang_thai") != "xong"]
        raise RuntimeError(f"Clip {', '.join(con)} chưa quay xong nên chưa ghép được.")
    pc.buoc(viec, 80, "Đang ghép các clip thành bản thô")
    goc = _thu_muc(da["id"])
    ten = f"ban_tho_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    tam = os.path.join(goc, "tam_tho")
    shutil.rmtree(tam, ignore_errors=True)
    os.makedirs(tam)
    phan = []
    for i, c in enumerate(cac):  # bản thô giữ nguyên tiếng thật của clip (08/11/2026)
        phan.append(os.path.join(tam, f"p{i:02d}.mp4"))
        _chuan_hoa(os.path.join(goc, "clip", c["file"]), phan[-1], "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=24,setsar=1")
    vao = sum([["-i", x] for x in phan], [])
    loc = "".join(f"[{i}:v][{i}:a]" for i in range(len(phan))) + f"concat=n={len(phan)}:v=1:a=1[o][a]"
    pc.chay([pc.ffmpeg(), "-y", *vao, "-filter_complex", loc, "-map", "[o]", "-map", "[a]", "-c:v", "libx264", "-crf", "18",
             "-pix_fmt", "yuv420p", *_TIENG_RA, os.path.join(goc, ten)])
    shutil.rmtree(tam, ignore_errors=True)
    ban = {"file": ten, "luc": time.time(), "so_clip": len(cac), "cong_thuc": cong_thuc(da)}
    pc.ghi_json(os.path.join(goc, ten[:-4] + "_cong_thuc.json"), ban["cong_thuc"])
    if len_drive:
        import duyet_drive
        if duyet_drive.da_noi():
            pc.buoc(viec, 92, "Đang đưa bản thô lên Google Drive")
            try:
                ban["drive"] = duyet_drive.dua_len_tep(da, os.path.join(goc, ten), f"bản thô {len(da.get('ban_tho') or []) + 1}")
            except Exception as exc:  # noqa: BLE001 - Drive hỏng thì bản thô vẫn có trong máy
                ban["drive_loi"] = duyet_drive._loi(exc)
    da.setdefault("ban_tho", []).append(ban)
    return luu(da)


@bp.route("/api/review_sp/tep/<pid>/<path:ten>", methods=["GET"])
def api_tep(pid, ten):
    try:
        return send_from_directory(_thu_muc(pid), ten)
    except ValueError as exc:
        return _loi(exc, 404)


@bp.route("/api/review_sp/kho_bai", methods=["POST"])
def api_them_bai():
    moi = request.get_json(silent=True) or {}
    try:
        if (moi.get("url") or "").strip() and not (moi.get("noi_dung") or "").strip():
            doc = doc_duong_dan_threads(moi["url"])
            bai = them_bai_tu_link(doc)
        else:
            bai = them_bai(moi.get("tac_gia") or "", moi.get("noi_dung") or "", moi.get("url") or "")
        return jsonify({"ok": True, "post": bai, "threads": kho_bai()})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


@bp.route("/api/review_sp/kho_bai/trang", methods=["POST"])
def api_lay_nhieu_bai():
    """Lấy nhiều bài từ một trang Threads, chạy nền (mỗi bài khoảng 10 giây)."""
    moi = request.get_json(silent=True) or {}
    url = str(moi.get("url") or "").strip()
    if not re.search(r"threads\.(?:com|net)/@[\w.]+", url):
        return _loi("Dán link trang cá nhân Threads, dạng https://www.threads.com/@ten.tai.khoan")
    if pc.moi_viec_dang_chay():
        return _loi("Máy đang làm một việc khác, chờ xong đã.")
    toi_da = max(1, min(30, int(moi.get("toi_da") or 10)))
    it_nhat = max(0, min(400, int(moi.get("it_nhat_tieng") or 60)))

    def lam(viec):
        viec["result"] = lay_nhieu_bai(url, toi_da, it_nhat, viec)
    return jsonify({"ok": True, "job_id": pc.bat_dau(XUONG, "lay_bai", lam)})


@bp.route("/api/review_sp/anh_bai/<ten>", methods=["GET"])
def api_anh_bai(ten):
    return send_from_directory(os.path.join(KHO, "anh_bai"), os.path.basename(ten))


@bp.route("/api/review_sp/kho_bai/<ma>", methods=["DELETE"])
def api_xoa_bai(ma):
    xoa_bai(ma)
    return jsonify({"ok": True, "threads": kho_bai()})


@bp.route("/api/review_sp/du_an/<pid>/lam", methods=["POST"])
def api_lam(pid):
    """Chạy một bước ở luồng nền: viet_loi, giong, clip (kèm `canh` để quay lại một clip), dung, tat_ca."""
    moi = request.get_json(silent=True) or {}
    viec_gi = moi.get("buoc") or "tat_ca"
    try:
        da = mo(pid)
        if pc.moi_viec_dang_chay():
            return _loi("Máy đang làm một việc khác (xưởng này, xưởng kia hoặc lịch tự động). Chờ xong hoặc bấm Dừng.")
        if viec_gi == "viet_loi" and not da.get("bai"):
            return _loi("Chưa chọn bài Threads nào để kể.")
        if viec_gi == "viet_concept" and not da.get("concept_chon"):
            return _loi("Chưa chọn concept.")
        thieu = [t for t in thieu_gi(da) if viec_gi != "giong" or "ảnh" not in t]
        if viec_gi in ("tat_ca", "dung", "giong", "mo_dau") and thieu:
            return _loi("Còn thiếu: " + ", ".join(thieu) + ".")
        if viec_gi in ("clip", "ban_tho") and not da["san_pham"]["anh"]:
            return _loi("Chưa có ảnh sản phẩm.")
    except Exception as exc:  # noqa: BLE001
        return _loi(exc, 404)

    def lam(viec):
        da = mo(pid)
        if viec_gi == "viet_loi":
            pc.buoc(viec, 20, "AI đang viết lời kể cho vừa số giây")
            luu(viet_loi(da))
        elif viec_gi == "viet_concept":
            pc.buoc(viec, 20, "AI đang viết lời theo concept")
            luu(viet_theo_concept(da))
        elif viec_gi == "giong":
            doc_loi(da, viec)
        elif viec_gi == "clip":
            quay(da, viec, moi.get("canh") or None)
        elif viec_gi == "mo_dau":
            lam_mo_dau(da, viec)
        elif viec_gi == "ban_tho":
            # Quay nốt clip chưa xong rồi nối thẳng, không cần lời kể hay giọng.
            if any(c.get("trang_thai") != "xong" for c in da.get("canh") or []):
                da = quay(da, viec)
            if viec.get("huy"):
                return
            ghep_tho(da, viec, moi.get("drive", True) is not False)
        elif viec_gi == "dung":
            dung(da, viec)
        else:
            if not (da.get("loi_ke") or "").strip() and da.get("concept_chon"):
                pc.buoc(viec, 2, "AI đang viết lời theo concept")
                da = luu(viet_theo_concept(da))
            doc_loi(da, viec)
            quay(da, viec)
            hong = [c["ma"][1:] for c in da["canh"] if c.get("trang_thai") != "xong"]
            if viec.get("huy"):
                return
            if hong:
                raise RuntimeError(f"Clip {', '.join(hong)} chưa quay được nên chưa dựng. Xem lý do ở từng clip rồi bấm Làm video lần nữa, clip xong rồi không quay lại.")
            dung(da, viec)
    return jsonify({"ok": True, "job_id": pc.bat_dau(XUONG, viec_gi, lam, pid)})


@bp.route("/api/review_sp/viec/<ma>", methods=["GET"])
def api_viec(ma):
    viec = pc.xem_viec(ma)
    return jsonify({"ok": bool(viec), "job": viec}), (200 if viec else 404)


@bp.route("/api/review_sp/viec/<ma>/huy", methods=["POST"])
def api_huy(ma):
    return jsonify({"ok": pc.huy_viec(ma)})
