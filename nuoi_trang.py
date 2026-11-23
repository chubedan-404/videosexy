"""Nuôi trang (16/11/2026).

30 video Shopee lên 4 Fanpage đều 0 view: cùng một clip lên cả 4 trang trong vài phút, comment link ngay, trang mới đăng dồn
dập. Lượt bật "Nuôi trang" (`cai_dat.nuoi`) thì video xong KHÔNG đăng ngay; luồng nền ở đây tự xếp lịch:
- Mỗi Fanpage một bản riêng: trang đầu nhận bản gốc, trang sau nhận bản dựng lại từ đúng clip đó (câu móc mới, khung phóng
  và lệch khác, màu khác: `review_sp.khung_bien_the`). Không quay thêm, không đọc giọng lại.
- Mỗi trang tối đa TOI_DA_NGAY video một ngày, cách nhau ít nhất CACH_TRANG, chỉ trong KHUNG giờ; hai lần đăng bất kỳ của
  Nuôi trang cách nhau ít nhất CACH_CHEO; cùng một video lên hai trang cách nhau ít nhất CACH_VIDEO.
- KHOI_DONG video đầu của mỗi trang không comment link. Sau đó chờ CHO_COMMENT, video có từ XEM_COMMENT view mới comment;
  quá BO_COMMENT mà vẫn ít view thì bỏ, không comment.
- Trang `tam_dung` của suc_khoe_aff (3 video gần nhất đều 0 view) thì ngừng đăng.
Danh sách Fanpage nuôi và nhật ký ở downloads/shopee_auto/nuoi_trang.json. Bài đăng ghi vào đúng chỗ cũ (`dang` của Fanpage
chính, `dang_them["fb:<id>"]` của trang khác) kèm `nuoi`, nên bảng dòng, báo động và tự học đọc được như mọi Reel.
"""
from __future__ import annotations

import copy
import os
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

import xuong_chung as pc

bp = Blueprint("nuoi_trang", __name__)

TEP = os.path.join(pc.TAI_VE, "shopee_auto", "nuoi_trang.json")
GIO = 3600
KHUNG = ((11, 13), (19, 22))     # giờ được đăng (giờ máy), [đầu, cuối)
TOI_DA_NGAY = 2
CACH_TRANG = 4 * GIO
CACH_CHEO = 40 * 60
CACH_VIDEO = 2 * GIO
KHOI_DONG = 5
CHO_COMMENT = 2 * GIO
XEM_COMMENT = 20
BO_COMMENT = 48 * GIO
LAN_HONG = 3                      # đăng hỏng bấy nhiêu lần lên một trang thì thôi trang đó cho video này
NGHI_HONG = 6 * GIO


def doc() -> Dict:
    d = pc.doc_json(TEP) or {}
    d.setdefault("trang", [])
    d.setdefault("nhat_ky", [])
    return d


def _ghi(d: Dict) -> None:
    os.makedirs(os.path.dirname(TEP), exist_ok=True)
    pc.ghi_json(TEP, d)


def ghi_nhat_ky(chu: str, bay: Optional[float] = None) -> None:
    d = doc()
    d["nhat_ky"] = (d["nhat_ky"] + [{"luc": bay or time.time(), "chu": chu[:300]}])[-60:]
    _ghi(d)


# ── Đọc các lượt ─────────────────────────────────────────────────────────────

def _cac_luot() -> List[Dict]:
    import shopee_auto as sau
    ra = []
    if not os.path.isdir(sau.KHO):
        return ra
    for ten in sorted(os.listdir(sau.KHO)):
        if ten.startswith("sa_") and ten.endswith(".json"):
            d = pc.doc_json(os.path.join(sau.KHO, ten))
            if d and d.get("id"):
                ra.append(d)
    return ra


def bai_cua_dong(d: Dict, m: Dict) -> List[Tuple[str, Dict, bool]]:
    """(mã trang, kết quả đăng, là Fanpage chính) của mọi lần đăng Facebook của một dòng (cả hỏng). Video thường `fbv:` tính
    chung trang."""
    ra = []
    dg = m.get("dang") or {}
    if dg.get("ok") or dg.get("loi"):
        ra.append(((dg.get("kenh") or "fb:" + str((d.get("cai_dat") or {}).get("trang") or "")).partition(":")[2], dg, True))
    for kenh, kq in (m.get("dang_them") or {}).items():
        if kenh.startswith(("fb:", "fbv:")) and kq:
            ra.append((kenh.partition(":")[2], kq, False))
    return ra


def _dau_ngay(bay: float) -> float:
    t = time.localtime(bay)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def trong_khung(bay: float) -> bool:
    h = time.localtime(bay).tm_hour
    return any(a <= h < b for a, b in KHUNG)


def gio_khung_toi(bay: float) -> float:
    """Giờ sớm nhất từ `bay` nằm trong khung được đăng."""
    if trong_khung(bay):
        return bay
    dau = _dau_ngay(bay)
    for ngay in (0, 1):
        for a, _ in KHUNG:
            t = time.mktime(time.localtime(dau + ngay * 86400 + 12 * GIO)[:3] + (a, 0, 0, 0, 0, -1))
            if t > bay:
                return t
    return bay + 86400


def tinh_trang(cac_luot: List[Dict], bay: float) -> Dict[str, Dict]:
    """Mỗi trang: giờ các bài đã lên, số bài Nuôi trang đã lên, giờ bài Nuôi trang cuối (mọi trang)."""
    ra: Dict[str, Dict] = {}
    for d in cac_luot:
        for m in d.get("muc") or []:
            for p, kq, _ in bai_cua_dong(d, m):
                if not kq.get("ok"):
                    continue
                t = ra.setdefault(p, {"luc": [], "nuoi": 0})
                t["luc"].append(float(kq.get("luc") or d.get("tao_luc") or 0))
                if kq.get("nuoi"):
                    t["nuoi"] += 1
    return ra


def _ung_vien(cac_luot: List[Dict]) -> List[Tuple[Dict, Dict]]:
    """Dòng của lượt Nuôi trang có video trên đĩa, cũ trước."""
    ra = []
    for d in sorted(cac_luot, key=lambda x: x.get("tao_luc") or 0):
        if not (d.get("cai_dat") or {}).get("nuoi"):
            continue
        for m in sorted(d.get("muc") or [], key=lambda x: x.get("thu") or 0):
            if m.get("video") and m.get("trang_thai") in ("co_video", "da_dang") and os.path.isfile(os.path.join(pc.TAI_VE, m["video"])):
                ra.append((d, m))
    return ra


def ke_hoach(cac_luot: List[Dict], trang: List[str], tam_dung: set, bay: float) -> Optional[Dict]:
    """Việc đăng kế tiếp (một bài), None nếu chưa tới lúc. Hàm thuần để bài kiểm gọi."""
    if not trang or not trong_khung(bay):
        return None
    tt = tinh_trang(cac_luot, bay)
    cuoi_nuoi = 0.0
    for d in cac_luot:
        for m in d.get("muc") or []:
            for _, kq, _ in bai_cua_dong(d, m):
                if kq.get("nuoi"):
                    cuoi_nuoi = max(cuoi_nuoi, float(kq.get("luc") or 0))
    if bay - cuoi_nuoi < CACH_CHEO:
        return None
    dau = _dau_ngay(bay)

    def thu_tu(p):
        luc = (tt.get(p) or {}).get("luc") or []
        return (sum(1 for x in luc if x >= dau), max(luc) if luc else 0)

    uv = _ung_vien(cac_luot)
    for p in sorted(trang, key=thu_tu):
        if p in tam_dung:
            continue
        luc = (tt.get(p) or {}).get("luc") or []
        if sum(1 for x in luc if x >= dau) >= TOI_DA_NGAY or (luc and bay - max(luc) < CACH_TRANG):
            continue
        for d, m in uv:
            bai = bai_cua_dong(d, m)
            if any(q == p and kq.get("ok") for q, kq, _ in bai):
                continue
            hong = [kq for q, kq, _ in bai if q == p and not kq.get("ok")]
            if hong and (int((hong[-1].get("nuoi") or {}).get("lan") or 1) >= LAN_HONG or bay - float(hong[-1].get("luc") or 0) < NGHI_HONG):
                continue
            len_ = [float(kq.get("luc") or 0) for _, kq, _ in bai if kq.get("ok")]
            if len_ and bay - max(len_) < CACH_VIDEO:
                continue
            if len_ and not m.get("pid_aff"):
                continue   # video không do tool làm: không dựng được bản khác, chỉ một trang nhận bản gốc
            return {"luot": d["id"], "thu": m["thu"], "trang": p, "ban": "bien_the" if len_ else "goc",
                    "khoi_dong": ((tt.get(p) or {}).get("nuoi") or 0) < KHOI_DONG, "k": len(len_)}
    return None


# ── Làm một việc đăng ────────────────────────────────────────────────────────

def lam_ban(ma: str, thu: int, trang: str, k: int, viec: Dict) -> Dict:
    """Dựng bản riêng cho `trang` từ dự án video của dòng: câu móc mới, khung khác (bản thứ k). Lưu ở `m.ban_nuoi[trang]`."""
    import kich_ban_aff as kba
    import review_sp as rv
    import shopee_auto as sau
    m = next(x for x in sau.mo(ma)["muc"] if x["thu"] == thu)
    da = copy.deepcopy(rv.mo(m["pid_aff"]))
    da["bien_the"] = max(1, k)
    moc = da.get("moc") or ""
    if moc:
        try:
            pc.buoc(viec, 8, "AI viết câu mở đầu khác cho trang này")
            moc = rv.viet_cau_moc(da, 1)[0]
        except Exception:  # noqa: BLE001 - không viết được câu mới thì vẫn khác khung, màu
            pass
        da["moc"] = moc
    xong = [c for c in da.get("canh") or [] if c.get("trang_thai") == "xong"]
    if da.get("dang_aff") not in kba.THEO_CANH and not da.get("giu_cuoi") and len(xong) > 1:
        lech = k % len(xong)   # kể không theo trình tự: mở bằng clip khác
        da["canh"] = xong[lech:] + xong[:lech]
    ten = f"nuoi_{ma[3:]}_{thu}_{trang[-6:]}.mp4"
    rv._dung_mot(da, ten, viec, chu="Đang dựng bản riêng cho trang này", pct=25)
    ban = dict(m.get("ban_nuoi") or {})
    ban[trang] = {"file": ten, "moc": moc, "k": k, "luc": time.time()}
    return sau._sua_muc(ma, thu, ban_nuoi=ban)


def thuc_hien(ke: Dict, viec: Dict) -> Dict:
    import kenh_dang
    import shopee_auto as sau
    ma, thu, p = ke["luot"], ke["thu"], ke["trang"]
    d = sau.mo(ma)
    m = next(x for x in d["muc"] if x["thu"] == thu)
    ten_sp = m.get("ten") or "Sản phẩm"
    if ke["ban"] == "goc":
        tep = m["video"]
    else:
        b = (m.get("ban_nuoi") or {}).get(p) or {}
        if not b.get("file") or not os.path.isfile(os.path.join(pc.TAI_VE, b["file"])):
            pc.buoc(viec, 5, f"Dựng bản riêng: {ten_sp}")
            m = lam_ban(ma, thu, p, ke["k"], viec)
            b = m["ban_nuoi"][p]
        tep = b["file"]
    cd = d["cai_dat"]
    td = (m.get("tieu_de") or "").strip()
    tieu, mo_ta = (td.split("\n")[0][:95], td) if td else (ten_sp[:95], cd["chu_thich"].replace("{ten}", ten_sp))
    pc.buoc(viec, 90, f"Đăng Reel: {ten_sp}")
    kq = (kenh_dang.dang(os.path.join(pc.TAI_VE, tep), tieu, mo_ta, ["fb:" + p]) or [{}])[0]
    kq["luc"] = time.time()
    chinh = p == str(cd.get("trang") or "") and not (m.get("dang") or {}).get("ok")
    cu = (m.get("dang") if chinh else (m.get("dang_them") or {}).get("fb:" + p)) or {}
    lan = int((cu.get("nuoi") or {}).get("lan") or 0) + 1 if not cu.get("ok") and cu else 1
    kq["nuoi"] = {"file": tep, "ban": ke["ban"], "cm": "khong" if ke["khoi_dong"] else "cho", "lan": lan}
    if chinh:
        moi = {"dang": kq}
        if kq.get("ok"):
            moi.update(trang_thai="da_dang", loi="")
        else:
            moi["loi"] = "Nuôi trang: đăng Facebook hỏng: " + str(kq.get("loi") or "")[:200]
        m = sau._sua_muc(ma, thu, **moi)
    else:
        them = dict(m.get("dang_them") or {})
        them["fb:" + p] = kq
        m = sau._sua_muc(ma, thu, dang_them=them, **({"trang_thai": "da_dang"} if kq.get("ok") else {}))
    ghi_nhat_ky(f"{'Đã đăng' if kq.get('ok') else 'Đăng hỏng'} {ten_sp[:50]} lên {_ten(p)}"
                f"{' (bản riêng)' if ke['ban'] != 'goc' else ''}{', khởi động: không comment' if ke['khoi_dong'] else ''}"
                f"{'' if kq.get('ok') else ': ' + str(kq.get('loi') or '')[:120]}")
    viec["result"] = {"ok": bool(kq.get("ok"))}
    return m


def _ten(p: str) -> str:
    try:
        import kenh_dang
        return next((k["ten"] for k in kenh_dang.ds_kenh() if k["id"] == "fb:" + p), p)
    except Exception:  # noqa: BLE001
        return p


# ── Comment trễ ──────────────────────────────────────────────────────────────

def can_comment(kq: Dict, xem: Optional[int], bay: float) -> str:
    """'' chưa tới lúc / chưa có số; 'comment'; 'bo' (quá lâu vẫn ít view)."""
    n = kq.get("nuoi") or {}
    if not kq.get("ok") or n.get("cm") != "cho":
        return ""
    tuoi = bay - float(kq.get("luc") or 0)
    if tuoi < CHO_COMMENT:
        return ""
    if xem is not None and xem >= XEM_COMMENT:
        return "comment"
    return "bo" if tuoi > BO_COMMENT else ""


def comment_tre(bay: Optional[float] = None, toi_da: int = 6) -> int:
    import hoc_aff
    import shopee_auto as sau
    bay = bay or time.time()
    lam = 0
    for d in _cac_luot():
        for m in d.get("muc") or []:
            for p, kq, chinh in bai_cua_dong(d, m):
                if lam >= toi_da or (kq.get("nuoi") or {}).get("cm") != "cho" or bay - float(kq.get("luc") or 0) < CHO_COMMENT:
                    continue
                kenh = "" if chinh else next((k for k, v in (m.get("dang_them") or {}).items() if v is kq), "")
                khoa = f"{d['id']}:{m['thu']}" + (f":{kenh}" if kenh else "")
                so = hoc_aff._doc().get(khoa) or {}
                if not so.get("luc") or bay - so["luc"] > 1800:
                    try:
                        hoc_aff.cap_nhat_so(luot=d["id"], cach=1800, toi_da=40)
                    except Exception:  # noqa: BLE001
                        pass
                    so = hoc_aff._doc().get(khoa) or {}
                xem = None if so.get("loi") or not so.get("luc") else int(so.get("xem") or 0)
                viec = can_comment(kq, xem, bay)
                if not viec:
                    continue
                lam += 1
                moi_kq = copy.deepcopy(kq)
                ten_sp = (m.get("ten") or "")[:50]
                if viec == "bo":
                    moi_kq["nuoi"]["cm"] = "bo"
                    ghi_nhat_ky(f"Không comment {ten_sp} trên {_ten(p)}: sau 2 ngày vẫn dưới {XEM_COMMENT} view")
                    cm = None
                else:
                    try:
                        link = sau._lay_link(d["id"], m)
                    except Exception:  # noqa: BLE001
                        link = ""
                    if not link:
                        moi_kq["nuoi"]["cm"] = "loi"
                        cm = None
                    else:
                        chu = sau.dien_comment((d.get("cai_dat") or {}).get("mau_comment") or sau.MAU_COMMENT, m, link, m["thu"])
                        cm = {**sau.binh_luan(p, str(kq.get("ma") or ""), chu), "chu": chu}
                        moi_kq["nuoi"]["cm"] = "xong" if cm.get("ok") else "loi"
                        ghi_nhat_ky(f"{'Đã comment' if cm.get('ok') else 'Comment hỏng'} link dưới {ten_sp} trên {_ten(p)} ({xem} view)")
                if chinh:
                    sau._sua_muc(d["id"], m["thu"], dang=moi_kq, **({"comment": cm} if cm else {}))
                else:
                    if cm:
                        moi_kq["comment"] = cm
                    moi_m = next(x for x in sau.mo(d["id"])["muc"] if x["thu"] == m["thu"])
                    them = dict(moi_m.get("dang_them") or {})
                    them[kenh] = moi_kq
                    sau._sua_muc(d["id"], m["thu"], dang_them=them)
    return lam


# ── Vòng nền ─────────────────────────────────────────────────────────────────

def _dang_ban() -> bool:
    if pc.moi_viec_dang_chay():
        return True
    try:
        import longform_studio as lf
        with lf._JOBS_LOCK:
            return any(j.get("status") == "running" for j in lf._JOBS.values())
    except Exception:  # noqa: BLE001
        return False


def _tam_dung() -> set:
    try:
        import suc_khoe_aff
        return {t["id"][3:] for t in suc_khoe_aff.tinh()["trang"] if t.get("tam_dung")}
    except Exception:  # noqa: BLE001
        return set()


def mot_vong(bay: Optional[float] = None) -> Optional[str]:
    """Một lần xét: comment trễ, rồi nếu rảnh thì bắt đầu một việc đăng. Trả mã việc (nếu có)."""
    import shopee_auto as sau
    bay = bay or time.time()
    try:
        comment_tre(bay)
    except Exception as e:  # noqa: BLE001
        print(f"[nuoi_trang] comment trễ hỏng: {e}")
    trang = doc()["trang"]
    if not trang or _dang_ban():
        return None
    ke = ke_hoach(_cac_luot(), trang, _tam_dung(), bay)
    if not ke:
        return None
    return pc.bat_dau(sau.XUONG, "nuoi_trang", lambda viec: thuc_hien(ke, viec), ke["luot"])


def _vong_lap() -> None:
    time.sleep(90)
    while True:
        try:
            mot_vong()
        except Exception as e:  # noqa: BLE001
            print(f"[nuoi_trang] hỏng: {e}")
        time.sleep(300)


def khoi_dong() -> None:
    if "pytest" not in sys.modules:
        threading.Thread(target=_vong_lap, daemon=True, name="nuoi-trang").start()


# ── Tình hình cho trang ──────────────────────────────────────────────────────

def tinh_hinh(bay: Optional[float] = None) -> Dict:
    bay = bay or time.time()
    cfg = doc()
    cl = _cac_luot()
    tt = tinh_trang(cl, bay)
    tam = _tam_dung()
    dau = _dau_ngay(bay)
    ds = []
    try:
        import kenh_dang
        cac = [k for k in kenh_dang.ds_kenh() if k["nen"] == "fb"]
    except Exception:  # noqa: BLE001
        cac = []
    uv = _ung_vien(cl)
    for k in cac:
        p = k["id"][3:]
        luc = (tt.get(p) or {}).get("luc") or []
        hom = sum(1 for x in luc if x >= dau)
        cuoi = max(luc) if luc else 0
        som = max(bay, cuoi + CACH_TRANG if cuoi else bay)
        if hom >= TOI_DA_NGAY:
            som = max(som, dau + 86400)
        n = (tt.get(p) or {}).get("nuoi") or 0
        cho = sum(1 for d, m in uv if not any(q == p and kq.get("ok") for q, kq, _ in bai_cua_dong(d, m))
                  and (m.get("pid_aff") or not any(kq.get("ok") for _, kq, _ in bai_cua_dong(d, m))))
        ds.append({"id": p, "ten": k["ten"], "chon": p in cfg["trang"], "tam_dung": p in tam, "hom_nay": hom, "cuoi": cuoi or None,
                   "nuoi": n, "khoi_dong": min(n, KHOI_DONG), "cho": cho, "som_nhat": gio_khung_toi(som)})
    return {"trang": ds, "nhat_ky": list(reversed(cfg["nhat_ky"]))[:15], "luat": {
        "khung": [f"{a}h–{b}h" for a, b in KHUNG], "toi_da_ngay": TOI_DA_NGAY, "cach_gio": CACH_TRANG // GIO,
        "khoi_dong": KHOI_DONG, "cho_comment_gio": CHO_COMMENT // GIO, "xem_comment": XEM_COMMENT}}


@bp.route("/api/nuoi_trang", methods=["GET"])
def api_xem():
    return jsonify({"ok": True, **tinh_hinh()})


@bp.route("/api/nuoi_trang", methods=["POST"])
def api_luu():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "Chỉ máy chạy tool đổi được."}), 403
    import kenh_dang
    co = {k["id"][3:] for k in kenh_dang.ds_kenh() if k["nen"] == "fb"}
    b = request.get_json(silent=True) or {}
    d = doc()
    d["trang"] = [str(x) for x in (b.get("trang") or []) if str(x) in co][:10]
    _ghi(d)
    return jsonify({"ok": True, **tinh_hinh()})
