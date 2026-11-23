"""
PRODUCT PHOTOS (21/10/2026): ảnh sản phẩm chụp bừa thành ảnh sạch dùng cho quảng cáo và làm ảnh mẫu cho Flow / Ảnh AI.

1. Tách nền bằng rembg (github.com/danielgatis/rembg, MIT), model `isnet-general-use` chạy máy, dưới 1 giây một ảnh
   (đo 21/10/2026). `birefnet-general-lite` đã thử: hơn 9 phút chưa xong một ảnh trên CPU, bỏ.
2. Dọn mặt nạ: bỏ vệt mờ (alpha thấp), tuỳ chọn chỉ giữ vật lớn nhất (ảnh giày thử còn dính chiếc tất bên cạnh).
3. Đặt lên nền: trong suốt, trắng, màu, studio (xám sáng loang), hoặc NỀN AI: Ảnh AI (`lf.generate_image_router`, model ở
   Cài đặt > Tài khoản AI) chỉ vẽ CẢNH TRỐNG, rồi tool ghép sản phẩm thật vào, có bóng đổ. KHÔNG nhờ AI vẽ lại sản phẩm:
   đo 21/10/2026, 9Router với ag/gemini-3.1-flash-image bỏ qua ảnh mẫu (gửi kiểu "image", "images" hay chat image_url đều
   vậy), ra một chai khác hẳn; ghép thì sản phẩm giữ nguyên từng điểm ảnh, nhãn chữ không bao giờ sai.
   Khung: giữ cỡ gốc, 1:1, 4:5, 9:16; sản phẩm cắt sát rồi đặt giữa, có bóng đổ mềm nếu chọn.

Máy chủ: `/api/product_photos/*`. Trang: `static/product_photos.js` + `.css`. Bài kiểm: `tests/test_product_photos.py`.
Dữ liệu: `downloads/product_photos/` (kho.json + ảnh). Việc nền tiền tố `pp_`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, send_file, send_from_directory

import longform_studio as lf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "product_photos")
MODEL = "isnet-general-use"
NEN = ("trong_suot", "trang", "mau", "studio", "ai")
KHUNG = {"goc": None, "1:1": (1080, 1080), "4:5": (1080, 1350), "9:16": (1080, 1920)}
CANH_AI = {
    "da": "on a polished white marble countertop, soft morning window light, minimal luxury bathroom blurred behind",
    "studio": "on a clean seamless pastel studio backdrop with a soft pedestal, professional softbox lighting, gentle shadow",
    "thien_nhien": "on a natural stone among fresh green leaves and soft sunlight, outdoor, shallow depth of field",
    "go": "on a warm wooden table in a cozy cafe, natural light, blurred background with plants",
    "toi": "on a dark matte surface with dramatic rim light and soft reflections, premium low-key look",
}
TOI_DA = 50

lay_cai_dat_ai: Callable[[], Dict] = lambda: {}
_khoa = threading.RLock()
_khoa_rembg = threading.Lock()
_phien: Dict = {"s": None}
_KHO: List[Dict] = []
_da_nap = False
_viec = {"id": ""}
bp = Blueprint("product_photos", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# KHO
# ─────────────────────────────────────────────────────────────────────────────

def _duong(*p: str) -> str:
    return os.path.join(KHO, *p)


def _nap() -> None:
    global _KHO, _da_nap
    with _khoa:
        if _da_nap:
            return
        try:
            with open(_duong("kho.json"), encoding="utf-8") as f:
                _KHO = json.load(f).get("anh") or []
        except Exception:  # noqa: BLE001
            _KHO = []
        for a in _KHO:
            if a.get("tt") == "chay":
                a.update(tt="loi", loi="Tool khởi động lại giữa chừng. Bấm Xử lý lại.")
        _da_nap = True


def _luu() -> None:
    with _khoa:
        os.makedirs(KHO, exist_ok=True)
        tam = _duong("kho.json.tam")
        with open(tam, "w", encoding="utf-8") as f:
            json.dump({"anh": _KHO}, f, ensure_ascii=False, indent=1)
        os.replace(tam, _duong("kho.json"))


def _tim(ma: str) -> Optional[Dict]:
    _nap()
    with _khoa:
        return next((a for a in _KHO if a["id"] == ma), None)


def _sua(a: Dict, **gia_tri) -> None:
    with _khoa:
        a.update(gia_tri)
        _luu()


def them(ten: str, luu: Callable[[str], None]) -> Dict:
    _nap()
    duoi = os.path.splitext(ten or "")[1].lower()
    if duoi not in (".png", ".jpg", ".jpeg", ".webp", ".heic"):
        raise ValueError("Ảnh phải là PNG, JPG, WEBP hoặc HEIC.")
    with _khoa:
        if len(_KHO) >= 300:
            raise ValueError("Kho đã có 300 ảnh, xoá bớt rồi thêm.")
    ma = time.strftime("pp%Y%m%d%H%M%S") + uuid.uuid4().hex[:6]
    os.makedirs(KHO, exist_ok=True)
    goc = f"{ma}_goc{duoi}"
    luu(_duong(goc))
    try:
        from PIL import Image, ImageOps
        with Image.open(_duong(goc)) as im:
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            if duoi == ".heic" or max(w, h) > 2400:   # ảnh điện thoại 4000px: thu về 2400 cho nhanh, HEIC đổi sang PNG
                im.thumbnail((2400, 2400))
                moi = f"{ma}_goc.png"
                im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(_duong(moi))
                if moi != goc:
                    os.remove(_duong(goc))
                goc = moi
                w, h = im.size
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(_duong(goc))
        except OSError:
            pass
        raise ValueError(f"Không đọc được ảnh {ten}: {str(e)[:80]}")
    a = {"id": ma, "ten": os.path.basename(ten)[:120], "goc": goc, "rong": w, "cao": h, "tach": "", "ra": "",
         "tt": "moi", "loi": "", "tao_luc": time.time(), "cai_dat": {}}
    with _khoa:
        _KHO.insert(0, a)
        _luu()
    return a


def xoa(ma: str) -> None:
    a = _tim(ma)
    if not a:
        raise ValueError("Không có ảnh này.")
    if a.get("tt") == "chay":
        raise ValueError("Ảnh đang xử lý, đợi xong rồi xoá.")
    with _khoa:
        _KHO.remove(a)
        _luu()
    for k in ("goc", "tach_tho", "tach", "ra"):
        if a.get(k):
            try:
                os.remove(_duong(a[k]))
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# TÁCH NỀN, DỌN MẶT NẠ, ĐẶT LÊN NỀN
# ─────────────────────────────────────────────────────────────────────────────

def tach_nen(duong: str):
    """Ảnh RGBA đã tách nền (rembg, model isnet). Model tải về ~/.u2net lần đầu (khoảng 170 MB)."""
    from PIL import Image, ImageOps
    from rembg import new_session, remove
    with _khoa_rembg:
        if _phien["s"] is None:
            # CHỈ CPU. Để mặc định thì onnxruntime gọi CoreML/Neural Engine: ngày 21/10/2026 thử birefnet làm tiến trình hệ
            # thống ANECompilerService kẹt 100% CPU hơn 20 phút và mọi lần tách sau đó treo theo. isnet trên CPU ~1,5 giây/ảnh.
            _phien["s"] = new_session(MODEL, providers=["CPUExecutionProvider"])
        with Image.open(duong) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            return remove(im, session=_phien["s"]).convert("RGBA")


def lam_sach(anh, giu_lon_nhat: bool = False, nguong: int = 40):
    """Bỏ vệt mờ (alpha dưới `nguong`) và, nếu chọn, mọi mảng tách rời nhỏ hơn mảng lớn nhất."""
    import numpy as np
    from PIL import Image
    a = np.array(anh)
    alpha = a[:, :, 3].astype(np.int32)
    alpha[alpha < nguong] = 0
    if giu_lon_nhat and alpha.any():
        from scipy import ndimage
        nhan, so = ndimage.label(alpha > 127)
        if so > 1:
            co = ndimage.sum(np.ones_like(nhan), nhan, index=range(1, so + 1))
            lon = int(np.argmax(co)) + 1
            # Giữ cả mảng lớn nhất và phần viền mờ dính liền với nó (giãn mặt nạ vài pixel), bỏ phần còn lại.
            giu = ndimage.binary_dilation(nhan == lon, iterations=6)
            alpha[~giu] = 0
    a[:, :, 3] = alpha.astype(np.uint8)
    return Image.fromarray(a, "RGBA")


def cat_sat(anh):
    hop = anh.getchannel("A").getbbox()
    return anh.crop(hop) if hop else anh


def _nen_studio(w: int, h: int):
    """Xám sáng loang: giữa sáng, mép tối nhẹ, như phông studio."""
    import numpy as np
    from PIL import Image
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt(((xx - w / 2) / (w * 0.75)) ** 2 + ((yy - h * 0.42) / (h * 0.75)) ** 2)
    v = np.clip(246 - 46 * d, 190, 246).astype(np.uint8)
    return Image.fromarray(np.dstack([v, v, np.clip(v.astype(int) + 2, 0, 255).astype(np.uint8), np.full_like(v, 255)]), "RGBA")


def _mau(hex_: str) -> Tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_ or "")
    s = m.group(1) if m else "f2f2f2"
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def dat_len_nen(tach, nen: str = "trang", mau: str = "#ffffff", khung: str = "1:1", bong: bool = True, le: float = 0.12,
                nen_anh=None, cao_vat: float = 0.0, chan: float = 0.0):
    """
    Sản phẩm (RGBA đã tách) cắt sát, đặt giữa khung, thêm bóng đổ mềm, lên nền chọn. Trả về ảnh RGBA.
    `nen_anh`: ảnh nền có sẵn (nền AI), khi đó `cao_vat` là chiều cao sản phẩm theo tỉ lệ khung và `chan` là vị trí chân
    sản phẩm theo tỉ lệ chiều cao (mặt bàn).
    """
    from PIL import Image, ImageFilter
    hop = tach.getchannel("A").getbbox() or (0, 0, tach.width, tach.height)
    # Vật bị cắt ở mép dưới ảnh gốc (tay cầm sản phẩm, cổ tay chạm mép): đặt sát đáy khung như thò vào từ ngoài, không
    # dựng đứng giữa cảnh (trông như bàn tay lơ lửng) và không có bóng elip dưới chân.
    cham_day = hop[3] >= tach.height - 2
    vat = cat_sat(tach)
    if nen_anh is not None:
        W, H = nen_anh.size
        ti = min(H * (0.72 if cham_day else cao_vat) / vat.height, W * 0.8 / vat.width)
        vat = vat.resize((max(1, int(vat.width * ti)), max(1, int(vat.height * ti))), Image.LANCZOS)
        x, y = (W - vat.width) // 2, (H - vat.height if cham_day else int(H * chan) - vat.height)
    elif khung not in KHUNG or KHUNG[khung] is None:
        W, H = tach.size
        x, y = (tach.getchannel("A").getbbox() or (0, 0, 0, 0))[:2]
    else:
        W, H = KHUNG[khung]
        ti = min(W * (1 - 2 * le) / vat.width, H * (1 - 2 * le) / vat.height)
        vat = vat.resize((max(1, int(vat.width * ti)), max(1, int(vat.height * ti))), Image.LANCZOS)
        x, y = (W - vat.width) // 2, (H - vat.height if cham_day else int((H - vat.height) * 0.52))
    if nen_anh is not None:
        nen_anh = nen_anh.convert("RGBA")
    elif nen == "trong_suot":
        nen_anh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    elif nen == "studio":
        nen_anh = _nen_studio(W, H)
    else:
        nen_anh = Image.new("RGBA", (W, H), _mau(mau if nen == "mau" else "#ffffff") + (255,))
    if bong and nen != "trong_suot" and not cham_day:
        # Bóng đổ: elip mờ dưới chân sản phẩm + bóng mờ của chính dáng sản phẩm lệch xuống.
        bong_anh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dang = Image.new("RGBA", vat.size, (0, 0, 0, 60))
        dang.putalpha(vat.getchannel("A").point(lambda v: v * 60 // 255))
        bong_anh.alpha_composite(dang, (x + vat.width // 40, y + vat.height // 30))
        from PIL import ImageDraw
        ve = ImageDraw.Draw(bong_anh)
        cao_e = max(6, vat.height // 18)
        ve.ellipse((x + vat.width * 0.12, y + vat.height - cao_e // 2, x + vat.width * 0.88, y + vat.height + cao_e // 2), fill=(0, 0, 0, 70))
        bong_anh = bong_anh.filter(ImageFilter.GaussianBlur(max(4, min(W, H) // 60)))
        nen_anh.alpha_composite(bong_anh)
    nen_anh.alpha_composite(vat, (x, y))
    return nen_anh


def nen_ai(tach, ra: str, canh: str, khung: str) -> None:
    """Ảnh AI vẽ cảnh trống theo `canh`, rồi ghép sản phẩm thật (RGBA đã tách) vào giữa, chân đặt trên mặt bàn."""
    from PIL import Image
    import ket_noi
    anh = ket_noi.may_anh()
    cai = dict(lay_cai_dat_ai() or {})
    if anh.get("key"):
        cai["api_key"] = anh["key"]
    w, h = KHUNG.get(khung) or (1080, 1080)
    loi_ta = (f"Empty background plate for a product photograph: an empty surface {canh}. The surface in the lower-middle of the "
              "frame is completely clear and in focus, ready for a product to be placed on it later. Eye-level camera, the surface "
              "edge runs across the lower third, background softly blurred. NO products, NO bottles, NO objects in the center, "
              "no people, no hands, no text, no logo, no watermark.")
    tam = ra + ".nen.png"
    kq = lf.generate_image_router(loi_ta, tam, cai, model=anh["model"], fmt={"image_width": w, "image_height": h}, no_text=True)
    if not kq.get("success"):
        loi = kq.get("error") or "Ảnh AI không trả ảnh."
        if "HTTP 401" in loi or "HTTP 403" in loi or "No credentials" in loi:
            loi = "9Router từ chối vẽ (khoá sai hoặc tài khoản bị đăng xuất). Kiểm ở Cài đặt > Tài khoản AI."
        raise RuntimeError(loi[:300])
    try:
        with Image.open(tam) as nen:
            nen = nen.convert("RGBA").resize((w, h), Image.LANCZOS)
        # Sản phẩm cao khoảng 52% khung (khung dọc 9:16 thì 40%), chân ở 80% chiều cao: chỗ mặt bàn trong cảnh vừa dặn.
        dat_len_nen(tach, nen_anh=nen, cao_vat=0.40 if h > w * 1.5 else 0.52, chan=0.80).convert("RGB").save(ra)
    finally:
        try:
            os.remove(tam)
        except OSError:
            pass


def chuan_cai_dat(c: Dict) -> Dict:
    c = c or {}
    canh = str(c.get("canh") or "")
    return {"nen": c.get("nen") if c.get("nen") in NEN else "trang",
            "mau": c.get("mau") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(c.get("mau") or "")) else "#f2efe9",
            "khung": c.get("khung") if c.get("khung") in KHUNG else "1:1",
            "bong": bool(c.get("bong", True)), "lon_nhat": bool(c.get("lon_nhat", False)),
            "canh": CANH_AI.get(canh, canh.strip()[:400]) or CANH_AI["studio"]}


def xu_ly_mot(a: Dict, cai: Dict) -> None:
    """Tách nền (một lần, cất lại), dọn mặt nạ, rồi đặt lên nền chọn."""
    if not a.get("tach_tho") or not os.path.isfile(_duong(a["tach_tho"])):
        tho = tach_nen(_duong(a["goc"]))
        a["tach_tho"] = f"{a['id']}_tach_tho.png"
        tho.save(_duong(a["tach_tho"]))
    from PIL import Image
    tho_duong = _duong(a.get("tach_tho") or a["tach"])
    with Image.open(tho_duong) as tho:
        sach = lam_sach(tho.convert("RGBA"), cai["lon_nhat"])
    if not sach.getchannel("A").getbbox():
        raise RuntimeError("Không tìm thấy sản phẩm nào trong ảnh để tách.")
    tach = f"{a['id']}_tach.png"
    sach.save(_duong(tach))
    cu = a.get("ra")
    if cai["nen"] == "ai":
        ra = f"{a['id']}_ra_{int(time.time())}.png"
        nen_ai(sach, _duong(ra), cai["canh"], cai["khung"] if cai["khung"] != "goc" else "1:1")
    else:
        ra = f"{a['id']}_ra_{int(time.time())}.png"
        dat_len_nen(sach, cai["nen"], cai["mau"], cai["khung"], cai["bong"]).save(_duong(ra))
    if cu and cu != ra:
        try:
            os.remove(_duong(cu))
        except OSError:
            pass
    _sua(a, tach=tach, ra=ra, tt="xong", loi="", cai_dat=cai, xong_luc=time.time())


def chay(ids: List[str], cai_dat: Dict) -> str:
    """Xử lý các ảnh `ids` theo cùng một cài đặt, lần lượt. Trả về mã việc nền."""
    _nap()
    if _viec["id"] and (lf.get_job(_viec["id"]) or {}).get("status") == "running":
        raise ValueError("Đang xử lý một lượt khác, đợi xong đã.")
    cai = chuan_cai_dat(cai_dat)
    ds = [a for a in (_tim(m) for m in ids or []) if a][:TOI_DA]
    if not ds:
        raise ValueError("Chọn ít nhất một ảnh.")
    for a in ds:
        _sua(a, tt="cho", loi="")

    def lam(viec: Dict) -> None:
        for k, a in enumerate(ds):
            if viec.get("cancel"):
                for b in ds[k:]:
                    _sua(b, tt="huy" if b.get("tt") == "cho" else b.get("tt"), loi="Đã dừng.")
                return
            lf.update_job(viec, step=f"Đang xử lý ảnh {k + 1}/{len(ds)}", progress=int(100 * k / len(ds)))
            _sua(a, tt="chay")
            try:
                xu_ly_mot(a, cai)
            except Exception as e:  # noqa: BLE001 - một ảnh hỏng không kéo cả lượt
                _sua(a, tt="loi", loi=str(e)[:300])
        lf.update_job(viec, step="Xong", progress=100)

    _viec["id"] = lf.start_job("pp_xu_ly", lam)
    return _viec["id"]


def ban_gui() -> Dict:
    _nap()
    v = lf.get_job(_viec["id"]) or {} if _viec["id"] else {}
    with _khoa:
        return {"anh": list(_KHO), "viec": {"chay": v.get("status") == "running", "tien_do": v.get("progress") or 0,
                                            "dang_lam": v.get("step") or "", "id": _viec["id"]}}


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _loi(msg: str, ma: int = 400):
    return jsonify({"ok": False, "error": msg}), ma


@bp.get("/api/product_photos/phien")
def api_phien():
    v = lf.get_job(_viec["id"]) or {} if _viec["id"] else {}
    return jsonify({"ok": True, "job_id": _viec["id"] if v.get("status") == "running" else ""})


@bp.get("/api/product_photos/kho")
def api_kho():
    return jsonify({"ok": True, **ban_gui(), "canh": list(CANH_AI)})


@bp.post("/api/product_photos/anh")
def api_them():
    tep = request.files.getlist("anh")
    if not tep:
        return _loi("Chưa chọn ảnh nào.")
    moi, loi = [], []
    for f in tep[:TOI_DA]:
        try:
            moi.append(them(f.filename or "anh.png", f.save))
        except ValueError as e:
            loi.append(str(e))
    if not moi:
        return _loi("; ".join(loi) or "Không thêm được ảnh nào.")
    return jsonify({"ok": True, "moi": [a["id"] for a in moi], "loi": loi})


@bp.post("/api/product_photos/xu_ly")
def api_xu_ly():
    t = request.get_json(silent=True) or {}
    try:
        ma = chay(t.get("ids") or [], t.get("cai_dat") or {})
    except ValueError as e:
        return _loi(str(e))
    return jsonify({"ok": True, "job_id": ma})


@bp.post("/api/product_photos/dung")
def api_dung():
    return jsonify({"ok": bool(_viec["id"] and lf.cancel_job(_viec["id"]))})


@bp.delete("/api/product_photos/anh/<ma>")
def api_xoa(ma):
    try:
        xoa(ma)
    except ValueError as e:
        return _loi(str(e))
    return jsonify({"ok": True})


@bp.get("/api/product_photos/tep/<ten>")
def api_tep(ten):
    if not re.fullmatch(r"pp\d{14}[0-9a-f]{6}_(goc|tach|ra_\d+)\.(png|jpg|jpeg|webp)", ten or ""):
        return _loi("Không có file này.", 404)
    return send_from_directory(KHO, ten, as_attachment=request.args.get("tai") == "1")


@bp.get("/api/product_photos/zip")
def api_zip():
    import tempfile
    import zipfile
    ids = set((request.args.get("ids") or "").split(","))
    _nap()
    with _khoa:
        co = [a for a in _KHO if a.get("ra") and (not ids - {""} or a["id"] in ids) and os.path.isfile(_duong(a["ra"]))]
    if not co:
        return _loi("Chưa có ảnh nào xử lý xong.")
    f = tempfile.TemporaryFile()
    with zipfile.ZipFile(f, "w", zipfile.ZIP_STORED) as z:
        for k, a in enumerate(co, 1):
            z.write(_duong(a["ra"]), f"{k:02d}_{os.path.splitext(a['ten'])[0][:50]}.png")
    f.seek(0)
    return send_file(f, mimetype="application/zip", as_attachment=True, download_name="product_photos.zip")
