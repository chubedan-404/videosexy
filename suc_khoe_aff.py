"""Báo động "video không được phân phối" (16/11/2026).

Người dùng đăng 30 video Shopee lên 4 Fanpage, tất cả 0 view, mà phải tự đi hỏi mới biết. Ở đây:
- Hai lần mỗi ngày (9h và 21h) tool tự đọc số mọi video đăng trong 7 ngày (`hoc_aff.cap_nhat_so`, chỉ đọc).
- CODE chấm từng video và từng trang, không để AI đoán:
    video đăng từ 24 giờ, đã đọc số sau mốc 24 giờ, 0 view  -> "chet" (không được phân phối)
    3 video đo được gần nhất của một trang đều chết        -> trang "do", đề nghị tạm dừng đăng trang đó
    trang có view rồi tụt hẳn (3 video cuối < 20% trung bình trước đó) hoặc có video chết -> "cam"
- Overview và Shopee Autopilot đọc kết quả qua /api/suc_khoe_aff.
Dữ liệu: số ở downloads/shopee_auto/so_reel.json (của hoc_aff), giờ đọc lần cuối ở downloads/shopee_auto/suc_khoe.json.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Dict, List, Optional

from flask import Blueprint, jsonify, request

import xuong_chung as pc

bp = Blueprint("suc_khoe_aff", __name__)

TEP = os.path.join(pc.TAI_VE, "shopee_auto", "suc_khoe.json")
NGAY = 86400
TUOI_CHAM = NGAY             # video đăng chưa tới 24 giờ thì chưa chấm
KHOANG = 7 * NGAY            # bảng sức khoẻ tính 7 ngày gần nhất
LIEN_CHET = 3                # bấy nhiêu video đo được gần nhất cùng chết thì trang đỏ
GIO_DOC = (9, 21)            # giờ tự đọc số mỗi ngày
TUT = 0.2                    # 3 video cuối dưới 20% trung bình trước đó là tụt


def _cac_video() -> List[Dict]:
    """Mọi video Facebook đã lên (Fanpage chính + Fanpage phụ + video thường), mỗi cái một khoá số của hoc_aff."""
    try:
        import shopee_auto as sau
    except Exception:  # noqa: BLE001
        return []
    ra: List[Dict] = []
    if not os.path.isdir(sau.KHO):
        return ra
    for ten in sorted(os.listdir(sau.KHO)):
        if not (ten.startswith("sa_") and ten.endswith(".json")):
            continue
        d = pc.doc_json(os.path.join(sau.KHO, ten)) or {}
        lu, trang = d.get("id"), str((d.get("cai_dat") or {}).get("trang") or "")
        for m in d.get("muc") or []:
            ten_sp = m.get("ten") or ""
            dg = m.get("dang") or {}
            if dg.get("ok") and dg.get("ma"):
                ra.append({"khoa": f"{lu}:{m.get('thu')}", "kenh": dg.get("kenh") or "fb:" + trang, "ten_trang": dg.get("ten") or "",
                           "luot": lu, "thu": m.get("thu"), "ten": ten_sp, "link": dg.get("link") or "",
                           "luc": float(dg.get("luc") or d.get("tao_luc") or 0)})
            for kenh, kq in (m.get("dang_them") or {}).items():
                if kenh.startswith(("fb:", "fbv:")) and (kq or {}).get("ok") and kq.get("ma"):
                    ra.append({"khoa": f"{lu}:{m.get('thu')}:{kenh}", "kenh": kenh, "ten_trang": kq.get("ten") or "",
                               "luot": lu, "thu": m.get("thu"), "ten": ten_sp, "link": kq.get("link") or "",
                               "luc": float(kq.get("luc") or d.get("tao_luc") or 0)})
    return ra


def cham_video(v: Dict, so: Optional[Dict], bay: float) -> str:
    """moi: chưa đủ 24 giờ; chua_do: chưa có số đọc sau mốc 24 giờ; chet: 0 view; song: có view."""
    if bay - v["luc"] < TUOI_CHAM:
        return "moi"
    if not so or so.get("loi") or not so.get("luc") or so["luc"] < v["luc"] + TUOI_CHAM:
        return "chua_do"
    return "chet" if int(so.get("xem") or 0) == 0 else "song"


def cham_trang(ds: List[Dict]) -> str:
    """`ds`: video của một trang trong 7 ngày, cũ trước, mỗi cái có `tt` và `xem`. Trả xanh / cam / do / chua."""
    do = [v for v in ds if v["tt"] in ("chet", "song")]
    if not do:
        return "chua"
    cuoi = do[-LIEN_CHET:]
    if len(cuoi) >= LIEN_CHET and all(v["tt"] == "chet" for v in cuoi):
        return "do"
    truoc = do[:-LIEN_CHET]
    if truoc:
        tb_truoc = sum(v["xem"] for v in truoc) / len(truoc)
        tb_cuoi = sum(v["xem"] for v in cuoi) / len(cuoi)
        if tb_truoc >= 5 and tb_cuoi < TUT * tb_truoc:
            return "cam"
    return "cam" if any(v["tt"] == "chet" for v in cuoi) else "xanh"


def tinh(bay: Optional[float] = None, so: Optional[Dict] = None, video: Optional[List[Dict]] = None) -> Dict:
    """Chấm mọi video và mọi trang. `so`, `video` để bài kiểm truyền vào."""
    import hoc_aff
    bay = bay or time.time()
    so = hoc_aff._doc() if so is None else so
    video = _cac_video() if video is None else video
    ten_kenh = {}
    try:
        import kenh_dang
        ten_kenh = {k["id"]: k["ten"] for k in kenh_dang.ds_kenh()}
    except Exception:  # noqa: BLE001
        pass
    tt_video: Dict[str, str] = {}
    trang: Dict[str, List[Dict]] = {}
    for v in sorted(video, key=lambda x: x["luc"]):
        s = so.get(v["khoa"])
        tt = cham_video(v, s, bay)
        tt_video[v["khoa"]] = tt
        if bay - v["luc"] <= KHOANG:
            ma = v["kenh"].partition(":")[2]   # Reel và video thường của cùng trang tính chung một trang
            trang.setdefault(ma, []).append({**v, "tt": tt, "xem": int((s or {}).get("xem") or 0)})
    cac_trang = []
    for ma, ds in trang.items():
        do = [v for v in ds if v["tt"] in ("chet", "song")]
        muc = cham_trang(ds)
        cac_trang.append({
            "id": "fb:" + ma, "ten": ten_kenh.get("fb:" + ma) or next((v["ten_trang"] for v in ds if v["ten_trang"]), ma),
            "muc": muc, "tam_dung": muc == "do", "dang": len(ds), "da_do": len(do),
            "co_view": sum(1 for v in do if v["tt"] == "song"), "chet": sum(1 for v in do if v["tt"] == "chet"),
            "xem_tb": round(sum(v["xem"] for v in do) / len(do), 1) if do else None,
            "cuoi": max(v["luc"] for v in ds)})
    thu_tu = {"do": 0, "cam": 1, "xanh": 2, "chua": 3}
    cac_trang.sort(key=lambda t: (thu_tu[t["muc"]], -t["cuoi"]))
    chet7 = [v for ds in trang.values() for v in ds if v["tt"] == "chet"]
    trang_chet = {v["kenh"].partition(":")[2] for v in chet7}
    tom = ""
    if chet7:
        tom = (f"{len(chet7)} video đăng hơn 24 giờ vẫn 0 view, trên {len(trang_chet)} trang."
               + (f" {sum(1 for t in cac_trang if t['tam_dung'])} trang nên tạm dừng đăng." if any(t["tam_dung"] for t in cac_trang) else ""))
    lan = (pc.doc_json(TEP) or {}).get("lan_doc")
    return {"video": tt_video, "trang": cac_trang, "so_chet": len(chet7), "so_trang_chet": len(trang_chet), "tom_tat": tom,
            "lan_doc": lan, "lan_toi": lan_toi(bay)}


# ── Tự đọc số hai lần mỗi ngày ───────────────────────────────────────────────

def _moc_gan_nhat(bay: float) -> float:
    """Mốc giờ đọc gần nhất đã qua (9h hoặc 21h giờ máy)."""
    t = time.localtime(bay)
    dau = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    qua = [dau + g * 3600 for g in GIO_DOC if dau + g * 3600 <= bay]
    return qua[-1] if qua else dau - NGAY + GIO_DOC[-1] * 3600


def lan_toi(bay: Optional[float] = None) -> float:
    bay = bay or time.time()
    t = time.localtime(bay)
    dau = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    sau = [dau + g * 3600 for g in GIO_DOC if dau + g * 3600 > bay]
    return sau[0] if sau else dau + NGAY + GIO_DOC[0] * 3600


def den_gio(bay: float, lan_doc: Optional[float]) -> bool:
    return not lan_doc or lan_doc < _moc_gan_nhat(bay)


def doc_so(bay: Optional[float] = None, cach: int = 3600) -> Dict:
    """Đọc số mọi video đăng trong 8 ngày (số cũ hơn 1 giờ thì hỏi lại), ghi giờ đọc."""
    import hoc_aff
    bay = bay or time.time()
    kq = hoc_aff.cap_nhat_so(bay=bay, toi_da=400, cach=cach, tu=bay - KHOANG - NGAY)
    os.makedirs(os.path.dirname(TEP), exist_ok=True)
    pc.ghi_json(TEP, {"lan_doc": bay, "ket_qua": kq})
    return kq


def _vong_lap() -> None:
    time.sleep(60)   # để tool khởi động xong
    while True:
        try:
            if den_gio(time.time(), (pc.doc_json(TEP) or {}).get("lan_doc")):
                doc_so()
        except Exception as e:  # noqa: BLE001 - mất mạng, token hết hạn: lần sau thử lại
            print(f"[suc_khoe_aff] đọc số hỏng: {e}")
        time.sleep(300)


def khoi_dong() -> None:
    if "pytest" not in sys.modules:
        threading.Thread(target=_vong_lap, daemon=True, name="suc-khoe-aff").start()


# ── Đường vào ────────────────────────────────────────────────────────────────

@bp.route("/api/suc_khoe_aff", methods=["GET"])
def api_xem():
    return jsonify({"ok": True, **tinh()})


@bp.route("/api/suc_khoe_aff/doc", methods=["POST"])
def api_doc():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "Chỉ máy chạy tool bấm được."}), 403
    try:
        return jsonify({"ok": True, **doc_so(cach=300 if (request.get_json(silent=True) or {}).get("moi") else 3600), **tinh()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
