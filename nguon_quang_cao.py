"""
NGUỒN QUẢNG CÁO VÀ KÊNH NGOÀI META (21/10/2026), dùng cho Ad Research và Competitor 360.

Cả ba nguồn đều mở bằng Chrome ẩn KHÔNG đăng nhập, không cookie của người dùng, rồi nghe phản hồi JSON của chính trang:
- Google: Trung tâm minh bạch quảng cáo (adstransparency.google.com), tìm theo TÊN MIỀN. Trang gọi RPC
  SearchService/SearchCreatives, kết quả là JSON đánh số ("1" mã nhà quảng cáo, "2" mã quảng cáo, "4" định dạng 1 chữ /
  2 ảnh / 3 video, "6"."1" lần đầu hiển thị, "7"."1" lần gần nhất, "12" tên nhà quảng cáo, "14" tên miền, "3"."3"."2" thẻ
  <img> ảnh chụp quảng cáo nếu có). Chữ quảng cáo nằm trong file xem trước content.js ("3"."1"."4") của CHÍNH quảng cáo đó,
  ở khối `request:[[{...}]]` (tiêu đề, đường dẫn hiển thị, mô tả, liên kết phụ), khoảng 2 giây một file. KHÔNG đọc trang
  chi tiết: trang đó tải kèm quảng cáo khác của cùng nhà quảng cáo nên chữ bị lẫn (thử 21/10/2026). Tìm theo tên miền ra cả quảng
  cáo của NGƯỜI KHÁC chạy trên tên miền đó (đo 21/10/2026: shopee.vn ra cả một cá nhân), ghi rõ ở trường "trang".
- TikTok Creative Center Top Ads: quảng cáo hiệu quả nhất theo nước và ngành. Không đăng nhập thì: mỗi lần gọi tối đa 20
  (limit 50 bị từ chối), không có trang 2, không tìm theo từ khoá. Tool xoay thời gian (7/30/180 ngày) và cách xếp (dành
  cho bạn, CTR, lượt thích, lượt hiển thị) để gom thêm. Gọi lại API bằng fetch trong chính trang (mang header ký sẵn).
- Kênh TikTok: trang @tên trả /api/post/item_list (video, lượt xem, thích, bình luận, chia sẻ, lưu).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
NGANH_CACHE = os.path.join(BASE_DIR, "downloads", "ad_research", "nganh_tiktok.json")
DINH_DANG_GOOGLE = {1: "text", 2: "image", 3: "video"}


def _trinh_duyet(p):
    tr = p.chromium.launch(channel="chrome", headless=True, args=["--disable-blink-features=AutomationControlled"])
    ctx = tr.new_context(locale="vi-VN", user_agent=UA, viewport={"width": 1280, "height": 900})
    return tr, ctx


def ten_mien(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0].removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", s):
        raise ValueError("Nhập tên miền trang web, ví dụ shopee.vn.")
    return s


# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE ADS TRANSPARENCY
# ─────────────────────────────────────────────────────────────────────────────

def chuan_google(a: Dict, bay_gio: Optional[float] = None) -> Dict:
    bay_gio = bay_gio or time.time()
    bd = int(((a.get("6") or {}).get("1")) or 0)
    kt = int(((a.get("7") or {}).get("1")) or 0)
    ar, cr = str(a.get("1") or ""), str(a.get("2") or "")
    return {"id": cr, "nguon": "google", "trang": str(a.get("12") or ""), "trang_url": str(a.get("14") or ""),
            "anh_trang": "", "luot_thich_trang": 0, "bat_dau": bd,
            "ngay_chay": max(1, int(((kt or bay_gio) - bd) // 86400) + 1) if bd else 0,
            "dang_chay": bool(kt) and bay_gio - kt < 3 * 86400, "nen_tang": ["google"], "so_ban": 1,
            "dinh_dang": DINH_DANG_GOOGLE.get(a.get("4"), "khac"), "chu": "", "tieu_de": "", "mo_ta": "", "cta": "", "link": "",
            "anh": (re.search(r'src="([^"]+)"', str(((a.get("3") or {}).get("3") or {}).get("2") or "")) or [None, ""])[1],
            "video": "", "anh_video": "", "_xem_truoc": str(((a.get("3") or {}).get("1") or {}).get("4") or ""),
            "xem": f"https://adstransparency.google.com/advertiser/{ar}/creative/{cr}?region=anywhere", "ma_nqc": ar}


def chu_content_js(js: str) -> str:
    """Chữ quảng cáo (tiêu đề, mô tả, liên kết phụ) trong file xem trước content.js của Google Ads Transparency."""
    i = js.find("request:[[{")
    if i < 0:
        return ""
    doan = js[i:i + 8000]
    for x, y in ((r"\x22", '"'), (r"\x27", "'"), (r"\x26quot;", '"'), (r"\x26amp;", "&"), (r"\x26#39;", "'"), (r"\x3d", "=")):
        doan = doan.replace(x, y)
    ra: List[str] = []
    # {0,400}: chuỗi ngắn ("31") vẫn phải khớp, kẻo dấu ngoặc kép lệch cặp và nuốt mất chuỗi sau.
    for x in re.findall(r'"([^"]{0,400})"', doan):
        x = x.strip()
        if len(x) < 3 or re.match(r"^(https?:|www\.|[\d\s.,:]+$)", x) or re.search(r"[{}<>=\\]", x) or ("." in x and " " not in x):
            continue
        if len(re.findall(r"[A-Za-zÀ-ỹ]{2,}", x)) < 2 or x in ra:
            continue
        ra.append(x)
    return " / ".join(ra[:14])[:1500]


def _doc_chu_google(url: str) -> str:
    import requests
    try:
        return chu_content_js(requests.get(url, timeout=40, headers={"User-Agent": UA}).text)
    except Exception:  # noqa: BLE001
        return ""


def lay_google(ten_mien_: str, so_luong: int = 40, quoc_gia: str = "VN", doc_chu: int = 10, bao=None, huy=None) -> List[Dict]:
    from playwright.sync_api import sync_playwright
    tm = ten_mien(ten_mien_)
    vung = quoc_gia if re.fullmatch(r"[A-Z]{2}", quoc_gia or "") else "anywhere"
    tho: Dict[str, Dict] = {}

    def nghe(r):
        if "SearchService/SearchCreatives" in r.url:
            try:
                for a in json.loads(r.text()).get("1") or []:
                    if isinstance(a, dict) and a.get("2"):
                        tho.setdefault(str(a["2"]), a)
            except Exception:  # noqa: BLE001
                pass
    with sync_playwright() as p:
        tr, ctx = _trinh_duyet(p)
        try:
            pg = ctx.new_page()
            pg.on("response", nghe)
            pg.goto(f"https://adstransparency.google.com/?region={vung}&domain={tm}", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(7000)
            dung = 0
            while len(tho) < so_luong and dung < 3 and not (huy and huy()):
                truoc = len(tho)
                pg.mouse.wheel(0, 8000)
                pg.wait_for_timeout(2500)
                dung = dung + 1 if len(tho) == truoc else 0
                if bao:
                    bao(len(tho), "")
            bay_gio = time.time()
            ads = [chuan_google(a, bay_gio) for a in tho.values()]
            for a in ads:
                a["xem"] = a["xem"].replace("region=anywhere", f"region={vung}")
            ads.sort(key=lambda a: -a["ngay_chay"])
            ads = ads[:so_luong]
        finally:
            tr.close()
    # Chữ quảng cáo: đọc file xem trước của các quảng cáo chạy lâu nhất, 4 luồng.
    from concurrent.futures import ThreadPoolExecutor
    can = [a for a in ads[:doc_chu] if a.get("_xem_truoc")]
    if can and bao:
        bao(len(ads), f"Đang đọc chữ {len(can)} quảng cáo Google")
    with ThreadPoolExecutor(max_workers=4) as ex:
        for a, chu in zip(can, ex.map(lambda a: _doc_chu_google(a["_xem_truoc"]), can)):
            a["chu"] = chu
    for a in ads:
        a.pop("_xem_truoc", None)
    return ads


# ─────────────────────────────────────────────────────────────────────────────
# TIKTOK CREATIVE CENTER TOP ADS
# ─────────────────────────────────────────────────────────────────────────────

_FETCH = """async ([q, h]) => { const r = await fetch('https://ads.tiktok.com/creative_radar_api/v1/' + q,
  {headers: h, credentials: 'include'}); return await r.text(); }"""


def _mo_creative_center(p, quoc_gia: str):
    """Mở trang Top Ads, bắt header ký sẵn (user-sign, timestamp...) của lần gọi đầu để gọi lại API."""
    tr, ctx = _trinh_duyet(p)
    pg = ctx.new_page()
    hdr: Dict = {}
    pg.on("request", lambda req: hdr.update(req.headers) if "creative_radar_api/v1/top_ads/v2/list" in req.url and not hdr else None)
    pg.goto(f"https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en?period=30&region={quoc_gia}",
            wait_until="domcontentloaded", timeout=60000)
    for _ in range(20):
        if hdr:
            break
        pg.wait_for_timeout(500)
    h = {k: v for k, v in hdr.items() if k.lower() in ("anonymous-user-id", "timestamp", "user-sign", "lang", "web-id")}
    if not h:
        tr.close()
        raise RuntimeError("Không mở được TikTok Creative Center lúc này. Thử lại sau ít phút.")
    return tr, pg, h


def nganh_tiktok(lam_moi: bool = False) -> List[Dict]:
    """Danh sách ngành của Top Ads (258 ngành, có ngành cha). Lưu đệm vào downloads/ad_research/nganh_tiktok.json."""
    if not lam_moi and os.path.isfile(NGANH_CACHE):
        try:
            with open(NGANH_CACHE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        tr, pg, h = _mo_creative_center(p, "VN")
        try:
            d = json.loads(pg.evaluate(_FETCH, ["top_ads/v2/filters", h]))
        finally:
            tr.close()
    ds = [{"id": str(i["id"]), "ten": i["value"], "cha": str(i.get("parent_id") or "")} for i in (d.get("data") or {}).get("industry") or []]
    if ds:
        os.makedirs(os.path.dirname(NGANH_CACHE), exist_ok=True)
        with open(NGANH_CACHE, "w", encoding="utf-8") as f:
            json.dump(ds, f, ensure_ascii=False)
    return ds


def chuan_tiktok_top(m: Dict, ten_nganh: Optional[Dict] = None) -> Dict:
    v = m.get("video_info") or {}
    url = (v.get("video_url") or {})
    nk = str(m.get("industry_key") or "").replace("label_", "")
    return {"id": str(m.get("id")), "nguon": "tiktok", "trang": m.get("brand_name") or "", "trang_url": "", "anh_trang": "",
            "luot_thich_trang": 0, "bat_dau": 0, "ngay_chay": 0, "dang_chay": True, "nen_tang": ["tiktok"], "so_ban": 1,
            "dinh_dang": "video", "chu": str(m.get("ad_title") or "")[:1500], "tieu_de": "", "mo_ta": "", "cta": "", "link": "",
            "anh": v.get("cover") or "", "video": url.get("720p") or url.get("540p") or next(iter(url.values()), ""),
            "anh_video": v.get("cover") or "", "giay": round(float(v.get("duration") or 0), 1),
            "ctr": m.get("ctr"), "like": m.get("like"), "nganh": (ten_nganh or {}).get(nk, ""),
            "muc_tieu": str(m.get("objective_key") or "").replace("campaign_objective_", "").replace("_", " "),
            "xem": f"https://ads.tiktok.com/business/creativecenter/topads/{m.get('id')}/pc/en"}


def lay_tiktok_top(quoc_gia: str = "VN", nganh: str = "", so_luong: int = 60, bao=None, huy=None) -> List[Dict]:
    from playwright.sync_api import sync_playwright
    quoc_gia = quoc_gia if re.fullmatch(r"[A-Z]{2}", quoc_gia or "") else "VN"
    ten = {x["id"]: x["ten"] for x in (nganh_tiktok() if os.path.isfile(NGANH_CACHE) else [])}
    tho: Dict[str, Dict] = {}
    with sync_playwright() as p:
        tr, pg, h = _mo_creative_center(p, quoc_gia)
        try:
            for ky in (30, 7, 180):
                for xep in ("for_you", "ctr", "like", "impression"):
                    if len(tho) >= so_luong or (huy and huy()):
                        break
                    q = f"top_ads/v2/list?period={ky}&page=1&limit=20&order_by={xep}&country_code={quoc_gia}" + (f"&industry={nganh}" if nganh else "")
                    try:
                        d = json.loads(pg.evaluate(_FETCH, [q, h]))
                    except Exception:  # noqa: BLE001
                        continue
                    for m in (d.get("data") or {}).get("materials") or []:
                        if m.get("id"):
                            tho.setdefault(str(m["id"]), m)
                    if bao:
                        bao(len(tho), "")
                    pg.wait_for_timeout(600)
        finally:
            tr.close()
    ads = [chuan_tiktok_top(m, ten) for m in tho.values()]
    ads.sort(key=lambda a: -(a.get("ctr") or 0))
    return ads[:so_luong]


# ─────────────────────────────────────────────────────────────────────────────
# KÊNH TIKTOK
# ─────────────────────────────────────────────────────────────────────────────

def ten_kenh_tiktok(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", s)
    s = m.group(1) if m else s.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{2,30}", s):
        raise ValueError("Nhập tên kênh TikTok, ví dụ @shopee_vn.")
    return s


def lay_kenh_tiktok(ten: str, so_video: int = 30, bao=None) -> Dict:
    """Thông tin kênh và các video mới nhất (lượt xem, thích, bình luận, chia sẻ, lưu) của một kênh TikTok công khai."""
    from playwright.sync_api import sync_playwright
    ten = ten_kenh_tiktok(ten)
    video: Dict[str, Dict] = {}

    def nghe(r):
        if "/api/post/item_list" in r.url:
            try:
                for it in json.loads(r.text()).get("itemList") or []:
                    video.setdefault(str(it.get("id")), it)
            except Exception:  # noqa: BLE001
                pass
    with sync_playwright() as p:
        tr, ctx = _trinh_duyet(p)
        try:
            pg = ctx.new_page()
            pg.on("response", nghe)
            pg.goto(f"https://www.tiktok.com/@{ten}", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(7000)
            kenh = {}
            try:
                j = json.loads(pg.eval_on_selector("#__UNIVERSAL_DATA_FOR_REHYDRATION__", "e => e.textContent"))
                ui = j["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]
                u, st = ui.get("user") or {}, ui.get("stats") or {}
                kenh = {"ten": u.get("nickname") or ten, "ma": ten, "mo_ta": u.get("signature") or "", "anh": u.get("avatarMedium") or "",
                        "theo_doi": st.get("followerCount"), "thich": st.get("heartCount") or st.get("heart"), "so_video": st.get("videoCount")}
            except Exception:  # noqa: BLE001
                kenh = {"ten": ten, "ma": ten}
            dung = 0
            while len(video) < so_video and dung < 3:
                truoc = len(video)
                pg.mouse.wheel(0, 6000)
                pg.wait_for_timeout(2500)
                dung = dung + 1 if len(video) == truoc else 0
                if bao:
                    bao(len(video))
        finally:
            tr.close()
    if not video and not kenh.get("theo_doi"):
        raise RuntimeError(f"Không đọc được kênh TikTok @{ten} (kênh không có, riêng tư, hoặc TikTok đang chặn).")
    ds = []
    for it in video.values():
        st = it.get("stats") or {}
        ds.append({"id": str(it.get("id")), "chu": str(it.get("desc") or "")[:600], "luc": int(it.get("createTime") or 0),
                   "xem": int(st.get("playCount") or 0), "thich": int(st.get("diggCount") or 0), "binh_luan": int(st.get("commentCount") or 0),
                   "chia_se": int(st.get("shareCount") or 0), "luu": int(st.get("collectCount") or 0),
                   "giay": int((it.get("video") or {}).get("duration") or 0), "anh": (it.get("video") or {}).get("cover") or "",
                   "ghim": bool(it.get("isPinnedItem")), "link": f"https://www.tiktok.com/@{ten}/video/{it.get('id')}"})
    ds.sort(key=lambda v: -v["luc"])
    return {"kenh": kenh, "video": ds[:so_video]}
