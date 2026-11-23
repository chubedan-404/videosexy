"""
SỐ LIỆU SHOPEE TỪ TRANG AFFILIATE (15/11/2026): video nào kéo được click, món nào ra đơn, bao nhiêu hoa hồng.

Chưa có Open API (mã 10035), nên tiện ích Chrome `tien_ich_shopee/` (người dùng đã chọn cách này, chỉ trang affiliate) mỗi sáng
mở hai trang Báo cáo click (/report/click_report) và Báo cáo chuyển đổi (/report/conversion_report), đọc chữ trong bảng như
người xem (lật trang bằng nút của trang, nghỉ giữa các trang), rồi gửi về đây. Shopee cập nhật số lúc 9:00 sáng mỗi ngày.

  1. GET  /api/shopee_so_lieu/viec  -> tiện ích hỏi mỗi phút: tới giờ đọc (từ 9:20, mỗi ngày một lần) hoặc người dùng bấm "Đọc ngay".
  2. POST /api/shopee_so_lieu/nhan  -> tiện ích gửi các dòng đọc được (mỗi dòng là {tên cột: chữ}).
  3. GET  /api/shopee_so_lieu       -> trang xem: click và đơn gom theo từng video/sản phẩm, theo ngày, theo nguồn.

Click gộp theo "Click id" nên đọc nhiều lần không đếm trùng. Ghép click với video bằng sub_id: link aff của tool luôn mang
mã sản phẩm (item_id) trong sub_id (Autopilot: autopilot, ngày, mã; từ khoá: tukhoa, ngày, mã), nên dò mã đó trong cột Sub_id.
Bảng chuyển đổi CHƯA thấy dòng thật nào (15/11/2026 tài khoản 0 đơn): cách đọc cột hoa hồng là đoán theo tên cột, có đơn đầu tiên
thì kiểm lại `_tien` và `_ma_don`.
"""
import hashlib
import os
import re
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional

from flask import Blueprint, jsonify, request

import xuong_chung as pc

bp = Blueprint("shopee_so_lieu", __name__)
TEP = os.path.join(pc.TAI_VE, "shopee_auto", "so_lieu.json")
_khoa = threading.Lock()
GIO_DOC = (9, 20)            # Shopee cập nhật 9:00; đọc sau đó một chút
QUA_HAN_GIAY = 30 * 60       # một lần đọc treo quá lâu thì cho đọc lại
TOI_DA_DONG = 5000           # mỗi lần gửi
GIU_CLICK = 20000            # giữ bấy nhiêu click mới nhất


def doc() -> Dict:
    d = pc.doc_json(TEP) or {}
    d.setdefault("click", {})
    d.setdefault("don", {})
    d.setdefault("lich_su", [])
    for k in ("ngay_doc", "yeu_cau", "dang", "loi"):
        d.setdefault(k, "" if k in ("ngay_doc", "loi") else (False if k == "yeu_cau" else None))
    return d


def _ghi(d: Dict) -> None:
    os.makedirs(os.path.dirname(TEP), exist_ok=True)
    pc.ghi_json(TEP, d)


# ─────────────────────────────────────────────────────────────────────────────
# TIỆN ÍCH HỎI VÀ GỬI
# ─────────────────────────────────────────────────────────────────────────────

def viec(bay: Optional[float] = None) -> Dict:
    """Có cần đọc báo cáo bây giờ không. Đánh dấu `dang` để hai lần hỏi liền không đọc hai lần."""
    bay = bay or time.time()
    with _khoa:
        d = doc()
        dang = d.get("dang")
        if dang and bay - float(dang) < QUA_HAN_GIAY:
            return {"lam": False}
        t = time.localtime(bay)
        hom_nay = time.strftime("%Y-%m-%d", t)
        toi_gio = (t.tm_hour, t.tm_min) >= GIO_DOC and d.get("ngay_doc") != hom_nay
        if not (d.get("yeu_cau") or toi_gio):
            return {"lam": False}
        d["dang"] = bay
        _ghi(d)
    return {"lam": True, "toi_da_trang": 30}


def _gio(chu: str) -> float:
    """'2026-11-14 23:40:12', '14/11/2026 23:40', '20:47:39 14-11-2026' (trang aff ghi giờ TRƯỚC ngày) -> giây.
    Tìm phần ngày và phần giờ riêng nên thứ tự nào cũng đọc được. Không đọc được thì 0."""
    chu = (chu or "").strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", chu)
    if m:
        y, mo, dd = m.groups()
    else:
        m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", chu)
        if not m:
            return 0.0
        dd, mo, y = m.groups()
    con = chu[:m.start()] + " " + chu[m.end():]
    g = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", con)
    h, mi, sg = g.groups() if g else ("0", "0", "0")
    try:
        return time.mktime((int(y), int(mo), int(dd), int(h), int(mi), int(sg or 0), 0, 0, -1))
    except (OverflowError, ValueError):
        return 0.0


def _cot(dong: Dict, *ten: str) -> str:
    """Lấy chữ của cột đầu tiên có tên chứa một trong các chữ cho trước (không phân biệt hoa thường)."""
    for k, v in dong.items():
        kk = k.lower()
        if any(t.lower() in kk for t in ten):
            return str(v or "").strip()
    return ""


def _tien(chu: str) -> float:
    """'₫12.345', '12.345 ₫', '1,234.5' -> số. Lấy số đầu tiên trong ô."""
    m = re.search(r"\d[\d.,]*", chu or "")
    if not m:
        return 0.0
    s = m.group(0)
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s) or re.fullmatch(r"\d{1,3}(\.\d{3})+,\d+", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _ma_don(dong: Dict) -> str:
    chu = " ".join(str(v) for v in dong.values())
    m = re.search(r"\b(\d{12,20})\b", chu) or re.search(r"\b([0-9A-Z]{12,20})\b", chu)
    return m.group(1) if m else ""


def nhan(click: List[Dict], don: List[Dict], tong_click: Optional[int] = None, tong_don: Optional[int] = None, loi: str = "") -> Dict:
    """Gộp các dòng tiện ích vừa đọc. Trả số dòng mới."""
    with _khoa:
        d = doc()
        moi_c = moi_d = 0
        for r in (click or [])[:TOI_DA_DONG]:
            if not isinstance(r, dict):
                continue
            ma = _cot(r, "click id")
            if not ma:
                continue
            if ma not in d["click"]:
                moi_c += 1
            d["click"][ma] = {"luc": _gio(_cot(r, "thời gian")), "gio": _cot(r, "thời gian")[:30], "khu_vuc": _cot(r, "khu vực")[:60],
                              "sub": _cot(r, "sub_id", "sub id")[:260], "nguon": _cot(r, "giới thiệu")[:120]}
        for r in (don or [])[:TOI_DA_DONG]:
            if not isinstance(r, dict):
                continue
            chu = " | ".join(f"{k}: {v}" for k, v in r.items())[:1500]
            ma = _ma_don(r) or hashlib.sha1(chu.encode("utf-8")).hexdigest()[:16]
            khoa = ma + "|" + hashlib.sha1(_cot(r, "sản phẩm").encode("utf-8")).hexdigest()[:8]
            if khoa not in d["don"]:
                moi_d += 1
            d["don"][khoa] = {"ma": ma, "chu": chu, "luc": _gio(_cot(r, "thời gian", "chi tiết")), "hoa_hong": _tien(_cot(r, "hoa hồng đơn")),
                              "gia_tri": _tien(_cot(r, "giá trị")), "san_pham": _cot(r, "sản phẩm")[:200]}
        if len(d["click"]) > GIU_CLICK:
            giu = sorted(d["click"].items(), key=lambda x: -x[1].get("luc", 0))[:GIU_CLICK]
            d["click"] = dict(giu)
        bay = time.time()
        d["lich_su"] = (d["lich_su"] + [{"luc": bay, "click_moi": moi_c, "don_moi": moi_d, "tong_click": tong_click,
                                         "tong_don": tong_don, "loi": (loi or "")[:300]}])[-60:]
        d.update(ngay_doc=time.strftime("%Y-%m-%d") if not loi else d.get("ngay_doc", ""), yeu_cau=False, dang=None, loi=(loi or "")[:300])
        _ghi(d)
    return {"click_moi": moi_c, "don_moi": moi_d}


def yeu_cau_doc() -> Dict:
    with _khoa:
        d = doc()
        d["yeu_cau"] = True
        d["dang"] = None
        _ghi(d)
    return {"yeu_cau": True}


# ─────────────────────────────────────────────────────────────────────────────
# GOM SỐ
# ─────────────────────────────────────────────────────────────────────────────

def _cac_dong() -> List[Dict]:
    """Mọi dòng Shopee Autopilot có mã sản phẩm, mới trước. Một sản phẩm làm nhiều lần thì lấy dòng mới nhất làm đại diện."""
    try:
        import shopee_auto as sau
    except Exception:  # noqa: BLE001
        return []
    ra = []
    if not os.path.isdir(sau.KHO):
        return ra
    for ten in sorted(os.listdir(sau.KHO), reverse=True):
        if not (ten.startswith("sa_") and ten.endswith(".json")):
            continue
        d = pc.doc_json(os.path.join(sau.KHO, ten)) or {}
        for m in d.get("muc") or []:
            if m.get("item_id"):
                ra.append({**m, "luot": d.get("id"), "tao_luc": d.get("tao_luc") or 0})
    return ra


def _nguon(sub: str) -> str:
    s = (sub or "").lower()
    return ("Autopilot" if "autopilot" in s else "Keyword schedule" if "tukhoa" in s else "YouTube book links" if "youtube" in s
            else "Other links" if s else "No sub_id")


def _kenh(chu: str) -> str:
    """Cột "Người giới thiệu": trang người bấm đến từ đâu, gom về tên nền tảng."""
    c = (chu or "").lower()
    for k, ten in (("facebook", "Facebook"), ("fb.", "Facebook"), ("youtube", "YouTube"), ("youtu.be", "YouTube"),
                   ("tiktok", "TikTok"), ("instagram", "Instagram"), ("threads", "Threads"), ("zalo", "Zalo")):
        if k in c:
            return ten
    return "Unknown" if not c or c in ("-", "--") else chu[:30]


def tong_hop(ngay: int = 30) -> Dict:
    d = doc()
    tu = time.time() - ngay * 86400 if ngay else 0
    dong = _cac_dong()
    theo_ma: Dict[str, Dict] = {}
    for m in dong:
        theo_ma.setdefault(str(m["item_id"]), m)
    ma_dai = sorted(theo_ma, key=len, reverse=True)

    def tim(chu: str) -> str:
        so = set(re.findall(r"\d{6,}", chu or ""))
        return next((ma for ma in ma_dai if ma in so), "")

    sp = defaultdict(lambda: {"click": 0, "don": 0, "hoa_hong": 0.0, "kenh": defaultdict(int)})
    ngay_click = defaultdict(int)
    nguon = defaultdict(int)
    kenh = defaultdict(int)
    tong = 0
    for c in d["click"].values():
        if c.get("luc", 0) and c["luc"] < tu:
            continue
        tong += 1
        ma = tim(c.get("sub", ""))
        k = _kenh(c.get("nguon", ""))
        if ma:
            sp[ma]["click"] += 1
            sp[ma]["kenh"][k] += 1
        if c.get("luc"):
            ngay_click[time.strftime("%Y-%m-%d", time.localtime(c["luc"]))] += 1
        nguon[_nguon(c.get("sub", ""))] += 1
        kenh[k] += 1
    tong_don = 0
    tong_hh = 0.0
    for o in d["don"].values():
        if o.get("luc", 0) and o["luc"] < tu:
            continue
        tong_don += 1
        tong_hh += o.get("hoa_hong") or 0
        ma = tim(o.get("chu", ""))
        if ma:
            sp[ma]["don"] += 1
            sp[ma]["hoa_hong"] += o.get("hoa_hong") or 0
    bang = []
    for ma, so in sp.items():
        m = theo_ma.get(ma, {})
        dt = m.get("dang_them") or {}
        bang.append({"item_id": ma, "ten": m.get("ten") or ma, "click": so["click"], "don": so["don"], "hoa_hong": round(so["hoa_hong"]),
                     "kieu": m.get("dang_aff") or "", "luot": m.get("luot") or "", "video": bool(m.get("video")),
                     "link": (m.get("dang") or {}).get("link") or "", "kenh_dang": ["Facebook"] * bool((m.get("dang") or {}).get("ok"))
                     + [("YouTube" if k.startswith("yt:") else "TikTok") for k, v in dt.items() if (v or {}).get("ok")],
                     "kenh": dict(so["kenh"])})
    # Video đã đăng mà chưa có click nào cũng hiện (0 click cũng là một con số).
    for ma, m in theo_ma.items():
        if ma not in sp and (m.get("dang") or {}).get("ok"):
            bang.append({"item_id": ma, "ten": m.get("ten") or ma, "click": 0, "don": 0, "hoa_hong": 0, "kieu": m.get("dang_aff") or "",
                         "luot": m.get("luot") or "", "video": True, "link": (m.get("dang") or {}).get("link") or "",
                         "kenh_dang": ["Facebook"], "kenh": {}})
    bang.sort(key=lambda x: (-x["hoa_hong"], -x["don"], -x["click"]))
    kieu = defaultdict(lambda: {"video": 0, "click": 0, "don": 0})
    for b in bang:
        if b["kieu"]:
            kieu[b["kieu"]]["video"] += 1
            kieu[b["kieu"]]["click"] += b["click"]
            kieu[b["kieu"]]["don"] += b["don"]
    moc = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(13, -1, -1)]
    cuoi = (d["lich_su"] or [{}])[-1]
    return {
        "ngay": ngay, "tong_click": tong, "tong_don": tong_don, "tong_hoa_hong": round(tong_hh),
        "san_pham": bang[:100], "theo_ngay": [{"ngay": k[8:] + "/" + k[5:7], "click": ngay_click.get(k, 0)} for k in moc],
        "nguon": dict(nguon), "kenh": dict(kenh), "kieu": dict(kieu),
        "lan_doc": cuoi.get("luc"), "loi": d.get("loi") or "", "dang": bool(d.get("dang")), "yeu_cau": bool(d.get("yeu_cau")),
        "da_doc": bool(d["lich_su"]), "tong_click_trang": cuoi.get("tong_click"),
    }


# ── Xem theo kênh (16/11/2026, người dùng: "xem all thì xem đơn hoa hồng, xem từng channel thì show ra từng video") ─────────

def cac_kenh() -> List[Dict]:
    """Fanpage đã có video Shopee + kênh YouTube đã nối (link sách), mỗi kênh kèm số video."""
    import kenh_dang
    ten = {k["id"]: k["ten"] for k in kenh_dang.ds_kenh()}
    dem: Dict[str, int] = defaultdict(int)
    for v in _video_fb():
        dem["fb:" + v["trang"]] += 1
    ra = [{"id": k, "ten": ten.get(k) or k[3:], "loai": "fb", "so": n} for k, n in sorted(dem.items(), key=lambda x: -x[1])]
    try:
        import aff_youtube
        gan = aff_youtube.doc()
        td = aff_youtube.doc_td()
        for k in aff_youtube.cac_kenh():
            n = sum(1 for g in gan.values() if g.get("ok") and g.get("kenh") == k["id"])
            if n or (td.get("bat") and td.get("kenh") == k["id"]):   # chỉ kênh có video gắn link (hoặc đang tự động gắn)
                ra.append({"id": "yt:" + k["id"], "ten": k["ten"], "loai": "yt", "so": n})
    except Exception:  # noqa: BLE001
        pass
    return ra


def _video_fb() -> List[Dict]:
    """Mọi video Shopee đã lên Facebook: trang, khoá số (so_reel.json), dòng Autopilot."""
    try:
        import shopee_auto as sau
    except Exception:  # noqa: BLE001
        return []
    ra = []
    if not os.path.isdir(sau.KHO):
        return ra
    for ten in sorted(os.listdir(sau.KHO)):
        if not (ten.startswith("sa_") and ten.endswith(".json")):
            continue
        d = pc.doc_json(os.path.join(sau.KHO, ten)) or {}
        lu = d.get("id")
        for m in d.get("muc") or []:
            dg = m.get("dang") or {}
            if dg.get("ok"):
                ra.append({"trang": (dg.get("kenh") or "fb:" + str((d.get("cai_dat") or {}).get("trang") or "")).partition(":")[2],
                           "khoa": f"{lu}:{m.get('thu')}", "kq": dg, "m": m, "luot": lu, "loai": "Reel"})
            for kenh, kq in (m.get("dang_them") or {}).items():
                if kenh.startswith(("fb:", "fbv:")) and (kq or {}).get("ok"):
                    ra.append({"trang": kenh.partition(":")[2], "khoa": f"{lu}:{m.get('thu')}:{kenh}", "kq": kq, "m": m, "luot": lu,
                               "loai": "Video thường" if kenh.startswith("fbv:") else "Reel"})
    return ra


def theo_trang(trang: str) -> Dict:
    """Từng video của một Fanpage: xem, reach (Facebook), tình trạng phân phối, click/đơn/hoa hồng của sản phẩm (link aff dùng chung
    mọi trang nên click là của sản phẩm, không tách được theo trang: ghi `chung` khi sản phẩm lên nhiều trang)."""
    import hoc_aff
    so = hoc_aff._doc()
    try:
        import suc_khoe_aff
        tt = suc_khoe_aff.tinh()["video"]
    except Exception:  # noqa: BLE001
        tt = {}
    sp = {x["item_id"]: x for x in tong_hop(0)["san_pham"]}
    ds = _video_fb()
    so_trang: Dict[str, set] = defaultdict(set)
    for v in ds:
        so_trang[str(v["m"].get("item_id") or v["khoa"])].add(v["trang"])
    hang = []
    for v in ds:
        if v["trang"] != str(trang):
            continue
        m, s_ = v["m"], so.get(v["khoa"]) or {}
        ma = str(m.get("item_id") or "")
        x = sp.get(ma) or {}
        hang.append({"ten": m.get("ten") or "", "link": v["kq"].get("link") or "", "luc": v["kq"].get("luc"), "loai": v["loai"],
                     "xem": None if s_.get("loi") or not s_.get("luc") else int(s_.get("xem") or 0),
                     "reach": s_.get("reach"), "tt": tt.get(v["khoa"]) or "", "click": x.get("click", 0), "don": x.get("don", 0),
                     "hoa_hong": x.get("hoa_hong", 0), "chung": len(so_trang[ma or v["khoa"]]) > 1, "luot": v["luot"], "thu": m.get("thu")})
    hang.sort(key=lambda h: -(h["luc"] or 0))
    return {"hang": hang, "tong": {"video": len(hang), "xem": sum(h["xem"] or 0 for h in hang),
                                   "reach": sum(h["reach"] or 0 for h in hang)}}


def click_7_ngay() -> List[int]:
    """Cho màn Overview: số click mỗi ngày, 7 ngày gần nhất (cũ -> mới)."""
    d = doc()
    dem = defaultdict(int)
    for c in d["click"].values():
        if c.get("luc"):
            dem[time.strftime("%Y-%m-%d", time.localtime(c["luc"]))] += 1
    return [dem.get(time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)), 0) for i in range(6, -1, -1)]


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _chi_may_nay() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def _loi(e, ma: int = 400):
    return jsonify({"ok": False, "error": str(e)[:300]}), ma


@bp.route("/api/shopee_so_lieu", methods=["GET"])
def api_xem():
    try:
        ngay = int(request.args.get("ngay") or 30)
    except ValueError:
        ngay = 30
    return jsonify({"ok": True, **tong_hop(ngay if ngay in (1, 7, 14, 30, 90, 0) else 30)})


@bp.route("/api/shopee_so_lieu/viec", methods=["GET"])
def api_viec():
    if not _chi_may_nay():
        return _loi("Không cho phép.", 403)
    return jsonify({"ok": True, **viec()})


@bp.route("/api/shopee_so_lieu/nhan", methods=["POST"])
def api_nhan():
    if not _chi_may_nay():
        return _loi("Không cho phép.", 403)
    t = request.get_json(silent=True) or {}
    so = lambda x: int(x) if isinstance(x, (int, float)) else None  # noqa: E731
    return jsonify({"ok": True, **nhan(t.get("click") or [], t.get("don") or [], so(t.get("tong_click")), so(t.get("tong_don")),
                                        str(t.get("loi") or ""))})


@bp.route("/api/shopee_so_lieu/doc_ngay", methods=["POST"])
def api_doc_ngay():
    if not _chi_may_nay():
        return _loi("Chỉ máy chạy tool bấm được.", 403)
    return jsonify({"ok": True, **yeu_cau_doc()})


@bp.route("/api/shopee_so_lieu/kenh", methods=["GET"])
def api_kenh():
    try:
        return jsonify({"ok": True, "kenh": cac_kenh()})
    except Exception as e:  # noqa: BLE001
        return _loi(e)


@bp.route("/api/shopee_so_lieu/trang", methods=["GET"])
def api_trang():
    try:
        return jsonify({"ok": True, **theo_trang(str(request.args.get("id") or ""))})
    except Exception as e:  # noqa: BLE001
        return _loi(e)
