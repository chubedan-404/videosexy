"""
SHOPEE THEO TỪ KHOÁ (14/11/2026): mỗi ngày tự tìm theo danh sách từ khoá, tự chọn món, tự lấy link aff, tự làm video.

Người dùng hỏi "làm sao scale được mà không cần ngồi với bạn", chọn làm phần từ khoá trước. Máy chủ KHÔNG gọi được tiện ích
Chrome, nên tiện ích tự hỏi (chrome.alarms mỗi phút, `tien_ich_shopee/nen.js`):
  1. GET  /api/shopee_tu_khoa/viec   -> tới giờ (hoặc người dùng bấm "Chạy ngay") thì trả danh sách từ khoá.
  2. tiện ích tìm từng từ khoá trên affiliate.shopee.vn (như nút Tìm), gửi POST /api/shopee_tu_khoa/chon với kết quả.
  3. máy chủ chọn món (`chon`): đúng loại hàng (AI bỏ phụ kiện), qua luật giảm / hoa hồng, chưa làm lần nào, xếp điểm, chia đều
     các từ khoá, tối đa `so_video` món; trả danh sách để tiện ích lấy link aff có sub_id.
  4. POST /api/shopee_tu_khoa/xong -> tạo lượt Autopilot (`shopee_auto.tao_tu_web`) với cài đặt đã lưu, rồi bắt đầu làm video.
     Video xong chờ người dùng duyệt (duyệt trước khi đăng giữ nguyên như Autopilot).
Giữ đúng giới hạn đã nói với người dùng: chỉ trang affiliate, tối đa 10 món mỗi lần, tiện ích đi chậm và dừng khi gặp xác minh.
"""
import math
import re
import threading
import time
import unicodedata
from typing import Dict, List

from flask import Blueprint, jsonify, request

import xuong_chung as pc

bp = Blueprint("shopee_tu_khoa", __name__)
_khoa = threading.Lock()
TOI_DA_TU = 20
TOI_DA_VIDEO = 10          # = shopee_auto.TOI_DA_LINK, tiện ích lấy tối đa 10 link mỗi lần
QUA_HAN_GIAY = 45 * 60     # một lần chạy treo quá lâu (Chrome tắt giữa chừng) thì cho chạy lại
NGAT_GIAY = 90             # tiện ích báo rảnh mà lượt bắt đầu quá 90 giây trước: lượt đó đã bị ngắt
NHAT_KY_TOI_DA = 30
DA_LAM_TOI_DA = 3000


def _tep() -> str:
    import shopee_auto
    return f"{shopee_auto.KHO}/tu_khoa.json"


MAC_DINH = {"bat": False, "tu_khoa": [], "gio": "08:00", "so_video": 3, "giam_toi_thieu": 0, "hoa_hong_toi_thieu": 0,
            "cai_dat": {}, "ngay_chay": "", "chay_ngay": False, "dang": None, "lan_hoi": 0, "da_lam": [], "nhat_ky": []}


def doc() -> Dict:
    return {**MAC_DINH, **(pc.doc_json(_tep()) or {})}


def _ghi(d: Dict) -> None:
    d["da_lam"] = d.get("da_lam", [])[-DA_LAM_TOI_DA:]
    d["nhat_ky"] = d.get("nhat_ky", [])[-NHAT_KY_TOI_DA:]
    pc.ghi_json(_tep(), d)


def _ghi_nhat_ky(d: Dict, chu: str) -> None:
    d.setdefault("nhat_ky", []).append({"luc": time.time(), "chu": chu[:400]})


def luu(moi: Dict) -> Dict:
    import shopee_auto
    tu = []
    for x in re.split(r"[\n,]", str(moi.get("tu_khoa") or "") if not isinstance(moi.get("tu_khoa"), list) else "\n".join(moi["tu_khoa"])):
        x = re.sub(r"\s+", " ", x).strip()[:60]
        if x and x.lower() not in {t.lower() for t in tu}:
            tu.append(x)
    if len(tu) > TOI_DA_TU:
        raise ValueError(f"Tối đa {TOI_DA_TU} từ khoá.")
    gio = str(moi.get("gio") or "08:00")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", gio):
        raise ValueError("Giờ chạy dạng 08:00.")
    so = int(moi.get("so_video") or 3)
    if not 1 <= so <= TOI_DA_VIDEO:
        raise ValueError(f"Mỗi ngày từ 1 tới {TOI_DA_VIDEO} video.")
    cd = shopee_auto._cai_dat(moi.get("cai_dat") or {})   # kiểm Fanpage, giọng, kiểu video... như khi tạo lượt tay
    with _khoa:
        d = doc()
        d.update(bat=bool(moi.get("bat")) and bool(tu), tu_khoa=tu, gio=gio, so_video=so, cai_dat=cd,
                 giam_toi_thieu=max(0, min(90, int(moi.get("giam_toi_thieu") or 0))),
                 hoa_hong_toi_thieu=max(0.0, min(50.0, float(moi.get("hoa_hong_toi_thieu") or 0))))
        _ghi(d)
    return d


def _den_gio(d: Dict, bay_gio: float) -> bool:
    if d.get("chay_ngay"):
        return bool(d.get("tu_khoa"))
    if not d.get("bat") or not d.get("tu_khoa"):
        return False
    hom_nay = time.strftime("%Y-%m-%d", time.localtime(bay_gio))
    return d.get("ngay_chay") != hom_nay and time.strftime("%H:%M", time.localtime(bay_gio)) >= d.get("gio", "08:00")


def viec(bay_gio: float = 0.0, ranh: bool = False) -> Dict:
    """Tiện ích hỏi mỗi phút. Trả {"lam": False} hoặc {"lam": True, "ma", "tu_khoa"}; đánh dấu đang chạy để không chạy đôi.

    `ranh`: tiện ích bản 1.2.0 báo nó đang rảnh. Đang có lượt chạy dở mà tiện ích lại rảnh nghĩa là lượt đó đã bị ngắt (15/11/2026
    đo thật: Chrome cho phần nền của tiện ích nghỉ khi bước AI chọn món chờ 32 giây), nên chạy lại ngay một lần thay vì chờ 45 phút."""
    bay_gio = bay_gio or time.time()
    with _khoa:
        d = doc()
        d["lan_hoi"] = bay_gio
        dang = d.get("dang") or None
        tre = bay_gio - float((dang or {}).get("luc") or 0)
        if dang and tre < QUA_HAN_GIAY and not (ranh and tre > NGAT_GIAY):
            _ghi(d)
            return {"lam": False}
        if dang:
            d["lan_ngat"] = int(d.get("lan_ngat") or 0) + 1
            if d["lan_ngat"] <= 2:
                _ghi_nhat_ky(d, "Lần chạy trước bị ngắt giữa chừng (Chrome tắt hay tiện ích bị cho nghỉ), chạy lại.")
                d["chay_ngay"] = True
            else:
                _ghi_nhat_ky(d, "Bị ngắt 3 lần liền, thôi không chạy lại. Kiểm tiện ích (chrome://extensions) rồi bấm Chạy ngay.")
                d["lan_ngat"] = 0
        if not _den_gio(d, bay_gio):
            d["dang"] = None
            _ghi(d)
            return {"lam": False}
        ma = time.strftime("tk_%Y%m%d_%H%M%S", time.localtime(bay_gio))
        d.update(dang={"ma": ma, "luc": bay_gio}, chay_ngay=False, ngay_chay=time.strftime("%Y-%m-%d", time.localtime(bay_gio)))
        _ghi_nhat_ky(d, f"Bắt đầu tìm {len(d['tu_khoa'])} từ khoá: {', '.join(d['tu_khoa'])}")
        _ghi(d)
        return {"lam": True, "ma": ma, "tu_khoa": d["tu_khoa"]}


# ─────────────────────────────────────────────────────────────────────────────
# CHỌN MÓN
# ─────────────────────────────────────────────────────────────────────────────

def so_ban(chu: str) -> int:
    """'348 lượt bán' -> 348, '10k+ lượt bán' -> 10000, '1,2k' -> 1200, '2,5tr' -> 2500000."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(k|tr|triệu)?", str(chu or "").lower())
    if not m:
        return 0
    so = float(m.group(1).replace(",", "."))
    return int(so * (1000 if m.group(2) == "k" else 1_000_000 if m.group(2) in ("tr", "triệu") else 1))


def diem(sp: Dict) -> float:
    """Điểm xếp món: mức giảm + hoa hồng (nặng hơn, là tiền của người dùng) + lượt bán (thang log, đỡ lệch món bán cực nhiều)."""
    return float(sp.get("giam") or 0) + 3 * float(sp.get("ty_le") or 0) + 10 * math.log10(so_ban(sp.get("da_ban")) + 1)


def _bo_dau(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn").replace("đ", "d")


def _khop_chu(tu: str, ten: str) -> bool:
    """Dự phòng khi AI lỗi: tên chứa ít nhất nửa số chữ của từ khoá (bỏ dấu)."""
    chu = _bo_dau(tu).split()
    t = _bo_dau(ten)
    return bool(chu) and sum(1 for c in chu if re.search(rf"\b{re.escape(c)}\b", t)) * 2 >= len(chu)


def dung_loai(tu: str, ds: List[Dict]) -> List[bool]:
    """
    Món nào ĐÚNG là loại hàng người dùng tìm, không phải phụ kiện, linh kiện, đồ thay thế (14/11/2026: tìm "nồi chiên không dầu"
    ra động cơ quạt, giỏ lót, chân cao su của nồi chiên). AI đọc tên; AI lỗi thì dùng `_khop_chu`.
    """
    if not ds:
        return []
    de = (f"Người dùng tìm trên Shopee: \"{tu}\". Dưới đây là tên các kết quả (tên shop đặt, nhồi từ khoá). Món nào ĐÚNG là loại "
          "hàng người dùng muốn mua (chính sản phẩm đó), món nào là phụ kiện, linh kiện, đồ thay thế, đồ dùng kèm hay hàng khác?\n\n"
          + "\n".join(f"{i}. {str(x.get('ten') or '')[:160]}" for i, x in enumerate(ds))
          + '\n\nTrả về JSON: {"dung": [chỉ số các món đúng loại]}')
    try:
        got = pc.hoi_ai("Bạn phân loại kết quả tìm kiếm Shopee. Chỉ trả JSON.", de)
        dung = {int(i) for i in got.get("dung") or [] if str(i).lstrip("-").isdigit()}
        return [i in dung for i in range(len(ds))]
    except Exception:  # noqa: BLE001 - AI hỏng thì vẫn chạy được bằng luật chữ
        return [_khop_chu(tu, str(x.get("ten") or "")) for x in ds]


def chon(ma: str, ket_qua: Dict[str, List[Dict]]) -> Dict:
    """Từ kết quả tìm của từng từ khoá, chọn tối đa `so_video` món. Trả {"chon": [...], "sub": [...]}."""
    d = doc()
    if (d.get("dang") or {}).get("ma") != ma:
        raise ValueError("Lần chạy này đã hết hạn hoặc không phải lần đang chạy.")
    da_lam = set(d.get("da_lam") or [])
    hang: Dict[str, List[Dict]] = {}
    bo = {"phu_kien": 0, "luat": 0, "da_lam": 0}
    for tu in d.get("tu_khoa") or []:
        ds = [x for x in (ket_qua or {}).get(tu) or [] if isinstance(x, dict) and re.fullmatch(r"\d{1,20}", str(x.get("item_id") or ""))
              and isinstance(x.get("gia"), (int, float)) and x["gia"] > 0][:30]
        loai = dung_loai(tu, ds)
        tot = []
        for x, ok in zip(ds, loai):
            if not ok:
                bo["phu_kien"] += 1
            elif str(x["item_id"]) in da_lam:
                bo["da_lam"] += 1
            elif float(x.get("giam") or 0) < d["giam_toi_thieu"] or float(x.get("ty_le") or 0) < d["hoa_hong_toi_thieu"]:
                bo["luat"] += 1
            else:
                tot.append({**x, "tu_khoa": tu})
        hang[tu] = sorted(tot, key=diem, reverse=True)
    ra, thay = [], set()
    while len(ra) < d["so_video"] and any(hang.values()):   # chia đều: mỗi vòng lấy món tốt nhất còn lại của từng từ khoá
        for tu in list(hang):
            if hang[tu] and len(ra) < d["so_video"]:
                x = hang[tu].pop(0)
                if x["item_id"] not in thay:
                    thay.add(x["item_id"])
                    ra.append(x)
    with _khoa:
        d = doc()
        _ghi_nhat_ky(d, f"Chọn {len(ra)} món ({', '.join(str(x.get('ten') or '')[:30] for x in ra)}). Bỏ: {bo['phu_kien']} phụ kiện / "
                        f"khác loại, {bo['luat']} không qua luật giảm-hoa hồng, {bo['da_lam']} đã làm trước đây.")
        if not ra:
            d["dang"] = None
        _ghi(d)
    return {"chon": ra, "sub": ["tukhoa", time.strftime("%Y%m%d")]}


def xong(ma: str, ds: List[Dict], dung: str = "") -> Dict:
    """Tiện ích đã lấy link: tạo lượt Autopilot với cài đặt đã lưu, rồi bắt đầu làm video."""
    import shopee_auto
    d = doc()
    if (d.get("dang") or {}).get("ma") != ma:
        raise ValueError("Lần chạy này đã hết hạn hoặc không phải lần đang chạy.")
    co = [x for x in ds or [] if isinstance(x, dict) and x.get("link_aff")]
    ket = {"luot": "", "job": "", "chu": ""}
    try:
        if not co:
            raise RuntimeError("Không lấy được link aff nào" + (f": {dung}" if dung else "."))
        luot = shopee_auto.tao_tu_web(co, d.get("cai_dat") or {})
        ket["luot"] = luot["id"]
        try:
            ket["job"] = shopee_auto.bat_dau(luot["id"])
            ket["chu"] = f"Tạo lượt {luot['id']} ({len(co)} món) và bắt đầu làm video."
        except RuntimeError as e:
            ket["chu"] = f"Tạo lượt {luot['id']} ({len(co)} món), CHƯA làm video: {e} Mở lượt đó bấm Làm video."
        if dung:
            ket["chu"] += f" Tiện ích dừng sớm: {dung}"
    except Exception as e:  # noqa: BLE001
        ket["chu"] = f"Hỏng: {e}"
    with _khoa:
        d = doc()
        d["da_lam"] = list(d.get("da_lam") or []) + [str(x["item_id"]) for x in co if x.get("item_id")]
        d["dang"] = None
        d["lan_ngat"] = 0
        _ghi_nhat_ky(d, ket["chu"])
        _ghi(d)
    return ket


def bao_loi(ma: str, loi: str) -> None:
    with _khoa:
        d = doc()
        if (d.get("dang") or {}).get("ma") == ma:
            d["dang"] = None
        _ghi_nhat_ky(d, f"Tiện ích báo lỗi: {str(loi)[:300]}")
        _ghi(d)


def chay_ngay() -> Dict:
    with _khoa:
        d = doc()
        if not d.get("tu_khoa"):
            raise ValueError("Chưa có từ khoá nào. Nhập từ khoá rồi bấm Lưu trước.")
        d["chay_ngay"] = True
        _ghi_nhat_ky(d, "Bấm Chạy ngay: tiện ích sẽ bắt đầu trong khoảng 1 phút (Chrome phải đang mở).")
        _ghi(d)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# GỢI Ý TỪ KHOÁ ĐANG NÓNG (15/11/2026)
# Nguồn: TikTok Creative Center Top Ads ở Việt Nam theo ngành (nguon_quang_cao.lay_tiktok_top, trang công khai, không đăng nhập).
# AI chỉ đọc chữ quảng cáo và gom thành cụm tìm Shopee; số quảng cáo đỡ mỗi cụm do CODE đếm từ chỉ số AI trả (chỉ số sai bị bỏ).
# ─────────────────────────────────────────────────────────────────────────────
NGANH_GOI_Y = {"18000000000": "Household Products", "16000000000": "Appliances", "14000000000": "Beauty & Personal Care",
               "21000000000": "Home Improvement", "12000000000": "Baby, Kids & Maternity", "22000000000": "Apparel & Accessories",
               "28000000000": "Sports & Outdoor", "19000000000": "Pets", "15000000000": "Tech & Electronics", "29000000000": "Health"}
NGANH_MAC_DINH = ["18000000000", "16000000000", "14000000000", "21000000000"]
_goi_y_chay = threading.Event()


def _goi_y_ai(ads: List[Dict], da_co: List[str]) -> List[Dict]:
    gon = [re.sub(r"\s+", " ", a.get("chu") or "")[:220] for a in ads]
    ds = "\n".join(f"[{i}] ({a.get('nganh') or ''}) {gon[i]}" for i, a in enumerate(ads))
    de = ("Đây là chữ của các quảng cáo TikTok đang chạy mạnh ở Việt Nam (đánh số).\n" + ds + "\n\n"
          "Gom thành tối đa 15 cụm từ khoá để tìm SẢN PHẨM trên Shopee (2-5 chữ tiếng Việt, kiểu người mua gõ, ví dụ "
          "\"máy hút bụi cầm tay\", \"kem chống nắng da dầu\"). Chỉ hàng vật lý mua được trên Shopee: bỏ app, game, dịch vụ, khoá học, "
          "tài chính, nhà hàng. Không gom tên thương hiệu riêng thành từ khoá. Mỗi cụm kèm số thứ tự các quảng cáo nói về loại hàng đó.\n"
          + (f"Bỏ các cụm đã có: {', '.join(da_co)}.\n" if da_co else "")
          + 'Trả JSON: {"tu_khoa": [{"tu": "...", "qc": [0, 3]}]}')
    got = pc.hoi_ai("Bạn gom quảng cáo thành từ khoá tìm sản phẩm Shopee. Chỉ trả JSON.", de, max_tokens=2500)
    ra, thay = [], set(x.lower() for x in da_co)
    for x in (got or {}).get("tu_khoa") or []:
        tu = re.sub(r"\s+", " ", str((x or {}).get("tu") or "")).strip()[:60]
        qc = sorted({int(i) for i in (x or {}).get("qc") or [] if isinstance(i, int) and 0 <= i < len(ads)})
        if not tu or not 2 <= len(tu.split()) <= 6 or tu.lower() in thay or not qc:
            continue
        thay.add(tu.lower())
        nganh = sorted({ads[i].get("nganh") or "" for i in qc} - {""})
        ra.append({"tu": tu, "so_qc": len(qc), "nganh": nganh, "vi_du": [(ads[i].get("chu") or "")[:120] for i in qc[:2]],
                   "xem": [ads[i].get("xem") for i in qc[:3] if ads[i].get("xem")]})
    ra.sort(key=lambda x: -x["so_qc"])
    return ra[:15]


def goi_y(nganh: List[str], lay=None) -> Dict:
    """Chạy nền: lấy Top Ads từng ngành, AI gom từ khoá, ghi vào tu_khoa.json mục "goi_y". `lay` để bài kiểm thay nguồn."""
    if lay is None:
        import nguon_quang_cao as nqc
        lay = lambda ma: nqc.lay_tiktok_top("VN", ma, so_luong=40)  # noqa: E731
    nganh = [x for x in nganh if x in NGANH_GOI_Y][:6] or list(NGANH_MAC_DINH)
    ads, loi = [], []
    for ma in nganh:
        try:
            ads += [{**a, "nganh": a.get("nganh") or NGANH_GOI_Y[ma]} for a in lay(ma) if (a.get("chu") or "").strip()]
        except Exception as e:  # noqa: BLE001 - một ngành hỏng thì vẫn dùng ngành khác
            loi.append(f"{NGANH_GOI_Y[ma]}: {str(e)[:120]}")
    ds = []
    if ads:
        ds = _goi_y_ai(ads[:160], doc().get("tu_khoa") or [])
    ket = {"luc": time.time(), "nganh": [NGANH_GOI_Y[x] for x in nganh], "so_qc": len(ads), "ds": ds,
           "loi": "; ".join(loi) if loi or ads else "Không lấy được quảng cáo nào từ TikTok Creative Center."}
    with _khoa:
        d = doc()
        d["goi_y"] = ket
        _ghi(d)
    return ket


def bat_dau_goi_y(nganh: List[str]) -> bool:
    if _goi_y_chay.is_set():
        return False
    _goi_y_chay.set()

    def chay():
        try:
            goi_y(nganh)
        except Exception as e:  # noqa: BLE001
            with _khoa:
                d = doc()
                d["goi_y"] = {"luc": time.time(), "ds": [], "loi": f"Hỏng: {str(e)[:200]}", "nganh": [], "so_qc": 0}
                _ghi(d)
        finally:
            _goi_y_chay.clear()
    threading.Thread(target=chay, daemon=True).start()
    return True


def _cong_khai(d: Dict) -> Dict:
    return {k: d.get(k) for k in ("bat", "tu_khoa", "gio", "so_video", "giam_toi_thieu", "hoa_hong_toi_thieu", "ngay_chay", "chay_ngay",
                                  "dang", "lan_hoi", "nhat_ky", "goi_y")} | {"so_da_lam": len(d.get("da_lam") or []), "bay_gio": time.time(),
                                  "dang_goi_y": _goi_y_chay.is_set(), "nganh_goi_y": NGANH_GOI_Y, "nganh_mac_dinh": NGANH_MAC_DINH}


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _chi_may_nay() -> bool:
    """Tiện ích và trang tool chạy trên máy chạy tool; máy khác cùng WiFi không được bấm thay."""
    return request.remote_addr in ("127.0.0.1", "::1")


def _loi(e, ma=400):
    return jsonify({"ok": False, "error": str(e)}), ma


@bp.route("/api/shopee_tu_khoa", methods=["GET"])
def api_xem():
    return jsonify({"ok": True, **_cong_khai(doc())})


@bp.route("/api/shopee_tu_khoa/luu", methods=["POST"])
def api_luu():
    if not _chi_may_nay():
        return _loi("Chỉ máy chạy tool đổi được lịch từ khoá.", 403)
    try:
        return jsonify({"ok": True, **_cong_khai(luu(request.get_json(silent=True) or {}))})
    except (ValueError, TypeError) as e:
        return _loi(e)


@bp.route("/api/shopee_tu_khoa/chay_ngay", methods=["POST"])
def api_chay_ngay():
    if not _chi_may_nay():
        return _loi("Chỉ máy chạy tool bấm được.", 403)
    try:
        return jsonify({"ok": True, **_cong_khai(chay_ngay())})
    except ValueError as e:
        return _loi(e)


@bp.route("/api/shopee_tu_khoa/goi_y", methods=["POST"])
def api_goi_y():
    if not _chi_may_nay():
        return _loi("Chỉ máy chạy tool bấm được.", 403)
    nganh = [str(x) for x in (request.get_json(silent=True) or {}).get("nganh") or []]
    return jsonify({"ok": True, "bat_dau": bat_dau_goi_y(nganh)})


@bp.route("/api/shopee_tu_khoa/viec", methods=["GET"])
def api_viec():
    if not _chi_may_nay():
        return _loi("Không cho phép.", 403)
    try:   # comment lại link sách dưới video YouTube hẹn giờ đã lên sóng (aff_youtube.thu_lai, tự giãn 2 phút một lần)
        import aff_youtube
        aff_youtube.thu_lai()
    except Exception as e:  # noqa: BLE001
        print(f"[aff_youtube] comment lại hỏng: {e}")
    v = viec(ranh=request.args.get("ranh") == "1")
    if not v.get("lam") and request.args.get("ranh") == "1" and not doc().get("dang"):
        # Tiện ích rảnh, không có lượt từ khoá: hỏi việc gắn link sách YouTube (aff_youtube, 16/11/2026, mã "yt_...").
        try:
            import aff_youtube
            v = aff_youtube.viec_tien_ich() or v
        except Exception as e:  # noqa: BLE001
            print(f"[aff_youtube] chưa lấy được việc: {e}")
    return jsonify({"ok": True, **v})


@bp.route("/api/shopee_tu_khoa/chon", methods=["POST"])
def api_chon():
    if not _chi_may_nay():
        return _loi("Không cho phép.", 403)
    t = request.get_json(silent=True) or {}
    try:
        ma = str(t.get("ma") or "")
        if ma.startswith("yt_"):
            import aff_youtube
            return jsonify({"ok": True, **aff_youtube.chon_tien_ich(ma, t.get("ket_qua") or {})})
        return jsonify({"ok": True, **chon(ma, t.get("ket_qua") or {})})
    except ValueError as e:
        return _loi(e)


@bp.route("/api/shopee_tu_khoa/xong", methods=["POST"])
def api_xong():
    if not _chi_may_nay():
        return _loi("Không cho phép.", 403)
    t = request.get_json(silent=True) or {}
    try:
        ma = str(t.get("ma") or "")
        if ma.startswith("yt_"):
            import aff_youtube
            return jsonify({"ok": True, **aff_youtube.xong_tien_ich(ma, t.get("ds") or [], str(t.get("dung") or ""))})
        return jsonify({"ok": True, **xong(ma, t.get("ds") or [], str(t.get("dung") or ""))})
    except ValueError as e:
        return _loi(e)


@bp.route("/api/shopee_tu_khoa/loi", methods=["POST"])
def api_loi():
    if not _chi_may_nay():
        return _loi("Không cho phép.", 403)
    t = request.get_json(silent=True) or {}
    if str(t.get("ma") or "").startswith("yt_"):
        import aff_youtube
        aff_youtube.loi_tien_ich(str(t.get("ma")), str(t.get("loi") or ""))
    else:
        bao_loi(str(t.get("ma") or ""), str(t.get("loi") or ""))
    return jsonify({"ok": True})
