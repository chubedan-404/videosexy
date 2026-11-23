"""
SHOPEE AFFILIATE (13/11/2026): link tiếp thị Shopee hàng loạt, tìm sản phẩm có hoa hồng, xem hoa hồng theo sub_id.

Chỉ dùng API chính thức của Shopee Affiliate Việt Nam (open-api.affiliate.shopee.vn/graphql), khoá là app_id + secret
người dùng lấy ở affiliate.shopee.vn > Open API. KHÔNG dùng máy chủ bên thứ ba (repo bcat95/shopee-aff có "Data API" ở
data.addlivetag.com bắt gửi cookie Shopee: không dùng), không cần cookie, không mở trình duyệt.

Ký yêu cầu: Authorization: SHA256 Credential=<app_id>, Timestamp=<giây>, Signature=sha256(app_id + giây + thân + secret).
Thân phải là ĐÚNG chuỗi gửi đi, nên tool tự dựng chuỗi JSON rồi gửi nguyên văn.

Khoá cất trong downloads/ket_noi.json (mục "shopee"), không bao giờ gửi ra trang. Lịch sử link ở downloads/shopee_aff/.
Máy chủ /api/shopee_aff/*.
"""
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from flask import Blueprint, jsonify, request

import ket_noi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "shopee_aff")
LICH_SU = os.path.join(KHO, "lich_su.json")
API = "https://open-api.affiliate.shopee.vn/graphql"
TOI_DA_LINK = 100       # mỗi lần tạo
GIU_LICH_SU = 1000
SO_LUONG = 4            # gọi song song; tài liệu Shopee: 8000 lượt mỗi giờ
CACH_BAO_CAO = 31       # tài liệu Shopee: hai lần hỏi Report không scrollId phải cách nhau hơn 30 giây
SAP_XEP = {"lien_quan": 1, "ban_chay": 2, "gia_cao": 3, "gia_thap": 4, "hoa_hong": 5}
TEN_MIEN = ("shopee.vn", "shp.ee", "shope.ee")
LOI_SHOPEE = {
    10020: "Shopee từ chối khoá (app_id hoặc secret sai, hoặc đồng hồ máy lệch quá 10 phút). Kiểm lại khoá ở affiliate.shopee.vn > Open API.",
    10030: "Gọi Shopee quá nhiều, chờ một lát rồi thử lại.",
    10031: "Shopee từ chối quyền truy cập.",
    10032: "Shopee báo mã affiliate không hợp lệ.",
    10033: "Tài khoản affiliate đang bị khoá.",
    10034: "Tài khoản affiliate nằm trong danh sách đen của Shopee.",
    10035: "Tài khoản chưa được cấp quyền Open API. Gửi yêu cầu ở affiliate.shopee.vn > Open API > liên hệ với chúng tôi.",
    11000: "Shopee báo lỗi bên họ, thử lại sau.",
    11001: "Shopee không nhận tham số gửi lên.",
    11002: "Lỗi liên kết tài khoản affiliate.",
}

_khoa = threading.RLock()
_bao_cao_luc = [0.0]
bp = Blueprint("shopee_aff", __name__)


class LoiShopee(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# KHOÁ
# ─────────────────────────────────────────────────────────────────────────────

def khoa() -> Dict[str, str]:
    s = ket_noi._tho().get("shopee") or {}
    return {"app_id": str(s.get("app_id") or "").strip(), "secret": str(s.get("secret") or "").strip()}


def doc_khoa() -> Dict:
    """Bản gửi cho trang: chỉ app_id (không phải bí mật) và đã có secret hay chưa."""
    k = khoa()
    return {"app_id": k["app_id"], "co_khoa": bool(k["app_id"] and k["secret"])}


def ghi_khoa(app_id: str, secret: str) -> Dict:
    app_id, secret = (app_id or "").strip(), (secret or "").strip()
    if app_id and not re.fullmatch(r"[0-9]{4,20}", app_id):
        raise ValueError("App ID của Shopee là một dãy số.")
    if secret and not re.fullmatch(r"[A-Za-z0-9]{8,128}", secret):
        raise ValueError("Secret chỉ gồm chữ và số, không có dấu cách.")
    with _khoa:
        d = ket_noi._tho()
        s = d.get("shopee") or {}
        if app_id:
            s["app_id"] = app_id
        if secret:
            s["secret"] = secret
        d["shopee"] = s
        ket_noi._ghi_tho(d)
    return doc_khoa()


# ─────────────────────────────────────────────────────────────────────────────
# GỌI API
# ─────────────────────────────────────────────────────────────────────────────

def chu_ky(app_id: str, secret: str, giay: int, than: str) -> str:
    return hashlib.sha256(f"{app_id}{giay}{than}{secret}".encode("utf-8")).hexdigest()


def _chuoi(s: str) -> str:
    """Chuỗi GraphQL: cú pháp chuỗi JSON dùng được nguyên."""
    return json.dumps(str(s), ensure_ascii=False)


def goi(query: str, k: Optional[Dict] = None, timeout: int = 20) -> Dict:
    k = k or khoa()
    if not (k["app_id"] and k["secret"]):
        raise LoiShopee("Chưa có khoá Shopee Affiliate. Dán App ID và Secret ở mục Khoá API.")
    than = json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":"))
    giay = int(time.time())
    tieu_de = {"Content-Type": "application/json",
               "Authorization": f"SHA256 Credential={k['app_id']}, Timestamp={giay}, "
                                f"Signature={chu_ky(k['app_id'], k['secret'], giay, than)}"}
    try:
        r = requests.post(API, data=than.encode("utf-8"), headers=tieu_de, timeout=timeout)
    except requests.RequestException as e:
        raise LoiShopee(f"Không gọi được Shopee: {e}") from e
    try:
        j = r.json()
    except ValueError as e:
        raise LoiShopee(f"Shopee trả về thứ không đọc được (mã {r.status_code}).") from e
    if j.get("errors"):
        e0 = j["errors"][0] or {}
        ext = e0.get("extensions") or {}
        ma = ext.get("code")
        goc = ext.get("message") or e0.get("message") or "lỗi không rõ"
        raise LoiShopee(f"{LOI_SHOPEE.get(ma, 'Shopee báo lỗi.')} ({ma}: {goc})" if ma else f"Shopee báo lỗi: {goc}")
    return j.get("data") or {}


# ─────────────────────────────────────────────────────────────────────────────
# LINK HÀNG LOẠT
# ─────────────────────────────────────────────────────────────────────────────

def la_link_shopee(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and any(host == m or host.endswith("." + m) for m in TEN_MIEN)


def tach_link(chu: str) -> List[str]:
    """Lấy mọi link Shopee trong một khối chữ (mỗi dòng một link, hay dán lẫn chữ), bỏ trùng, giữ thứ tự."""
    ra, thay = [], set()
    for m in re.finditer(r"https?://[^\s<>\"'，、]+", chu or ""):
        u = m.group(0).rstrip(".,;)]}")
        if la_link_shopee(u) and u not in thay:
            thay.add(u)
            ra.append(u)
    return ra


def chuan_sub(ds) -> List[str]:
    """Shopee nhận tối đa 5 sub_id, mỗi cái chữ/số/gạch dưới. Ký tự khác thành gạch dưới, bỏ ô trống."""
    if isinstance(ds, str):
        ds = re.split(r"[,\s]+", ds)
    ra = []
    for s in ds or []:
        s = re.sub(r"[^A-Za-z0-9_]", "_", str(s).strip())[:50].strip("_")
        if s:
            ra.append(s)
    return ra[:5]


def tao_mot(url: str, sub: List[str], k: Optional[Dict] = None) -> str:
    q = (f"mutation{{generateShortLink(input:{{originUrl:{_chuoi(url)},subIds:[{','.join(_chuoi(s) for s in sub)}]}})"
         f"{{shortLink}}}}")
    link = ((goi(q, k).get("generateShortLink") or {}).get("shortLink") or "").strip()
    if not link:
        raise LoiShopee("Shopee không trả link rút gọn.")
    return link


def tao_hang_loat(chu: str, sub) -> Dict:
    goc = tach_link(chu)
    if not goc:
        raise ValueError("Không thấy link Shopee nào (shopee.vn, shp.ee, s.shopee.vn).")
    if len(goc) > TOI_DA_LINK:
        raise ValueError(f"Mỗi lần tối đa {TOI_DA_LINK} link, bạn dán {len(goc)}.")
    sub, k = chuan_sub(sub), khoa()
    if not (k["app_id"] and k["secret"]):
        raise LoiShopee("Chưa có khoá Shopee Affiliate. Dán App ID và Secret ở mục Khoá API.")

    def mot(u):
        try:
            return {"goc": u, "link": tao_mot(u, sub, k), "loi": ""}
        except LoiShopee as e:
            return {"goc": u, "link": "", "loi": str(e)}

    with ThreadPoolExecutor(SO_LUONG) as ex:
        kq = list(ex.map(mot, goc))
    luc = time.time()
    them_lich_su([{**x, "sub": sub, "luc": luc} for x in kq if x["link"]])
    return {"ket_qua": kq, "sub": sub, "xong": sum(1 for x in kq if x["link"])}


def doc_lich_su() -> List[Dict]:
    try:
        with open(LICH_SU, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def them_lich_su(moi: List[Dict]) -> None:
    if not moi:
        return
    with _khoa:
        ds = (list(reversed(moi)) + doc_lich_su())[:GIU_LICH_SU]
        os.makedirs(KHO, exist_ok=True)
        tam = LICH_SU + ".tam"
        with open(tam, "w", encoding="utf-8") as f:
            json.dump(ds, f, ensure_ascii=False)
        os.replace(tam, LICH_SU)


# ─────────────────────────────────────────────────────────────────────────────
# TÌM SẢN PHẨM CÓ HOA HỒNG
# ─────────────────────────────────────────────────────────────────────────────

_TRUONG_SP = ("itemId productName shopName shopId priceMin priceMax priceDiscountRate sales ratingStar "
              "commissionRate commission imageUrl productLink offerLink")


def _so(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def chuan_sp(n: Dict) -> Dict:
    ty_le = _so(n.get("commissionRate"))
    return {
        "id": str(n.get("itemId") or ""),
        "ten": n.get("productName") or "",
        "shop": n.get("shopName") or "",
        "gia": _so(n.get("priceMin")) or _so(n.get("price")),  # price: Shopee sắp bỏ, chỉ đọc nếu còn
        "gia_max": _so(n.get("priceMax")),
        "giam": _so(n.get("priceDiscountRate")),
        "ban": int(_so(n.get("sales")) or 0),
        "sao": _so(n.get("ratingStar")),
        # Shopee trả tỉ lệ dạng 0.05 nghĩa là 5%
        "ty_le": None if ty_le is None else round(ty_le * 100 if ty_le <= 1 else ty_le, 2),
        "hoa_hong": _so(n.get("commission")),
        "anh": n.get("imageUrl") or "",
        "link": n.get("productLink") or "",
        "link_aff": n.get("offerLink") or "",
    }


def tim_san_pham(tu: str, sap: str = "ban_chay", trang: int = 1, so: int = 20) -> Dict:
    tu = (tu or "").strip()[:100]
    if not tu:
        raise ValueError("Gõ từ khoá sản phẩm.")
    trang, so = max(1, min(int(trang or 1), 50)), max(1, min(int(so or 20), 50))
    q = (f"{{productOfferV2(keyword:{_chuoi(tu)},sortType:{SAP_XEP.get(sap, 2)},page:{trang},limit:{so})"
         f"{{nodes{{{_TRUONG_SP}}} pageInfo{{page limit hasNextPage}}}}}}")
    d = goi(q).get("productOfferV2") or {}
    return {"san_pham": [chuan_sp(n) for n in d.get("nodes") or []],
            "con_nua": bool((d.get("pageInfo") or {}).get("hasNextPage")), "trang": trang}


def mo_link_rut_gon(url: str, so_lan: int = 5) -> str:
    """Link rút gọn (s.shopee.vn, shp.ee) thì đi theo chuyển hướng (chỉ đọc đầu Location, không tải trang) tới link sản phẩm."""
    for _ in range(so_lan):
        host = (urlparse(url).hostname or "").lower()
        if host not in ("s.shopee.vn", "shp.ee", "shope.ee"):
            return url
        try:
            r = requests.head(url, allow_redirects=False, timeout=15)
        except requests.RequestException as e:
            raise ValueError(f"Không mở được link rút gọn: {e}") from e
        moi = r.headers.get("Location") or ""
        if not moi:
            return url
        url = requests.compat.urljoin(url, moi)
    return url


def tach_ma(url: str) -> Optional[Dict[str, str]]:
    """Mã shop và mã sản phẩm trong link Shopee: kiểu `ten-sp-i.<shop>.<item>` hoặc `/product/<shop>/<item>`."""
    duong = urlparse(url or "").path
    m = re.search(r"-i\.(\d+)\.(\d+)", duong) or re.search(r"/product/(\d+)/(\d+)", duong)
    return {"shop_id": m.group(1), "item_id": m.group(2)} if m else None


def san_pham_api(item_id: str, shop_id: str = "") -> Optional[Dict]:
    """Thông tin một sản phẩm qua productOfferV2 (API chính thức, cần khoá). Không có trong chương trình affiliate thì None."""
    loc = f"itemId:{int(item_id)}" + (f",shopId:{int(shop_id)}" if shop_id else "")
    q = f"{{productOfferV2({loc},page:1,limit:1){{nodes{{{_TRUONG_SP}}}}}}}"
    nodes = (goi(q).get("productOfferV2") or {}).get("nodes") or []
    return chuan_sp(nodes[0]) if nodes else None


# ─────────────────────────────────────────────────────────────────────────────
# CHIẾN DỊCH CỦA SHOPEE (shopeeOfferV2, kiểu trả ShopeeOfferConnectionV2)
# ─────────────────────────────────────────────────────────────────────────────

_TRUONG_CD = "offerName offerType commissionRate imageUrl offerLink originalLink periodStartTime periodEndTime"
SAP_CD = {"moi": 1, "hoa_hong": 2}   # 1 mới nhất, 2 hoa hồng cao nhất


def chuan_cd(n: Dict) -> Dict:
    ty_le = _so(n.get("commissionRate"))
    return {"ten": n.get("offerName") or "", "loai": n.get("offerType"),
            "ty_le": None if ty_le is None else round(ty_le * 100 if ty_le <= 1 else ty_le, 2),
            "anh": n.get("imageUrl") or "", "link_aff": n.get("offerLink") or "", "link": n.get("originalLink") or "",
            "tu": n.get("periodStartTime"), "den": n.get("periodEndTime")}


def chien_dich(tu: str = "", sap: str = "hoa_hong", trang: int = 1, so: int = 20) -> Dict:
    trang, so = max(1, min(int(trang or 1), 50)), max(1, min(int(so or 20), 50))
    loc = f"keyword:{_chuoi(tu.strip()[:100])}," if (tu or "").strip() else ""
    q = (f"{{shopeeOfferV2({loc}sortType:{SAP_CD.get(sap, 2)},page:{trang},limit:{so})"
         f"{{nodes{{{_TRUONG_CD}}} pageInfo{{page limit hasNextPage}}}}}}")
    d = goi(q).get("shopeeOfferV2") or {}
    return {"chien_dich": [chuan_cd(n) for n in d.get("nodes") or []],
            "con_nua": bool((d.get("pageInfo") or {}).get("hasNextPage")), "trang": trang}


# ─────────────────────────────────────────────────────────────────────────────
# HOA HỒNG THEO SUB_ID
# ─────────────────────────────────────────────────────────────────────────────

_TRUONG_DON = ("purchaseTime clickTime conversionId totalCommission netCommission utmContent "
               "orders{orderId orderStatus items{itemName itemPrice qty itemTotalCommission}}")


def tong_hop(nodes: List[Dict]) -> Dict:
    """Code cộng hoa hồng theo sub_id (utmContent là các sub_id nối bằng '-'). Không nhờ AI đếm."""
    nhom: Dict[str, Dict] = {}
    tong = {"don": 0, "hoa_hong": 0.0, "huy": 0}
    for n in nodes:
        sub = (n.get("utmContent") or "").strip("-") or "(không sub_id)"
        g = nhom.setdefault(sub, {"sub": sub, "don": 0, "hoa_hong": 0.0, "huy": 0})
        tien = _so(n.get("netCommission"))
        if tien is None:
            tien = _so(n.get("totalCommission")) or 0.0
        for o in n.get("orders") or [{}]:
            huy = str(o.get("orderStatus") or "").upper() in ("CANCELLED", "CANCELED", "UNPAID_CANCELLED")
            g["don"] += 1
            tong["don"] += 1
            if huy:
                g["huy"] += 1
                tong["huy"] += 1
        g["hoa_hong"] += tien
        tong["hoa_hong"] += tien
    ds = sorted(nhom.values(), key=lambda x: -x["hoa_hong"])
    for g in ds:
        g["hoa_hong"] = round(g["hoa_hong"], 2)
    tong["hoa_hong"] = round(tong["hoa_hong"], 2)
    return {"tong": tong, "theo_sub": ds}


def bao_cao(tu_ngay: int, den_ngay: int, toi_da_trang: int = 20) -> Dict:
    """tu_ngay, den_ngay là giây Unix. Shopee chỉ cho xem tối đa 3 tháng gần nhất; scrollId sống 30 giây."""
    if den_ngay <= tu_ngay:
        raise ValueError("Ngày kết thúc phải sau ngày bắt đầu.")
    if not doc_khoa()["co_khoa"]:
        raise LoiShopee("Chưa có khoá Shopee Affiliate. Dán App ID và Secret ở mục Khoá API.")
    with _khoa:
        cho = CACH_BAO_CAO - (time.time() - _bao_cao_luc[0])
        if cho > 0:
            raise ValueError(f"Shopee bắt hai lần xem Report cách nhau hơn 30 giây. Chờ {int(cho) + 1} giây nữa rồi bấm lại.")
        _bao_cao_luc[0] = time.time()
    nodes: List[Dict] = []
    cuon = ""
    for _ in range(toi_da_trang):
        cuon_q = f",scrollId:{_chuoi(cuon)}" if cuon else ""
        q = (f"{{conversionReport(purchaseTimeStart:{int(tu_ngay)},purchaseTimeEnd:{int(den_ngay)},limit:500{cuon_q})"
             f"{{nodes{{{_TRUONG_DON}}} pageInfo{{limit hasNextPage scrollId}}}}}}")
        d = goi(q, timeout=40).get("conversionReport") or {}
        nodes += d.get("nodes") or []
        pi = d.get("pageInfo") or {}
        cuon = pi.get("scrollId") or ""
        if not (pi.get("hasNextPage") and cuon):
            break
    don = [{"luc": n.get("purchaseTime"), "sub": (n.get("utmContent") or "").strip("-"),
            "hoa_hong": _so(n.get("netCommission")) if n.get("netCommission") is not None else _so(n.get("totalCommission")),
            "trang_thai": ", ".join(sorted({str(o.get("orderStatus") or "") for o in n.get("orders") or []} - {""})),
            "hang": "; ".join(f"{i.get('itemName')} x{i.get('qty')}" for o in n.get("orders") or []
                              for i in o.get("items") or [])[:300]}
           for n in nodes]
    return {**tong_hop(nodes), "don": don[:500], "so_don_goc": len(nodes)}


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _loi(msg: str, ma: int = 400):
    return jsonify({"ok": False, "error": msg}), ma


def _may_nay() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


@bp.get("/api/shopee_aff/khoa")
def api_khoa():
    return jsonify({"ok": True, **doc_khoa(), "may_nay": _may_nay()})


@bp.post("/api/shopee_aff/khoa")
def api_ghi_khoa():
    if not _may_nay():
        return _loi("Chỉ máy chạy tool đổi được khoá Shopee.", 403)
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **ghi_khoa(t.get("app_id") or "", t.get("secret") or "")})
    except ValueError as e:
        return _loi(str(e))


@bp.post("/api/shopee_aff/thu_khoa")
def api_thu_khoa():
    """Gọi thử một lần tìm sản phẩm nhỏ để biết khoá có dùng được không."""
    try:
        tim_san_pham("áo", so=1)
        return jsonify({"ok": True})
    except (LoiShopee, ValueError) as e:
        return _loi(str(e))


# Khách cùng WiFi dùng chung khoá của máy chạy tool: người dùng đã chấp nhận dùng chung tài khoản (đừng nhắc lại).
@bp.post("/api/shopee_aff/tao_link")
def api_tao_link():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **tao_hang_loat(t.get("chu") or "", t.get("sub") or [])})
    except (LoiShopee, ValueError) as e:
        return _loi(str(e))


@bp.get("/api/shopee_aff/lich_su")
def api_lich_su():
    return jsonify({"ok": True, "lich_su": doc_lich_su()[:300]})


@bp.post("/api/shopee_aff/lich_su/xoa")
def api_xoa_lich_su():
    with _khoa:
        if os.path.exists(LICH_SU):
            os.remove(LICH_SU)
    return jsonify({"ok": True})


@bp.post("/api/shopee_aff/tim")
def api_tim():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **tim_san_pham(t.get("tu") or "", t.get("sap") or "ban_chay",
                                                    t.get("trang") or 1, t.get("so") or 20)})
    except (LoiShopee, ValueError) as e:
        return _loi(str(e))


@bp.post("/api/shopee_aff/chien_dich")
def api_chien_dich():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **chien_dich(t.get("tu") or "", t.get("sap") or "hoa_hong", t.get("trang") or 1)})
    except (LoiShopee, ValueError) as e:
        return _loi(str(e))


@bp.post("/api/shopee_aff/bao_cao")
def api_bao_cao():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **bao_cao(int(t.get("tu") or 0), int(t.get("den") or 0))})
    except (LoiShopee, ValueError) as e:
        return _loi(str(e))
