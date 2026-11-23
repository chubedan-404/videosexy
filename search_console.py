"""
SEARCH CONSOLE (11/11/2026): số liệu Google Search thật của trang web mình, qua Google Search Console API.

Khác SEO Studio: SEO Studio đoán từ bên ngoài (bò trang, gợi ý tìm kiếm, KHÔNG có lượng tìm). Màn này đọc số Google
ghi lại cho chính trang của bạn: mỗi từ khoá được hiện bao nhiêu lần, bao nhiêu lượt bấm, đứng hạng mấy. Chỉ xem được
trang mà tài khoản Google đã đăng nhập là chủ (hoặc được cấp quyền) trong Search Console.

API dùng (searchconsole v1):
- sites.list: các trang tài khoản có quyền.
- searchanalytics.query: lượt hiện, lượt bấm, CTR, vị trí theo ngày / từ khoá / trang / nước / thiết bị.
  Số liệu trễ khoảng 2-3 ngày; tool lấy tới 3 ngày trước để không lẫn số chưa chốt.
- sitemaps.list / sitemaps.submit: xem và gửi sitemap.
- urlInspection.index.inspect: một URL đã vào chỉ mục chưa, lần bò cuối, canonical Google chọn (2000 lượt/ngày/trang).

Đăng nhập: OAuth bằng file giấy phép của dự án Google `tutien-v3` có sẵn trong thư mục tool (dùng chung file với
YouTube nhưng quyền lưu RIÊNG ở downloads/search_console/token.json, không đụng quyền của kênh YouTube). Dự án đó phải
bật "Google Search Console API" trong Google Cloud; chưa bật thì Google trả 403 và tool nói rõ.

Cơ hội (do CODE tính, AI chỉ viết lời khuyên từ các danh sách này, không tự thêm số):
- gan_top: từ khoá đứng hạng 4-15 mà lượt hiện cao, đẩy lên top 3 là ra lượt bấm.
- ctr_thap: từ khoá có tỉ lệ bấm thấp hơn một nửa mức TRUNG VỊ của chính trang ở cùng hạng (không dùng bảng CTR
  chung trên mạng): tiêu đề / mô tả chưa hút.
- tut: trang mất lượt bấm so với kỳ trước.
- moi: từ khoá mới có lượt hiện mà kỳ trước không có.
- tranh_nhau: một từ khoá có từ hai trang của mình cùng hiện (tự giành chỗ của nhau).

Máy chủ `/api/gsc/*`, trang `static/search_console.js` + `.css`, bài kiểm `tests/test_search_console.py`.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import statistics
import threading
import time
from typing import Callable, Dict, List, Optional

from flask import Blueprint, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "search_console")
FILE_TOKEN = os.path.join(KHO, "token.json")
DU_AN = "tutien-v3"
SCOPES = ["https://www.googleapis.com/auth/webmasters"]
SO_NGAY = (7, 28, 90)
TRE_NGAY = 3
SO_DONG = 1000

lay_cai_dat_ai: Callable[[], Dict] = lambda: {}
bp = Blueprint("search_console", __name__)
_PHIEN: Dict = {}
_KHOA = threading.Lock()


def _doc(p: str) -> Dict:
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _ghi(p: str, d) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tam = p + ".tmp"
    with open(tam, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tam, p)


# ─── Đăng nhập ───────────────────────────────────────────────────────────────

def giay_phep() -> str:
    """File client secret của dự án tutien-v3; không có thì file nào là bản Desktop."""
    ds = sorted(glob.glob(os.path.join(BASE_DIR, "client_secret_*.json")))
    for f in ds:
        if str((_doc(f).get("installed") or {}).get("project_id") or "") == DU_AN:
            return f
    return next((f for f in ds if _doc(f).get("installed")), "")


def _quyen():
    if not os.path.isfile(FILE_TOKEN):
        return None
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    q = Credentials.from_authorized_user_file(FILE_TOKEN, SCOPES)
    if q and q.expired and q.refresh_token:
        q.refresh(Request())
        _ghi(FILE_TOKEN, json.loads(q.to_json()))
    return q if q and q.valid else None


def _dv():
    q = _quyen()
    if not q:
        raise LoiGsc("Chưa đăng nhập Google cho Search Console.")
    from googleapiclient.discovery import build
    return build("searchconsole", "v1", credentials=q, cache_discovery=False)


class LoiGsc(Exception):
    pass


def doc_loi(exc: Exception) -> str:
    """Câu báo dễ hiểu cho các lỗi hay gặp."""
    s = str(exc)
    if "has not been used" in s or "is disabled" in s or "SERVICE_DISABLED" in s:
        return ("Dự án Google tutien-v3 chưa bật Google Search Console API. Mở link Google Cloud của API này, "
                "bấm Enable (Bật), chờ 1-2 phút rồi thử lại.")
    if "insufficient" in s.lower() or "User does not have sufficient permission" in s:
        return "Tài khoản Google này không có quyền với trang đó trong Search Console."
    if "invalid_grant" in s:
        return "Quyền đăng nhập đã hết hạn. Bấm Đăng nhập lại."
    return s[-400:]


def bat_dau_dang_nhap() -> Dict:
    """Mở luồng OAuth, trả link để người dùng tự bấm cấp quyền (tool không gõ mật khẩu)."""
    global _PHIEN
    giay = giay_phep()
    if not giay:
        return {"ok": False, "error": "Không thấy file giấy phép Google (client_secret_*.json) trong thư mục tool."}
    from google_auth_oauthlib.flow import InstalledAppFlow, _RedirectWSGIApp, _WSGIRequestHandler
    import wsgiref.simple_server
    if _PHIEN.get("server"):
        try:
            _PHIEN["server"].server_close()
        except Exception:  # noqa: BLE001
            pass
    app = _RedirectWSGIApp("Đã cấp quyền Search Console. Đóng tab này và quay lại tool.")
    may = wsgiref.simple_server.make_server("localhost", 0, app, handler_class=_WSGIRequestHandler)
    flow = InstalledAppFlow.from_client_secrets_file(giay, SCOPES)
    flow.redirect_uri = f"http://localhost:{may.server_port}/"
    link, _ = flow.authorization_url(prompt="select_account consent", access_type="offline")
    _PHIEN = {"server": may, "done": False, "error": "", "link": link, "luc": time.time()}

    def chay():
        try:
            may.timeout = 2
            while not app.last_request_uri and not _PHIEN.get("huy") and time.time() - _PHIEN["luc"] < 1800:
                may.handle_request()
            if not app.last_request_uri:
                _PHIEN["error"] = "Hết thời gian chờ cấp quyền."
                return
            flow.fetch_token(authorization_response=app.last_request_uri.replace("http", "https"))
            _ghi(FILE_TOKEN, json.loads(flow.credentials.to_json()))
        except Exception as exc:  # noqa: BLE001
            _PHIEN["error"] = doc_loi(exc)
        finally:
            _PHIEN["done"] = True
            try:
                may.server_close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=chay, daemon=True).start()
    return {"ok": True, "link": link}


def tinh_hinh() -> Dict:
    p = _PHIEN
    dang_cho = bool(p.get("server")) and not p.get("done")
    try:
        co = bool(_quyen())
    except Exception:  # noqa: BLE001
        co = False
    return {"ok": True, "da_noi": co, "dang_cho": dang_cho, "link": p.get("link") if dang_cho else "",
            "loi": p.get("error") if p.get("done") else "", "giay_phep": os.path.basename(giay_phep()),
            "du_an": DU_AN}


def dang_xuat() -> Dict:
    try:
        os.remove(FILE_TOKEN)
    except OSError:
        pass
    return {"ok": True}


# ─── Đọc số ──────────────────────────────────────────────────────────────────

def ds_trang() -> List[Dict]:
    r = _dv().sites().list().execute()
    ra = [{"site": s["siteUrl"], "quyen": s.get("permissionLevel", "")} for s in r.get("siteEntry") or []
          if s.get("permissionLevel") != "siteUnverifiedUser"]
    return sorted(ra, key=lambda x: x["site"])


def ky(so_ngay: int, hom_nay: Optional[dt.date] = None) -> Dict:
    """Kỳ này và kỳ trước cùng độ dài, lùi TRE_NGAY ngày vì số gần đây chưa chốt."""
    hom_nay = hom_nay or dt.date.today()
    het = hom_nay - dt.timedelta(days=TRE_NGAY)
    dau = het - dt.timedelta(days=so_ngay - 1)
    het_truoc = dau - dt.timedelta(days=1)
    dau_truoc = het_truoc - dt.timedelta(days=so_ngay - 1)
    return {"dau": dau.isoformat(), "het": het.isoformat(),
            "dau_truoc": dau_truoc.isoformat(), "het_truoc": het_truoc.isoformat()}


def _hoi(dv, site: str, dau: str, het: str, chieu: List[str], so_dong: int = SO_DONG) -> List[Dict]:
    body = {"startDate": dau, "endDate": het, "dimensions": chieu, "rowLimit": so_dong, "type": "web"}
    r = dv.searchanalytics().query(siteUrl=site, body=body).execute()
    ra = []
    for d in r.get("rows") or []:
        x = {"clicks": int(d.get("clicks", 0)), "impressions": int(d.get("impressions", 0)),
             "ctr": float(d.get("ctr", 0.0)), "position": float(d.get("position", 0.0))}
        for k, v in zip(chieu, d.get("keys") or []):
            x[k] = v
        ra.append(x)
    return ra


def tong(dong: List[Dict]) -> Dict:
    c = sum(d["clicks"] for d in dong)
    i = sum(d["impressions"] for d in dong)
    vt = (sum(d["position"] * d["impressions"] for d in dong) / i) if i else 0.0
    return {"clicks": c, "impressions": i, "ctr": (c / i) if i else 0.0, "position": round(vt, 1)}


# ─── Cơ hội, do code tính ────────────────────────────────────────────────────

def gan_top(tu: List[Dict], toi_da: int = 20) -> List[Dict]:
    ra = [d for d in tu if 4 <= d["position"] <= 15 and d["impressions"] >= 10]
    return sorted(ra, key=lambda d: -d["impressions"])[:toi_da]


def ctr_thap(tu: List[Dict], toi_da: int = 20) -> List[Dict]:
    """Thấp hơn một nửa trung vị CTR của chính trang ở cùng hạng (làm tròn). Nhóm hạng dưới 5 từ khoá thì bỏ."""
    nhom: Dict[int, List[float]] = {}
    for d in tu:
        if d["impressions"] >= 20 and d["position"] <= 10:
            nhom.setdefault(round(d["position"]), []).append(d["ctr"])
    trung_vi = {h: statistics.median(v) for h, v in nhom.items() if len(v) >= 5}
    ra = []
    for d in tu:
        h = round(d["position"])
        if d["impressions"] >= 50 and h in trung_vi and trung_vi[h] > 0 and d["ctr"] < trung_vi[h] / 2:
            ra.append({**d, "ctr_trung_vi": trung_vi[h]})
    return sorted(ra, key=lambda d: -d["impressions"])[:toi_da]


def tut(nay: List[Dict], truoc: List[Dict], khoa: str, toi_da: int = 20) -> List[Dict]:
    cu = {d[khoa]: d for d in truoc}
    ra = []
    for k, d in cu.items():
        moi = next((x for x in nay if x[khoa] == k), None)
        c_moi = moi["clicks"] if moi else 0
        mat = d["clicks"] - c_moi
        if d["clicks"] >= 5 and mat >= 3 and c_moi <= d["clicks"] * 0.7:
            ra.append({khoa: k, "clicks_truoc": d["clicks"], "clicks": c_moi, "mat": mat,
                       "position_truoc": round(d["position"], 1),
                       "position": round(moi["position"], 1) if moi else None})
    return sorted(ra, key=lambda d: -d["mat"])[:toi_da]


def moi(nay: List[Dict], truoc: List[Dict], toi_da: int = 20) -> List[Dict]:
    cu = {d["query"] for d in truoc}
    ra = [d for d in nay if d["query"] not in cu and d["impressions"] >= 10]
    return sorted(ra, key=lambda d: -d["impressions"])[:toi_da]


def tranh_nhau(tu_trang: List[Dict], toi_da: int = 15) -> List[Dict]:
    gom: Dict[str, List[Dict]] = {}
    for d in tu_trang:
        if d["impressions"] >= 10:
            gom.setdefault(d["query"], []).append(d)
    ra = []
    for q, ds in gom.items():
        if len(ds) >= 2:
            ds = sorted(ds, key=lambda d: -d["impressions"])
            ra.append({"query": q, "impressions": sum(d["impressions"] for d in ds),
                       "trang": [{"page": d["page"], "impressions": d["impressions"],
                                  "position": round(d["position"], 1)} for d in ds[:4]]})
    return sorted(ra, key=lambda d: -d["impressions"])[:toi_da]


# ─── Báo cáo ─────────────────────────────────────────────────────────────────

def _ma(site: str) -> str:
    return hashlib.sha1(site.encode()).hexdigest()[:12]


def bao_cao(site: str, so_ngay: int = 28) -> Dict:
    so_ngay = so_ngay if so_ngay in SO_NGAY else 28
    dv = _dv()
    k = ky(so_ngay)
    ngay = _hoi(dv, site, k["dau"], k["het"], ["date"])
    ngay_truoc = _hoi(dv, site, k["dau_truoc"], k["het_truoc"], ["date"])
    tu = _hoi(dv, site, k["dau"], k["het"], ["query"])
    tu_truoc = _hoi(dv, site, k["dau_truoc"], k["het_truoc"], ["query"])
    trang = _hoi(dv, site, k["dau"], k["het"], ["page"])
    trang_truoc = _hoi(dv, site, k["dau_truoc"], k["het_truoc"], ["page"])
    tu_trang = _hoi(dv, site, k["dau"], k["het"], ["query", "page"], 5000)
    nuoc = _hoi(dv, site, k["dau"], k["het"], ["country"], 20)
    tb = _hoi(dv, site, k["dau"], k["het"], ["device"], 10)
    bc = {
        "site": site, "so_ngay": so_ngay, "ky": k, "luc": time.time(),
        "tong": tong(ngay), "tong_truoc": tong(ngay_truoc),
        "ngay": sorted(ngay, key=lambda d: d["date"]),
        "tu": sorted(tu, key=lambda d: -d["clicks"])[:200],
        "trang": sorted(trang, key=lambda d: -d["clicks"])[:200],
        "nuoc": nuoc, "thiet_bi": tb,
        "co_hoi": {"gan_top": gan_top(tu), "ctr_thap": ctr_thap(tu), "tut_trang": tut(trang, trang_truoc, "page"),
                   "tut_tu": tut(tu, tu_truoc, "query"), "moi": moi(tu, tu_truoc), "tranh_nhau": tranh_nhau(tu_trang)},
        "ai": None,
    }
    _ghi(os.path.join(KHO, f"bc_{_ma(site)}.json"), bc)
    return bc


AI_SYSTEM = """You are an SEO analyst writing for a Vietnamese small-business owner who is not technical.
You get REAL Google Search Console numbers for their website as JSON (totals, this period vs previous, and lists of
opportunities already computed by code). Write advice in plain Vietnamese.
Rules:
- Use ONLY numbers present in the JSON. Never invent search volume, rankings, or traffic estimates.
- If a list is empty, say there is nothing there; do not make up items.
- Every action must name the exact query or page it is about.
Return ONLY JSON: {"tom_tat": "2-3 câu", "viec": [{"uu_tien": 1-3, "lam_gi": "...", "vi_sao": "...", "doi_tuong": "query hoặc URL"}]}
Give at most 8 actions, most valuable first."""


def ai_khuyen(bc: Dict) -> Dict:
    import longform_studio as lf
    gon = {"site": bc["site"], "so_ngay": bc["so_ngay"], "tong": bc["tong"], "tong_truoc": bc["tong_truoc"],
           "top_tu": bc["tu"][:15], "top_trang": bc["trang"][:10], "co_hoi": bc["co_hoi"]}
    r = lf.ask_json(AI_SYSTEM, json.dumps(gon, ensure_ascii=False), lay_cai_dat_ai(), max_tokens=3000)
    if not r.get("success"):
        raise LoiGsc(r.get("error") or "AI không trả lời.")
    d = r.get("data") or {}
    ai = {"tom_tat": str(d.get("tom_tat") or ""), "viec": [v for v in (d.get("viec") or []) if isinstance(v, dict)][:8]}
    bc["ai"] = ai
    _ghi(os.path.join(KHO, f"bc_{_ma(bc['site'])}.json"), bc)
    return ai


def sitemaps(site: str) -> List[Dict]:
    r = _dv().sitemaps().list(siteUrl=site).execute()
    ra = []
    for s in r.get("sitemap") or []:
        nd = s.get("contents") or []
        ra.append({"path": s.get("path"), "lan_gui": s.get("lastSubmitted"), "lan_doc": s.get("lastDownloaded"),
                   "loi": int(s.get("errors") or 0), "canh_bao": int(s.get("warnings") or 0),
                   "dang_cho": bool(s.get("isPending")),
                   "so_url": sum(int(c.get("submitted") or 0) for c in nd)})
    return ra


def gui_sitemap(site: str, url: str) -> None:
    _dv().sitemaps().submit(siteUrl=site, feedpath=url).execute()


def kiem_url(site: str, url: str) -> Dict:
    r = _dv().urlInspection().index().inspect(body={"inspectionUrl": url, "siteUrl": site,
                                                    "languageCode": "vi"}).execute()
    x = (r.get("inspectionResult") or {})
    i = x.get("indexStatusResult") or {}
    m = x.get("mobileUsabilityResult") or {}
    return {"url": url, "ket_luan": i.get("verdict"), "trang_thai": i.get("coverageState"),
            "lan_bo": i.get("lastCrawlTime"), "robots": i.get("robotsTxtState"),
            "cho_index": i.get("indexingState"), "canonical_google": i.get("googleCanonical"),
            "canonical_trang": i.get("userCanonical"), "tim_thay_tu": (i.get("referringUrls") or [])[:5],
            "di_dong": m.get("verdict"), "link": x.get("inspectionResultLink")}


# ─── Đường vào ───────────────────────────────────────────────────────────────

def _boc(f):
    try:
        return jsonify({"ok": True, **f()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": doc_loi(exc)}), 400


@bp.get("/api/gsc/tinh_hinh")
def _r_tinh_hinh():
    return jsonify(tinh_hinh())


@bp.post("/api/gsc/dang_nhap")
def _r_dang_nhap():
    r = bat_dau_dang_nhap()
    return jsonify(r) if r["ok"] else (jsonify(r), 400)


@bp.post("/api/gsc/dang_xuat")
def _r_dang_xuat():
    return jsonify(dang_xuat())


@bp.get("/api/gsc/trang")
def _r_trang():
    return _boc(lambda: {"trang": ds_trang()})


@bp.post("/api/gsc/bao_cao")
def _r_bao_cao():
    d = request.json or {}
    site = str(d.get("site") or "")
    if not site:
        return jsonify({"ok": False, "error": "Chưa chọn trang."}), 400
    with _KHOA:
        return _boc(lambda: {"bc": bao_cao(site, int(d.get("so_ngay") or 28))})


@bp.get("/api/gsc/bao_cao")
def _r_bao_cao_cu():
    site = request.args.get("site") or ""
    bc = _doc(os.path.join(KHO, f"bc_{_ma(site)}.json")) if site else {}
    return jsonify({"ok": True, "bc": bc or None})


@bp.post("/api/gsc/ai")
def _r_ai():
    site = str((request.json or {}).get("site") or "")
    bc = _doc(os.path.join(KHO, f"bc_{_ma(site)}.json"))
    if not bc:
        return jsonify({"ok": False, "error": "Chưa có báo cáo cho trang này."}), 400
    return _boc(lambda: {"ai": ai_khuyen(bc)})


@bp.get("/api/gsc/sitemap")
def _r_sitemap():
    return _boc(lambda: {"sitemap": sitemaps(request.args.get("site") or "")})


@bp.post("/api/gsc/sitemap")
def _r_gui_sitemap():
    d = request.json or {}
    url = str(d.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"ok": False, "error": "Link sitemap phải bắt đầu bằng http."}), 400
    return _boc(lambda: (gui_sitemap(str(d.get("site") or ""), url), {"sitemap": sitemaps(str(d.get("site") or ""))})[1])


@bp.post("/api/gsc/kiem_url")
def _r_kiem_url():
    d = request.json or {}
    url = str(d.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"ok": False, "error": "Nhập đầy đủ link, bắt đầu bằng http."}), 400
    return _boc(lambda: {"kq": kiem_url(str(d.get("site") or ""), url)})
