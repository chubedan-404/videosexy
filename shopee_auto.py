"""
SHOPEE AUTOPILOT (13/11/2026): dán link Shopee -> làm video sản phẩm -> đăng Reel lên Fanpage -> comment link affiliate.

Không làm lại gì từ đầu, chỉ nối các bộ máy đã có:
- Lấy sản phẩm: `shopee_aff.san_pham_api` (API Affiliate chính thức, cần khoá) rồi cất vào Product Library (`kho_anh`). Chưa có khoá
  thì người dùng thả ảnh và gõ tên ngay trên dòng của link. KHÔNG đọc trang Shopee hay API nội bộ của Shopee (13/11/2026 trợ lý định
  thử, lệnh bị chặn vì là cào trang trái ý Shopee; đừng thêm lại). Ngoại lệ 14/11/2026: người dùng nghe rủi ro và chọn dùng tiện ích
  Chrome `tien_ich_shopee/` chạy trong Chrome CỦA HỌ, chỉ trên affiliate.shopee.vn (`tao_tu_web`). KHÔNG mở shopee.vn bằng máy: thử
  một trang sản phẩm là bị xác minh ngay (scene=crawler_item).
- Làm video: một mẻ của Batch Product Videos (`lam_hang_loat.tao` + `chay`), mỗi link một video, cùng concept, giọng, máy làm clip.
- Link aff: `shopee_aff.tao_mot` với sub_id `autopilot`, mã lượt, mã sản phẩm (Report cộng hoa hồng theo đó). Chưa có khoá thì người
  dùng dán link aff tạo ở affiliate.shopee.vn vào dòng đó.
- Đăng: `kenh_dang.dang` kiểu Reel lên MỘT Fanpage. Comment: Graph API `/{video_id}/comments` bằng token của trang (quyền
  pages_manage_engagement). Reel vừa đăng có lúc chưa nhận comment: thử lại vài lần.
- Mặc định "duyệt trước khi đăng": video xong thì chờ người dùng bấm Đăng từng cái hoặc Đăng hết.

Máy chủ /api/shopee_auto/*, việc nền của xuong_chung (XUONG = "shopee_auto"), dữ liệu downloads/shopee_auto/<mã>.json.
"""
import os
import re
import threading
import time
import uuid
from typing import Dict, List, Optional

import requests
from flask import Blueprint, jsonify, request, send_file

import xuong_chung as pc

XUONG = "shopee_auto"
# Máy làm clip mặc định của Shopee: Muse (người dùng 14/11/2026 "chỉnh lại muse là mặc định"; Muse dư lượt, Flow tốn credit thật).
# Product Video của PlayAh vẫn theo review_sp.CACH_MAC_DINH.
MAY_MAC_DINH = "muse"
KHO = os.path.join(pc.TAI_VE, "shopee_auto")
TOI_DA_LINK = 20
# Mẫu comment (14/11/2026 người dùng: "Mua hàng tại [link] ..... giảm xx%..... nghĩ thêm nhé"). {giam}, {gia} do CODE điền từ số
# Shopee của dòng (`dien_comment`); món không giảm giá thì chỉ dùng mẫu không có {giam}, kẻo nói sai. Không emoji.
MAU_COMMENT = [
    "Mua hàng tại {link}, đang giảm {giam}% còn khoảng {gia}. Giá có thể đổi theo thời điểm nha.",
    "Món trong video đây nè: {link} (giảm {giam}%, còn khoảng {gia}, bấm vào xem giá mới nhất)",
    "Ai hỏi món trong video thì mua tại {link} nha, đang giảm {giam}%.",
    "Link mua đây: {link}. Đang giảm {giam}%, còn khoảng {gia}, giá có thể đổi nên xem trong link nha.",
    "Mua hàng tại {link}, giá khoảng {gia}, có thể đổi theo thời điểm nha.",
    "Món trong video đây nè: {link} (khoảng {gia}, bấm vào xem giá mới nhất)",
    "Ai hỏi món trong video thì mua tại {link} nha.",
]


def _gia_gon(gia) -> str:
    if not isinstance(gia, (int, float)) or gia <= 0:
        return ""
    return f"{round(gia / 1000):,} nghìn".replace(",", ".") if gia < 1_000_000 else f"{gia / 1_000_000:.1f} triệu".replace(".", ",")


def dien_comment(mau: List[str], m: Dict, link: str, thu: int) -> str:
    """Chọn mẫu theo số thật của dòng (có giảm thì mẫu có {giam}; không giảm thì mẫu không có {giam}; không giá thì mẫu không
    có {gia}), xoay vòng theo thứ tự dòng, rồi điền."""
    giam = m.get("giam") if isinstance(m.get("giam"), (int, float)) and 0 < m["giam"] < 100 else None
    gia = _gia_gon(m.get("gia"))
    hop = [x for x in mau if ("{giam}" in x) == bool(giam) and (gia or "{gia}" not in x)] \
        or [x for x in MAU_COMMENT if ("{giam}" in x) == bool(giam) and (gia or "{gia}" not in x)] or ["Mua hàng tại {link}"]
    chu = hop[thu % len(hop)]
    return (chu.replace("{link}", link).replace("{giam}", str(int(giam)) if giam else "").replace("{gia}", gia)
            .replace("{ten}", m.get("ten") or "sản phẩm"))


CHU_THICH = "{ten}\n\nLink mua ở phần bình luận."
LAN_THU_COMMENT = 4
CHO_COMMENT = 20  # giây giữa hai lần thử comment

_khoa = threading.RLock()
bp = Blueprint("shopee_auto", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# LƯU
# ─────────────────────────────────────────────────────────────────────────────

def _ma_hop_le(ma: str) -> bool:
    return bool(re.fullmatch(r"sa_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", ma or ""))


def _tep(ma: str) -> str:
    if not _ma_hop_le(ma):
        raise ValueError("Mã lượt không hợp lệ.")
    return os.path.join(KHO, ma + ".json")


def mo(ma: str) -> Dict:
    d = pc.doc_json(_tep(ma))
    if not d:
        raise FileNotFoundError("Không có lượt này.")
    return d


def luu(d: Dict) -> Dict:
    with _khoa:
        pc.ghi_json(_tep(d["id"]), d)
    return d


def _sua_muc(ma: str, thu: int, **moi) -> Dict:
    with _khoa:
        d = mo(ma)
        m = next((x for x in d["muc"] if x["thu"] == thu), None)
        if m is None:
            raise ValueError("Không có dòng này.")
        m.update(moi)
        luu(d)
        return m


def ds() -> List[Dict]:
    ra = []
    if os.path.isdir(KHO):
        for ten in sorted(os.listdir(KHO), reverse=True):
            if ten.endswith(".json") and _ma_hop_le(ten[:-5]):
                d = pc.doc_json(os.path.join(KHO, ten)) or {}
                if d.get("id"):
                    ra.append({"id": d["id"], "ten": d.get("ten") or "", "tao_luc": d.get("tao_luc"), "so": len(d.get("muc") or []),
                               "da_dang": sum(1 for m in d.get("muc") or [] if m.get("trang_thai") == "da_dang")})
    return ra[:40]


# ─────────────────────────────────────────────────────────────────────────────
# TẠO LƯỢT, LẤY SẢN PHẨM
# ─────────────────────────────────────────────────────────────────────────────

def _cai_dat(moi: Dict) -> Dict:
    import review_sp as rv
    import lam_hang_loat as lhl
    import kenh_dang
    so_giay = int(moi.get("so_giay") or 30)
    trang = str(moi.get("trang") or "")
    co_trang = {k["id"][3:] for k in kenh_dang.ds_kenh() if k["nen"] == "fb"}
    if trang and trang not in co_trang:
        raise ValueError("Fanpage này chưa nối trong Cài đặt.")
    mau = [str(x).strip()[:300] for x in (moi.get("mau_comment") or []) if "{link}" in str(x)] or list(MAU_COMMENT)
    # Kênh đăng thêm (15/11/2026): YouTube Shorts / TikTok đã nối, đăng cùng video sau khi Reel lên Fanpage.
    co_them = {k["id"] for k in kenh_dang.ds_kenh() if k["nen"] in NEN_THEM}
    them = [str(x) for x in (moi.get("kenh_them") or []) if str(x) in co_them and str(x) != "fb:" + trang][:8]
    return {"so_giay": so_giay if so_giay in lhl.SO_GIAY else 30,
            "cach_quay": rv.tim_cach(str(moi.get("cach_quay") or MAY_MAC_DINH))["id"],
            "giong": str(moi.get("giong") or ""), "concept": [str(x) for x in moi.get("concept") or []][:8],
            "trang": trang, "duyet_truoc": moi.get("duyet_truoc") is not False, "dang_aff": _dang(moi.get("dang_aff")),
            "chu_thich": (str(moi.get("chu_thich") or "").strip() or CHU_THICH)[:600], "mau_comment": mau[:10],
            "kenh_them": them, "tu_hoc": moi.get("tu_hoc") is True,
            # Nuôi trang (16/11/2026, nuoi_trang.py): video xong KHÔNG đăng ngay, tool tự xếp lịch đăng từng Fanpage.
            "nuoi": moi.get("nuoi") is True}


def _cai_dat_luu() -> Dict:
    """Cài đặt người dùng lưu lần cuối (lịch từ khoá, 14/11/2026): trang mở ra chọn sẵn Fanpage, máy làm clip, giọng... thay vì
    Fanpage đầu danh sách và máy Flow mặc định (tốn credit)."""
    try:
        import shopee_tu_khoa
        return dict(shopee_tu_khoa.doc().get("cai_dat") or {})
    except Exception:  # noqa: BLE001
        return {}


def _dang(ma) -> str:
    """Kiểu video affiliate (kich_ban_aff.DANG), người dùng chọn ở ô "Kiểu video"; lạ thì mặc định."""
    import kich_ban_aff as kba
    return ma if ma in kba.DANG else kba.DANG_MAC_DINH


def _ds_giong() -> Dict:
    """Giọng Edge đứng đầu (giọng nữ HoaiMy là mặc định cho video Shopee, người dùng chọn 14/11/2026), rồi tới Kho giọng."""
    g = pc.ds_giong()
    return {**g, "installed": True, "voices": list(pc.GIONG_EDGE) + list(g.get("voices") or [])}


def nhap_san_pham(m: Dict) -> Dict:
    """Lấy tên, giá, hoa hồng, ảnh của một dòng qua API chính thức rồi cất vào Product Library. Không có khoá thì để chờ ảnh."""
    import kho_anh
    import shopee_aff as sa
    if not sa.doc_khoa()["co_khoa"]:
        return {**m, "trang_thai": "cho_sp", "ghi": "Chưa có khoá Shopee: thả ảnh và gõ tên sản phẩm cho dòng này."}
    if not m.get("item_id"):
        return {**m, "trang_thai": "cho_sp", "ghi": "Không đọc được mã sản phẩm trong link: thả ảnh và gõ tên."}
    try:
        sp = sa.san_pham_api(m["item_id"], m.get("shop_id") or "")
    except sa.LoiShopee as e:
        return {**m, "trang_thai": "cho_sp", "ghi": f"Shopee chưa trả thông tin ({e}). Thả ảnh và gõ tên để làm tiếp."}
    if not sp:
        return {**m, "trang_thai": "cho_sp", "ghi": "Sản phẩm này không có trong chương trình affiliate (không có hoa hồng)."}
    ten = (sp["ten"] or "Sản phẩm Shopee")[:80]
    if sp.get("anh"):
        try:
            kho_anh.them(kho_anh._tai_anh(sp["anh"]), ten, tags=["shopee"], nguon=m["link"])
        except Exception as e:  # noqa: BLE001
            return {**m, "ten": ten, "trang_thai": "cho_sp", "ghi": f"Tải ảnh sản phẩm hỏng ({str(e)[:80]}). Thả ảnh vào dòng này."}
    gia = f"Giá khoảng {int(sp['gia']):,}đ".replace(",", ".") if sp.get("gia") else ""
    kho_anh.dat_thong_tin(ten, mo_ta=" ".join(x for x in (ten + ".", gia) if x), link=m["link"])
    return {**m, "ten": ten, "kho_ten": ten, "gia": sp.get("gia"), "ty_le": sp.get("ty_le"), "hoa_hong": sp.get("hoa_hong"),
            "link_aff": m.get("link_aff") or sp.get("link_aff") or "", "trang_thai": "san_sang", "ghi": ""}


def tao(chu: str, cai_dat: Dict) -> Dict:
    import shopee_aff as sa
    links = sa.tach_link(chu)
    if not links:
        raise ValueError("Không thấy link Shopee nào (shopee.vn, s.shopee.vn, shp.ee).")
    if len(links) > TOI_DA_LINK:
        raise ValueError(f"Mỗi lượt tối đa {TOI_DA_LINK} link, bạn dán {len(links)}.")
    cd = _cai_dat(cai_dat)
    ma = time.strftime("sa_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    muc = []
    for i, goc in enumerate(links):
        try:
            link = sa.mo_link_rut_gon(goc)
        except ValueError:
            link = goc
        so = sa.tach_ma(link) or {}
        muc.append(nhap_san_pham({"thu": i, "link_goc": goc, "link": link.split("?")[0], "item_id": so.get("item_id", ""),
                                  "shop_id": so.get("shop_id", ""), "ten": "", "kho_ten": "", "trang_thai": "cho_sp", "ghi": "",
                                  "loi": "", "video": "", "link_aff": "", "dang": None, "comment": None}))
    return luu({"id": ma, "ten": f"Lượt {time.strftime('%H:%M %d/%m')} · {len(muc)} sản phẩm", "tao_luc": time.time(),
                "cai_dat": cd, "me_id": "", "muc": muc})


MIEN_ANH = ("susercontent.com", "shopeemobile.com")


def _anh_shopee(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower()
    return url.startswith("https://") and any(host == m or host.endswith("." + m) for m in MIEN_ANH)


def tao_tu_web(ds_sp: List[Dict], cai_dat: Dict) -> Dict:
    """
    Lượt mới từ tiện ích Chrome "Alex's Studio - Shopee Affiliate" (tien_ich_shopee/, 14/11/2026): tiện ích đọc trang
    affiliate.shopee.vn bằng tài khoản người dùng đang đăng nhập rồi gửi về tên, giá, % hoa hồng, ảnh chính, link sản phẩm và
    link aff có sub_id. Mỗi món một dòng đã điền sẵn: ảnh vào Product Library, link aff để comment dưới Reel.
    """
    import kho_anh
    import shopee_aff as sa
    sp_dung = [x for x in ds_sp or [] if isinstance(x, dict) and re.fullmatch(r"\d{1,20}", str(x.get("item_id") or ""))]
    if not sp_dung:
        raise ValueError("Tiện ích không gửi về sản phẩm nào.")
    if len(sp_dung) > TOI_DA_LINK:
        raise ValueError(f"Mỗi lượt tối đa {TOI_DA_LINK} sản phẩm.")
    cd = _cai_dat(cai_dat)
    ma = time.strftime("sa_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    muc = []
    for i, x in enumerate(sp_dung):
        link = str(x.get("link") or "").split("?")[0]
        so = sa.tach_ma(link) if sa.la_link_shopee(link) else None
        aff = str(x.get("link_aff") or "").strip()
        ten = re.sub(r"\s+", " ", str(x.get("ten") or "")).strip()[:80]
        gia = x.get("gia") if isinstance(x.get("gia"), (int, float)) else None
        m = {"thu": i, "link_goc": link, "link": link if so else "", "item_id": str(x["item_id"]), "shop_id": (so or {}).get("shop_id", ""),
             "ten": ten, "kho_ten": "", "gia": gia, "ty_le": x.get("ty_le") if isinstance(x.get("ty_le"), (int, float)) else None,
             "giam": x.get("giam") if isinstance(x.get("giam"), (int, float)) else None, "da_ban": str(x.get("da_ban") or "")[:40],
             "trang_thai": "cho_sp", "ghi": "", "loi": "", "video": "", "link_aff": aff if aff and sa.la_link_shopee(aff) else "",
             "dang": None, "comment": None, "nguon": "trang_aff"}
        ghi = []
        if x.get("loi"):
            ghi.append(f"Tiện ích báo: {str(x['loi'])[:150]}")
        if not m["link_aff"]:
            ghi.append("Chưa có link aff: dán link vào ô dưới.")
        elif not x.get("co_sub"):
            ghi.append("Link aff chưa gắn được sub_id (Report sẽ không tách riêng video này).")
        anh = str(x.get("anh") or "")
        if ten and _anh_shopee(anh):
            try:
                kho_anh.them(kho_anh._tai_anh(anh), ten, tags=["shopee"], nguon=m["link"] or anh)
                gia_chu = f"Giá khoảng {f'{int(gia):,}'.replace(',', '.')}đ lúc lấy, có thể đổi" if gia else ""
                kho_anh.dat_thong_tin(ten, mo_ta=" ".join(s for s in (ten + ".", gia_chu) if s), link=m["link"])
                m.update(kho_ten=ten, trang_thai="san_sang")
            except Exception as e:  # noqa: BLE001
                ghi.append(f"Tải ảnh hỏng ({str(e)[:80]}): thả ảnh vào dòng này.")
        else:
            ghi.append("Chưa có ảnh: thả ảnh và gõ tên cho dòng này.")
        m["ghi"] = " ".join(ghi)
        muc.append(m)
    return luu({"id": ma, "ten": f"Lượt {time.strftime('%H:%M %d/%m')} · {len(muc)} sản phẩm (trang aff)", "tao_luc": time.time(),
                "cai_dat": cd, "me_id": "", "muc": muc})


def them_anh(ma: str, thu: int, ten: str, teps: List[bytes]) -> Dict:
    """Chưa có khoá: người dùng thả ảnh và gõ tên sản phẩm cho một dòng. Ảnh vào Product Library dưới đúng tên đó."""
    import kho_anh
    ten = (ten or "").strip()[:80]
    if not ten:
        raise ValueError("Gõ tên sản phẩm trước.")
    if not teps:
        raise ValueError("Thả ít nhất một ảnh sản phẩm.")
    d = mo(ma)
    m = next((x for x in d["muc"] if x["thu"] == thu), None)
    if m is None:
        raise ValueError("Không có dòng này.")
    for du_lieu in teps[:6]:
        kho_anh.them(du_lieu, ten, tags=["shopee"], nguon=m["link"])
    kho_anh.dat_thong_tin(ten, link=m["link"])
    return _sua_muc(ma, thu, ten=ten, kho_ten=ten, trang_thai="san_sang" if m["trang_thai"] == "cho_sp" else m["trang_thai"], ghi="")


# ─────────────────────────────────────────────────────────────────────────────
# LÀM VIDEO
# ─────────────────────────────────────────────────────────────────────────────

def chay(ma: str, viec: Dict) -> Dict:
    """Mọi dòng đã có sản phẩm mà chưa có video: gom thành một mẻ Batch Product Videos, làm, rồi gắn video về từng dòng."""
    import lam_hang_loat as lhl
    d = mo(ma)
    cd = d["cai_dat"]
    # "cho" = lượt trước bị huỷ giữa chừng (14/11/2026: huỷ xong bấm Làm video lại không được vì thiếu trạng thái này).
    lam = [m for m in d["muc"] if m["trang_thai"] in ("san_sang", "loi", "cho") and m.get("kho_ten") and not m.get("video")]
    if not lam:
        raise RuntimeError("Không có dòng nào sẵn sàng làm video (dòng nào cũng cần tên và ảnh sản phẩm).")
    # 14/11/2026: dòng có giá làm video SĂN DEAL (kich_ban_aff): lời theo số thật, không nói đã mua/mở hộp, sản phẩm đứng một
    # mình. Đường mẻ Batch Product Videos (kịch bản PlayAh, tay cầm sản phẩm) chỉ còn cho dòng chưa có giá.
    import kich_ban_aff as kba
    deal = [m for m in lam if kba.dung_duoc(m)]
    lam = [m for m in lam if not kba.dung_duoc(m)]
    # Tiến độ có cấu trúc (15/11/2026, người dùng: "chờ trông vô vọng, không biết nó làm tới đâu"): video thứ mấy, bước nào,
    # giờ bắt đầu, thời gian các video đã xong (để ước còn bao lâu). Trang đọc viec["sa"]; dòng chữ viec["step"] chỉ là chi tiết.
    sa = viec["sa"] = {"hang": [m["thu"] for m in deal], "i": 0, "thu": None, "buoc": "", "bd": time.time(), "bd_video": 0,
                       "xong": [], "tb": _tg_tb()}
    for k, m in enumerate(deal):
        if viec.get("huy"):
            break
        _sua_muc(ma, m["thu"], trang_thai="dang_lam", loi="")
        sa.update(i=k, thu=m["thu"], buoc="viet", bd_video=time.time())
        nhan = f"Video {k + 1}/{len(deal)}"

        def bao(chu, n=nhan):
            c = chu.lower()
            sa["buoc"] = ("viet" if "viết" in c else "giong" if "giọng" in c else "quay" if "quay" in c else "dung" if "dựng" in c else sa["buoc"])
            viec["progress"] = 0
            pc.buoc(viec, None, f"{n}: {chu}")
        try:
            kq = kba.lam_video(m, cd, viec, bao=bao, pid=m.get("pid_aff") or "")
        except Exception as e:  # noqa: BLE001 - một video hỏng không kéo cả lượt
            _sua_muc(ma, m["thu"], trang_thai="cho" if viec.get("huy") else "loi", loi=str(e)[:300])
            continue
        if kq.get("file"):
            sa["xong"].append(round(time.time() - sa["bd_video"]))
            _ghi_tg(sa["xong"][-1])
            _sua_muc(ma, m["thu"], trang_thai="co_video", video=kq["file"], loi_ke=kq.get("loi_ke") or "", pid_aff=kq["pid"], dang_aff=kq.get("dang") or "san_deal", mo=kq.get("mo"),
                      tieu_de=kq.get("tieu_de") or "")
        else:
            _sua_muc(ma, m["thu"], trang_thai="cho", pid_aff=kq["pid"], loi="Chưa làm xong video.")
    if lam and not viec.get("huy"):
        sa.update(buoc="me", thu=None, hang=sa["hang"] + [m["thu"] for m in lam])
        pc.buoc(viec, 2, f"Tạo mẻ {len(lam)} video")
        me = lhl.tao({"san_pham": [m["kho_ten"] for m in lam], "so_luong": len(lam), "so_giay": cd["so_giay"],
                      "cach_quay": cd["cach_quay"], "giong": cd["giong"], "concept": cd["concept"], "kieu_loi": "gioi_thieu",
                      "ten": f"Shopee Autopilot · {d['ten']}"})
        with _khoa:
            d = mo(ma)
            d["me_id"] = me["id"]
            for k, m in enumerate(lam):
                x = next(y for y in d["muc"] if y["thu"] == m["thu"])
                x.update(trang_thai="dang_lam", me_thu=k, loi="")
            luu(d)
        try:
            lhl.chay(me["id"], viec)
        except Exception as e:  # noqa: BLE001 - từng video hỏng được ghi ở dưới; lỗi chung thì ghi cho mọi dòng chưa có video
            loi_chung = str(e)[:300]
        else:
            loi_chung = ""
        me = lhl.mo(me["id"])
        with _khoa:
            d = mo(ma)
            for x in d["muc"]:
                if x.get("trang_thai") != "dang_lam" or x.get("me_thu") is None:
                    continue
                v = me["video"][x["me_thu"]]
                if v.get("trang_thai") == "xong" and v.get("file"):
                    x.update(trang_thai="co_video", video=v["file"], loi_ke=v.get("loi_ke") or "")
                else:
                    x.update(trang_thai="cho" if viec.get("huy") else "loi", loi=v.get("loi") or loi_chung or "Chưa làm xong video.")
            luu(d)
    d = mo(ma)
    sa.update(thu=None, buoc="xong", i=len(deal))
    if not d["cai_dat"].get("duyet_truoc") and not d["cai_dat"].get("nuoi") and not viec.get("huy"):
        for x in [m for m in d["muc"] if m["trang_thai"] == "co_video"]:
            sa.update(thu=x["thu"], buoc="dang")
            pc.buoc(viec, None, f"Đăng và comment: {x.get('ten') or x['link']}")
            dang_mot(ma, x["thu"])
    return mo(ma)


TEP_TG = None   # downloads/shopee_auto/thoi_gian.json: số giây làm xong mỗi video gần đây (ước thời gian còn lại)


def _tep_tg() -> str:
    return TEP_TG or os.path.join(KHO, "thoi_gian.json")


def _tg_tb() -> Optional[int]:
    """Trung vị số giây làm một video của 20 video gần nhất; chưa có thì None."""
    ds = sorted((pc.doc_json(_tep_tg()) or {}).get("giay") or [])
    return int(ds[len(ds) // 2]) if ds else None


def _ghi_tg(giay: int) -> None:
    try:
        ds = ((pc.doc_json(_tep_tg()) or {}).get("giay") or [])[-19:] + [int(giay)]
        os.makedirs(os.path.dirname(_tep_tg()), exist_ok=True)
        pc.ghi_json(_tep_tg(), {"giay": ds})
    except Exception:  # noqa: BLE001 - ghi thời gian hỏng không làm hỏng lượt
        pass


def bat_dau(ma: str) -> str:
    dang = pc.moi_viec_dang_chay()
    if dang:
        raise RuntimeError(f"Máy đang chạy một việc khác ({dang[0].get('step') or dang[0].get('kind')}). Chờ xong rồi bấm lại.")
    mo(ma)
    return pc.bat_dau(XUONG, "luot", lambda viec: chay(ma, viec), ma)


# ─────────────────────────────────────────────────────────────────────────────
# ĐĂNG VÀ COMMENT
# ─────────────────────────────────────────────────────────────────────────────

def _token_trang(trang: str) -> str:
    from facebook_publisher import FacebookPublisher, load_facebook_config
    p = next((x for x in load_facebook_config().get("pages") or [] if str(x.get("id")) == str(trang)), None)
    if not p or not p.get("access_token"):
        raise RuntimeError("Fanpage này chưa nối trong Cài đặt.")
    pub = FacebookPublisher(page_id=str(trang), access_token=p["access_token"])
    pub._ensure_page_token()
    return pub.access_token


def binh_luan(trang: str, ma_video: str, loi: str, so_lan: int = LAN_THU_COMMENT, cho: float = CHO_COMMENT) -> Dict:
    """Comment dưới Reel vừa đăng bằng token của trang. Reel còn đang xử lý thì Facebook chưa nhận comment: thử lại vài lần."""
    from facebook_publisher import GRAPH_API_BASE
    token, cuoi = _token_trang(trang), ""
    for lan in range(so_lan):
        r = requests.post(f"{GRAPH_API_BASE}/{ma_video}/comments", data={"message": loi, "access_token": token}, timeout=30)
        try:
            j = r.json()
        except ValueError:
            j = {}
        if r.status_code == 200 and j.get("id"):
            return {"ok": True, "id": j["id"]}
        cuoi = ((j.get("error") or {}).get("message") or f"HTTP {r.status_code}")[:200]
        if "permission" in cuoi.lower() or "(#200)" in cuoi or "(#10)" in cuoi:
            return {"ok": False, "loi": "Token của trang chưa có quyền comment (cần pages_manage_engagement). Facebook báo: " + cuoi}
        if lan + 1 < so_lan:
            time.sleep(cho)
    return {"ok": False, "loi": "Facebook chưa nhận comment: " + cuoi}


def _sub(ma: str, m: Dict) -> List[str]:
    return ["autopilot", ma[3:11], (m.get("item_id") or f"d{m['thu']}")[:20]]


def _lay_link(ma: str, m: Dict) -> str:
    """Link aff của dòng: link đã dán, không có thì tạo bằng khoá Shopee (nếu có). Mọi kênh dùng chung một link."""
    import shopee_aff as sa
    link = m.get("link_aff") or ""
    if not link and sa.doc_khoa()["co_khoa"]:
        link = sa.tao_mot(m["link"], _sub(ma, m))
        _sua_muc(ma, m["thu"], link_aff=link)
    return link


def dang_mot(ma: str, thu: int, them: bool = True) -> Dict:
    """Đăng video của một dòng lên Fanpage của lượt, rồi comment link aff. Đăng rồi thì không đăng lại.
    `them=False`: chỉ Fanpage chính, không tự đăng các kênh đăng thêm (dang_chon tự lo phần đó)."""
    import kenh_dang
    import shopee_aff as sa
    d = mo(ma)
    cd = d["cai_dat"]
    m = next((x for x in d["muc"] if x["thu"] == thu), None)
    if m is None:
        raise ValueError("Không có dòng này.")
    if (m.get("dang") or {}).get("ok"):
        raise ValueError("Video này đã đăng rồi.")
    if not m.get("video"):
        raise ValueError("Dòng này chưa có video.")
    if not cd.get("trang"):
        if cd.get("kenh_them"):   # không chọn Fanpage mà có kênh đăng thêm: chỉ đăng các kênh đó
            m = dang_them(ma, thu)
            if any((v or {}).get("ok") for v in (m.get("dang_them") or {}).values()):
                m = _sua_muc(ma, thu, trang_thai="da_dang")
            return m
        raise ValueError("Lượt này chưa chọn Fanpage để đăng.")
    link = m.get("link_aff") or ""
    if not link and sa.doc_khoa()["co_khoa"]:
        try:
            link = sa.tao_mot(m["link"], _sub(ma, m))
        except sa.LoiShopee as e:
            return _sua_muc(ma, thu, loi=f"Chưa tạo được link aff: {e}")
    if not link:
        return _sua_muc(ma, thu, loi="Chưa có link aff: dán link aff vào dòng này, hoặc nối khoá Shopee để tool tự tạo.")
    ten = m.get("ten") or "Sản phẩm"
    # Chú thích AI viết theo bài (kich_ban_aff, dạng review/thread) thắng mẫu chú thích của lượt (14/11/2026).
    td = (m.get("tieu_de") or "").strip()
    tieu, mo_ta = (td.split("\n")[0][:95], td) if td else (ten[:95], cd["chu_thich"].replace("{ten}", ten))
    got = kenh_dang.dang(os.path.join(pc.TAI_VE, m["video"]), tieu, mo_ta, ["fb:" + cd["trang"]])
    kq = (got or [{}])[0]
    kq["luc"] = time.time()   # hoc_aff chỉ tính Reel đăng từ 1 ngày
    if not kq.get("ok"):
        return _sua_muc(ma, thu, link_aff=link, dang=kq, loi="Đăng Facebook hỏng: " + str(kq.get("loi") or "")[:200])
    mau = cd.get("mau_comment") or MAU_COMMENT
    loi = dien_comment(mau, m, link, thu)
    cm = binh_luan(cd["trang"], kq.get("ma") or "", loi) if kq.get("ma") else {"ok": False, "loi": "Facebook không trả mã video."}
    m = _sua_muc(ma, thu, link_aff=link, dang=kq, comment={**cm, "chu": loi}, trang_thai="da_dang",
                 loi="" if cm.get("ok") else "Đã đăng nhưng chưa comment được: " + cm.get("loi", ""))
    return dang_them(ma, thu) if them and cd.get("kenh_them") else m


NEN_THEM = ("fb", "yt", "tt")   # fb: Fanpage khác ngoài Fanpage chính của lượt (15/11/2026)
CONG_KHAI_AFF = "Link trong bài là link tiếp thị liên kết Shopee: bạn mua qua link thì kênh nhận một phần hoa hồng, giá không đổi."


def binh_luan_yt(kenh: str, ma_video: str, chu: str) -> Dict:
    """Comment link aff dưới video YouTube vừa đăng (quyền youtube.force-ssl đã có sẵn trong vé của kênh)."""
    import youtube_upload as yt
    try:
        dv = yt._dich_vu(kenh)
        if dv is None:
            return {"ok": False, "loi": "Kênh YouTube này chưa cấp quyền cho tool."}
        r = dv.commentThreads().insert(part="snippet", body={"snippet": {"videoId": ma_video, "topLevelComment": {
            "snippet": {"textOriginal": chu}}}}).execute()
        return {"ok": True, "id": r.get("id") or ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "loi": str(e)[:200]}


def dang_them(ma: str, thu: int, kenh: Optional[List[str]] = None) -> Dict:
    """Đăng video của một dòng lên các kênh đăng thêm của lượt (YouTube Shorts, TikTok). Kênh nào đã đăng thì bỏ qua,
    kênh nào hỏng thì ghi lỗi riêng, không đụng tới Reel Facebook. Gọi lại được để đăng các kênh còn thiếu."""
    import kenh_dang
    d = mo(ma)
    cd = d["cai_dat"]
    m = next((x for x in d["muc"] if x["thu"] == thu), None)
    if m is None:
        raise ValueError("Không có dòng này.")
    if not m.get("video"):
        raise ValueError("Dòng này chưa có video.")
    try:
        link = _lay_link(ma, m)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Chưa tạo được link aff: {e}")
    if not link:
        raise ValueError("Dòng này chưa có link aff.")
    da = dict(m.get("dang_them") or {})
    ten = m.get("ten") or "Sản phẩm"
    td = (m.get("tieu_de") or "").strip()
    tieu = (td.split("\n")[0] if td else ten)[:88].rstrip()
    chu_link = dien_comment(cd.get("mau_comment") or MAU_COMMENT, m, link, thu)
    for kenh in (cd.get("kenh_them") or []) if kenh is None else kenh:
        if (da.get(kenh) or {}).get("ok") or kenh == "fb:" + str(cd.get("trang") or ""):
            continue
        nen = kenh.partition(":")[0]
        if nen == "fbv":
            # Video thường (không phải Reel) lên Fanpage, 16/11/2026: người dùng muốn so Reel với video thường.
            kq = _dang_fb_video(kenh[4:], os.path.join(pc.TAI_VE, m["video"]), (td.split("\n")[0] if td else ten)[:95],
                                td or cd["chu_thich"].replace("{ten}", ten))
            if kq.get("ok") and kq.get("ma"):
                kq["comment"] = {**binh_luan(kenh[4:], kq["ma"], chu_link), "chu": chu_link}
        elif nen == "fb":
            # Fanpage khác: đăng Reel cùng chú thích như Fanpage chính, comment cùng link aff bằng token của trang đó.
            mo_ta = td or cd["chu_thich"].replace("{ten}", ten)
            kq = (kenh_dang.dang(os.path.join(pc.TAI_VE, m["video"]), (td.split("\n")[0] if td else ten)[:95], mo_ta, [kenh]) or [{}])[0]
            if kq.get("ok") and kq.get("ma"):
                kq["comment"] = {**binh_luan(kenh[3:], kq["ma"], chu_link), "chu": chu_link}
        elif nen == "yt":
            # Shorts: mô tả có link aff + câu nói rõ là link tiếp thị liên kết; comment thêm link như Fanpage.
            mo_ta = "\n\n".join(x for x in (td or cd["chu_thich"].replace("{ten}", ten), chu_link, CONG_KHAI_AFF, "#shorts") if x)
            kq = (kenh_dang.dang(os.path.join(pc.TAI_VE, m["video"]), tieu + " #shorts", mo_ta, [kenh]) or [{}])[0]
            if kq.get("ok") and kq.get("ma"):
                kq["comment"] = binh_luan_yt(kenh.partition(":")[2], kq["ma"], chu_link)
        else:
            # TikTok: API chỉ cho gửi vào hộp nháp, người dùng mở app TikTok viết chú thích và bấm đăng.
            kq = (kenh_dang.dang(os.path.join(pc.TAI_VE, m["video"]), tieu, "", [kenh]) or [{}])[0]
        kq["luc"] = time.time()
        da[kenh] = kq
        m = _sua_muc(ma, thu, dang_them=da)
    return m


def _dang_fb_video(trang: str, duong: str, tieu: str, mo_ta: str) -> Dict:
    from facebook_publisher import FacebookPublisher, load_facebook_config
    p = next((x for x in load_facebook_config().get("pages") or [] if str(x.get("id")) == str(trang)), None)
    if not p:
        return {"ok": False, "loi": "Fanpage này không còn trong Cài đặt."}
    got = FacebookPublisher.publish_multi_pages(video_path=duong, title=tieu, description=mo_ta, target_pages_data=[p], post_type="video")
    kq = (got.get("results") or [{}])[0]
    if kq.get("status") == "success":
        return {"ok": True, "link": kq.get("url") or "", "ma": str(kq.get("video_id") or ""), "kenh": "fbv:" + str(trang), "luc": time.time()}
    return {"ok": False, "loi": str(kq.get("error") or got.get("error") or "Facebook không nhận video.")[:200]}


def dang_chon(ma: str, thu: int, kenh: List[str]) -> Dict:
    """Nút "Đăng…": đăng một dòng lên đúng các kênh người dùng tích. Kênh đã đăng thì bỏ qua (không đăng trùng)."""
    d = mo(ma)
    m = next((x for x in d["muc"] if x["thu"] == thu), None)
    if m is None:
        raise ValueError("Không có dòng này.")
    if not m.get("video"):
        raise ValueError("Dòng này chưa có video.")
    kenh = [str(k) for k in kenh or []]
    if not kenh:
        raise ValueError("Chưa chọn kênh nào.")
    chinh = "fb:" + str(d["cai_dat"].get("trang") or "")
    if chinh in kenh and not (m.get("dang") or {}).get("ok"):
        m = dang_mot(ma, thu, them=False)
    con = [k for k in kenh if k != chinh]
    if con:
        m = dang_them(ma, thu, con)
        if m.get("trang_thai") != "da_dang" and any((v or {}).get("ok") for v in (m.get("dang_them") or {}).values()):
            m = _sua_muc(ma, thu, trang_thai="da_dang")
    return m


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _loi(e, ma: int = 400):
    return jsonify({"ok": False, "error": str(e)[:400]}), ma


def _ra(d: Dict) -> Dict:
    return d


@bp.route("/api/shopee_auto/phien", methods=["GET"])
def api_phien():
    import review_sp as rv
    import lam_hang_loat as lhl
    import concept as cc_
    import kenh_dang
    import kich_ban_aff as kba
    import shopee_aff as sa
    cac = ds()
    ma = request.args.get("luot") or (cac[0]["id"] if cac else "")
    try:
        luot = mo(ma) if ma else None
    except (ValueError, FileNotFoundError):
        luot = None
    viec = pc.viec_dang_chay(XUONG)
    import hoc_aff
    so_reel = {k.split(":", 1)[1]: v for k, v in hoc_aff._doc().items() if luot and k.startswith(luot["id"] + ":")}
    return jsonify({"ok": True, "job_id": viec, "viec": pc.xem_viec(viec) if viec else None, "luot": luot, "cac_luot": cac, "so_reel": so_reel,
                    "co_khoa": sa.doc_khoa()["co_khoa"], "trang": [k for k in kenh_dang.ds_kenh() if k["nen"] == "fb"],
                    "kenh_them": [k for k in kenh_dang.ds_kenh() if k["nen"] in NEN_THEM],
                    "giong": _ds_giong(), "dang": [{"id": k, "ten": v} for k, v in kba.DANG.items()],
                    "dang_mac_dinh": kba.DANG_MAC_DINH, "cai_dat_luu": _cai_dat_luu(), "cach_quay": rv.CACH_QUAY, "cach_mac_dinh": MAY_MAC_DINH, "muse_han_muc": __import__("muse_han_muc").doc_luu(),
                    "concept": [{"id": c["id"], "ten": c["ten"]} for c in cc_.ds()], "so_giay": list(lhl.SO_GIAY),
                    "mau_comment": MAU_COMMENT, "chu_thich": CHU_THICH, "toi_da": TOI_DA_LINK})


@bp.route("/api/shopee_auto/tao", methods=["POST"])
def api_tao():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "luot": tao(t.get("chu") or "", t.get("cai_dat") or {})})
    except ValueError as e:
        return _loi(e)


@bp.route("/api/shopee_auto/tao_tu_web", methods=["POST"])
def api_tao_tu_web():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "luot": tao_tu_web(t.get("san_pham") or [], t.get("cai_dat") or {})})
    except ValueError as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/anh", methods=["POST"])
def api_anh(ma, thu):
    try:
        teps = [f.read() for f in request.files.getlist("anh")]
        return jsonify({"ok": True, "muc": them_anh(ma, thu, request.form.get("ten") or "", teps)})
    except (ValueError, FileNotFoundError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/sua", methods=["POST"])
def api_sua(ma, thu):
    t = request.get_json(silent=True) or {}
    moi = {}
    if "link_aff" in t:
        link = str(t.get("link_aff") or "").strip()
        import shopee_aff as sa
        if link and not sa.la_link_shopee(link):
            return _loi("Link aff phải là link Shopee (s.shopee.vn, shope.ee...).")
        moi["link_aff"] = link
    try:
        return jsonify({"ok": True, "muc": _sua_muc(ma, thu, **moi)})
    except (ValueError, FileNotFoundError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/xoa", methods=["POST"])
def api_xoa_muc(ma, thu):
    try:
        with _khoa:
            d = mo(ma)
            if any(m["thu"] == thu and m["trang_thai"] == "dang_lam" for m in d["muc"]):
                return _loi("Dòng này đang làm video.")
            d["muc"] = [m for m in d["muc"] if m["thu"] != thu]
            luu(d)
        return jsonify({"ok": True, "luot": d})
    except (ValueError, FileNotFoundError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/cai_dat", methods=["POST"])
def api_cai_dat(ma):
    try:
        with _khoa:
            d = mo(ma)
            d["cai_dat"] = _cai_dat({**d["cai_dat"], **(request.get_json(silent=True) or {})})
            luu(d)
        return jsonify({"ok": True, "luot": d})
    except (ValueError, FileNotFoundError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/chay", methods=["POST"])
def api_chay(ma):
    try:
        return jsonify({"ok": True, "job_id": bat_dau(ma)})
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/dang", methods=["POST"])
def api_dang(ma, thu):
    try:
        return jsonify({"ok": True, "muc": dang_mot(ma, thu)})
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/dang_them", methods=["POST"])
def api_dang_them(ma, thu):
    try:
        return jsonify({"ok": True, "muc": dang_them(ma, thu)})
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/dang_chon", methods=["POST"])
def api_dang_chon(ma, thu):
    try:
        return jsonify({"ok": True, "muc": dang_chon(ma, thu, (request.get_json(silent=True) or {}).get("kenh") or [])})
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/comment", methods=["POST"])
def api_comment_lai(ma, thu):
    """Reel đã đăng mà comment chưa vào (thiếu quyền, Facebook chậm): comment lại, không đăng lại video."""
    try:
        d = mo(ma)
        m = next(x for x in d["muc"] if x["thu"] == thu)
        if not (m.get("dang") or {}).get("ok") or not (m.get("dang") or {}).get("ma"):
            return _loi("Video này chưa đăng.")
        chu = (m.get("comment") or {}).get("chu") or (d["cai_dat"].get("mau_comment") or MAU_COMMENT)[0].replace(
            "{ten}", m.get("ten") or "").replace("{link}", m.get("link_aff") or "")
        cm = binh_luan(d["cai_dat"]["trang"], m["dang"]["ma"], chu, so_lan=1)
        return jsonify({"ok": True, "muc": _sua_muc(ma, thu, comment={**cm, "chu": chu},
                                                     loi="" if cm.get("ok") else "Đã đăng nhưng chưa comment được: " + cm.get("loi", ""))})
    except (StopIteration, ValueError, FileNotFoundError, RuntimeError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/tat_ca", methods=["GET"])
def api_tat_ca():
    """Mọi lượt kèm dòng và số Reel của từng lượt, cho chế độ "Tất cả" (một bảng chung, lọc theo ngày tạo)."""
    import hoc_aff
    so = hoc_aff._doc()
    ra = []
    for x in ds():
        try:
            d = mo(x["id"])
        except (ValueError, FileNotFoundError):
            continue
        d["so_reel"] = {k.split(":", 1)[1]: v for k, v in so.items() if k.startswith(d["id"] + ":")}
        ra.append(d)
    return jsonify({"ok": True, "luot": ra})


@bp.route("/api/shopee_auto/luot/<ma>/so", methods=["POST"])
def api_so(ma):
    """Hỏi lại Facebook lượt xem, reach của các Reel trong lượt (mỗi Reel cách nhau ít nhất 5 phút)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "Chỉ máy chạy tool bấm được."}), 403
    try:
        import hoc_aff
        return jsonify({"ok": True, **hoc_aff.cap_nhat_so(luot=ma, cach=300)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@bp.route("/api/shopee_auto/luot/<ma>/xoa", methods=["POST"])
def api_xoa(ma):
    try:
        if pc.viec_dang_chay(XUONG):
            return _loi("Đang chạy, dừng trước đã.")
        os.remove(_tep(ma))
        return jsonify({"ok": True})
    except (ValueError, FileNotFoundError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/viec/<job>", methods=["GET"])
def api_viec(job):
    v = pc.xem_viec(job)
    return jsonify({"ok": True, "viec": v}) if v else _loi("Không có việc này.", 404)


@bp.route("/api/shopee_auto/viec/<job>/huy", methods=["POST"])
def api_huy(job):
    return jsonify({"ok": pc.huy_viec(job)})


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/video", methods=["GET"])
def api_video(ma, thu):
    try:
        m = next(x for x in mo(ma)["muc"] if x["thu"] == thu)
        duong = os.path.realpath(os.path.join(pc.TAI_VE, m.get("video") or ""))
        if not m.get("video") or not duong.startswith(os.path.realpath(pc.TAI_VE)) or not os.path.isfile(duong):
            return _loi("Chưa có video.", 404)
        return send_file(duong, mimetype="video/mp4", conditional=True)
    except (StopIteration, ValueError, FileNotFoundError) as e:
        return _loi(e, 404)


# ─────────────────────────────────────────────────────────────────────────────
# BẢN CHO SHOPEE VIDEO (16/11/2026)
# Shopee Video cần video "gốc", sản phẩm rõ, không watermark nền tảng khác. Dựng lại đúng dự án video của dòng (cùng clip, giọng,
# phụ đề, thẻ) nhưng BỎ biển hãng + nhãn giá (nhan_aff) và nhãn tên Fanpage (nhan_kenh). Không quay thêm, không gọi AI.
# Người dùng tự đăng trong app Shopee (Shopee không có API đăng video).
# ─────────────────────────────────────────────────────────────────────────────

def chu_thich_shopee(m: Dict) -> str:
    """Chú thích dán vào Shopee Video: dòng đầu chú thích AI đã viết (hoặc tên), không link ngoài (Shopee gắn sản phẩm riêng)."""
    td = (m.get("tieu_de") or "").strip()
    dong = [x.strip() for x in td.split("\n") if x.strip() and "http" not in x and "link" not in x.lower()] if td else []
    than = "\n".join(dong[:3]) or (m.get("ten") or "Sản phẩm")[:120]
    the = [t for t in than.split() if t.startswith("#")]
    return than if the else than + "\n#shopeevideo #reviewshopee"


def lam_ban_shopee(ma: str, thu: int, viec: Dict) -> Dict:
    import copy
    import review_sp as rv
    m = next((x for x in mo(ma)["muc"] if x["thu"] == thu), None)
    if m is None:
        raise ValueError("Không có dòng này.")
    if not m.get("pid_aff"):
        raise ValueError("Dòng này chưa có dự án video (chưa làm video bằng tool), không dựng lại được.")
    da = copy.deepcopy(rv.mo(m["pid_aff"]))
    da.pop("nhan_aff", None)
    da["nhan_kenh"] = {}
    ten = f"shopee_video_{ma[3:]}_{thu}.mp4"
    rv._dung_mot(da, ten, viec, chu="Đang dựng bản sạch cho Shopee Video (bỏ biển hãng, nhãn giá, tên trang)", pct=30)
    return _sua_muc(ma, thu, ban_shopee={"file": ten, "luc": time.time(), "chu_thich": chu_thich_shopee(m)})


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/ban_shopee", methods=["POST"])
def api_ban_shopee(ma, thu):
    try:
        dang = pc.moi_viec_dang_chay()
        if dang:
            return _loi(f"Máy đang chạy một việc khác ({dang[0].get('step') or dang[0].get('kind')}). Chờ xong rồi bấm lại.")
        m = next((x for x in mo(ma)["muc"] if x["thu"] == thu), None)
        if not m or not m.get("pid_aff"):
            return _loi("Dòng này chưa có video do tool làm.")
        return jsonify({"ok": True, "job_id": pc.bat_dau(XUONG, "ban_shopee", lambda viec: lam_ban_shopee(ma, thu, viec), ma)})
    except (ValueError, FileNotFoundError) as e:
        return _loi(e)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/ban_shopee/tai", methods=["GET"])
def api_tai_ban_shopee(ma, thu):
    try:
        m = next(x for x in mo(ma)["muc"] if x["thu"] == thu)
        ban = m.get("ban_shopee") or {}
        duong = os.path.realpath(os.path.join(pc.TAI_VE, ban.get("file") or ""))
        if not ban.get("file") or not duong.startswith(os.path.realpath(pc.TAI_VE)) or not os.path.isfile(duong):
            return _loi("Chưa có bản cho Shopee Video.", 404)
        import unicodedata
        goc = unicodedata.normalize("NFD", (m.get("ten") or "san pham").replace("đ", "d").replace("Đ", "D"))
        ten = re.sub(r"[^0-9A-Za-z]+", "_", "".join(c for c in goc if unicodedata.category(c) != "Mn")).strip("_")[:40]
        return send_file(duong, mimetype="video/mp4", as_attachment=request.args.get("tai") == "1",
                         download_name=f"shopee_video_{ten or thu}.mp4", conditional=True)
    except (StopIteration, ValueError, FileNotFoundError) as e:
        return _loi(e, 404)


@bp.route("/api/shopee_auto/luot/<ma>/muc/<int:thu>/shopee_video", methods=["POST"])
def api_shopee_video(ma, thu):
    """Người dùng tự đăng trong app Shopee rồi ghi lại: đã đăng (và link nếu có)."""
    try:
        b = request.get_json(silent=True) or {}
        link = str(b.get("link") or "").strip()[:300]
        if link and not re.match(r"^https?://", link):
            return _loi("Link phải bắt đầu bằng http.")
        m = next(x for x in mo(ma)["muc"] if x["thu"] == thu)
        ban = dict(m.get("ban_shopee") or {})
        ban.update({"da_dang": bool(b.get("da_dang", True)), "link": link, "dang_luc": time.time() if b.get("da_dang", True) else None})
        return jsonify({"ok": True, "muc": _sua_muc(ma, thu, ban_shopee=ban)})
    except (StopIteration, ValueError, FileNotFoundError) as e:
        return _loi(e)
