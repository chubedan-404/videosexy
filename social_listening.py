"""
SOCIAL LISTENING (22/10/2026): nghe người ta nói gì về một thương hiệu (và tối đa 2 đối thủ).

Học cách tổ chức của Obsei (Apache 2.0) và Harken (MIT): nguồn -> phân loại -> gom chủ đề -> so lần trước -> báo động.
Không chép code của hai dự án đó; phần đọc nguồn dùng lại các bộ đọc đã có trong tool.

Nguồn (đo 22/10/2026):
- Báo chí: RSS Google News tìm theo cụm từ khoá trong N ngày (công khai, không khoá).
- YouTube: search.list tìm video nhắc từ khoá + commentThreads.list lấy bình luận, qua kênh YouTube đã nối
  (`youtube_upload._dich_vu`). Không nối thì yt-dlp tìm và đọc bình luận (chậm hơn).
- TikTok: KHÔNG tìm theo từ khoá được (không đăng nhập là gặp giải hình kéo thanh trượt). Chỉ nghe bình luận dưới các video
  của kênh TikTok thương hiệu: mở trang video, bấm nút bình luận, nghe /api/comment/list.
- Facebook: bình luận dưới các bài gần đây của fanpage, bằng phiên Facebook của người dùng (cookies.txt, như Content Check),
  đọc nút "Comment" trong GraphQL. Facebook chỉ cho khoảng 6-10 bình luận "liên quan nhất" mỗi bài. KHÔNG chụp màn hình.

Quyền riêng tư: chỉ đếm ý kiến về THƯƠNG HIỆU. Không lưu tên, ảnh, link trang của người bình luận; tên người được gắn thẻ
trong bình luận thay bằng "@…". Bình luận do chính fanpage trả lời thì bỏ.

AI: phân loại từng ý kiến (khen/chê/trung lập, ý định, chủ đề ngắn, có liên quan không), gom chủ đề thành nhóm (dùng lại
tên nhóm của lần trước để so được), rồi viết tổng kết. Báo động do CODE tính (so lần trước), không do AI.

Máy chủ: `/api/listen/*`. Trang: `static/social_listening.js` + `.css`. Bài kiểm: `tests/test_social_listening.py`.
Dữ liệu: `downloads/social_listening/<mã>.json` (một mục theo dõi, nhiều lần nghe). Việc nền tiền tố `sl_`.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import threading
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request

import longform_studio as lf
import social_research as sr

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "social_listening")
NGUON = ("bao", "youtube", "tiktok", "facebook")
TEN_NGUON = {"bao": "Báo chí", "youtube": "YouTube", "tiktok": "TikTok", "facebook": "Facebook"}
SO_NGAY = (7, 30, 90)
TOI_DA_TH = 3          # thương hiệu của bạn + 2 đối thủ
SO_BAI_BL = 6          # số video/bài mỗi kênh được đọc bình luận
GIU_LAN = 8            # số lần nghe giữ lại; lần cũ hơn lần kề trước chỉ giữ số liệu, bỏ từng ý kiến
CAM_XUC = ("khen", "che", "trung_lap")
Y_DINH = ("hoi", "muon_mua", "than_phien", "khen", "khac")

lay_cai_dat_ai: Callable[[], Dict] = lambda: {}
_khoa = threading.RLock()
bp = Blueprint("social_listening", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# KHO
# ─────────────────────────────────────────────────────────────────────────────

def _ma_hop_le(ma: str) -> bool:
    return bool(re.fullmatch(r"sl_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", ma or ""))


def _duong(ma: str) -> str:
    return os.path.join(KHO, ma + ".json")


def _doc(ma: str) -> Optional[Dict]:
    if not _ma_hop_le(ma):
        return None
    try:
        with open(_duong(ma), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _ghi(d: Dict) -> None:
    with _khoa:
        os.makedirs(KHO, exist_ok=True)
        tam = _duong(d["id"]) + ".tam"
        with open(tam, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tam, _duong(d["id"]))


def _sua(ma: str, **gia_tri) -> Dict:
    with _khoa:
        d = _doc(ma)
        if d is None:
            raise ValueError("Không có mục theo dõi này.")
        d.update(gia_tri)
        _ghi(d)
        return d


def _sua_lan(ma: str, **gia_tri) -> Dict:
    """Sửa lần nghe mới nhất."""
    with _khoa:
        d = _doc(ma)
        d["lan"][-1].update(gia_tri)
        _ghi(d)
        return d


def _sua_nguon(ma: str, khoa: str, **gia_tri) -> None:
    with _khoa:
        d = _doc(ma)
        d["lan"][-1]["nguon"].setdefault(khoa, {}).update(gia_tri)
        _ghi(d)


def danh_sach() -> List[Dict]:
    if not os.path.isdir(KHO):
        return []
    ra = [d for d in (_doc(f[:-5]) for f in os.listdir(KHO) if f.endswith(".json")) if d]
    ra.sort(key=lambda d: (d.get("lan") or [{}])[-1].get("luc") or d.get("tao_luc") or 0, reverse=True)
    return ra


def ban_gui(d: Dict, gon: bool = False) -> Dict:
    v = lf.get_job(d.get("job_id") or "") or {}
    chay = v.get("status") == "running"
    ra = {**d, "dang_chay": chay, "tien_do": v.get("progress") or 0, "dang_lam": v.get("step") or ""}
    lan = [dict(x) for x in d.get("lan") or []]
    if lan and not chay and lan[-1].get("buoc") == "chay":
        lan[-1]["buoc"], lan[-1]["loi"] = "loi", lan[-1].get("loi") or "Tool khởi động lại giữa chừng. Bấm Nghe lại."
    ra["lan"] = lan
    if gon:
        cuoi = lan[-1] if lan else {}
        ra["lan"] = []
        ra["cuoi"] = {"luc": cuoi.get("luc"), "buoc": cuoi.get("buoc"), "bao_dong": len(cuoi.get("bao_dong") or [])}
    return ra


# ─────────────────────────────────────────────────────────────────────────────
# CHỮ
# ─────────────────────────────────────────────────────────────────────────────

def bo_dau(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def co_tu_khoa(chu: str, tu_khoa: List[str]) -> bool:
    """Chữ có nhắc một trong các từ khoá không (không phân biệt dấu, hoa thường, dấu cách)."""
    goc = re.sub(r"\s+", "", bo_dau(chu))
    return any(re.sub(r"\s+", "", bo_dau(k)) in goc for k in tu_khoa if k.strip())


def che_ten(chu: str) -> str:
    """Bỏ tên người được gắn thẻ kiểu @ten trong bình luận TikTok, YouTube."""
    return re.sub(r"@[^\s@,.!?:;]+(?:\s[A-ZĐÀ-Ỹ][^\s@,.!?:;]*){0,3}", "@…", chu or "")


def bo_the_facebook(body: Dict) -> str:
    """Chữ bình luận Facebook, thay tên người được gắn thẻ (ranges có entity User) bằng "@…". Vị trí tính theo UTF-16."""
    chu = (body or {}).get("text") or ""
    the = []
    for r in (body or {}).get("ranges") or []:
        e = r.get("entity") or {}
        if e.get("__typename") in ("User", "Page") or e.get("__isActor") == "User":
            try:
                the.append((int(r["offset"]), int(r["length"])))
            except (KeyError, TypeError, ValueError):
                pass
    if not the:
        return chu
    b = chu.encode("utf-16-le")
    for o, n in sorted(the, reverse=True):
        b = b[:o * 2] + "@…".encode("utf-16-le") + b[(o + n) * 2:]
    return b.decode("utf-16-le", "ignore")


def ma_y_kien(nguon: str, goc: str) -> str:
    return nguon[:2] + hashlib.sha1(f"{nguon}:{goc}".encode()).hexdigest()[:12]


def _y(nguon, loai, goc, chu, luc, thich, link, bai="", th=0) -> Dict:
    return {"id": ma_y_kien(nguon, str(goc)), "th": th, "nguon": nguon, "loai": loai, "chu": (chu or "").strip()[:600],
            "luc": int(luc or 0), "thich": int(thich or 0), "link": link or "", "bai": (bai or "").strip()[:140]}


# ─────────────────────────────────────────────────────────────────────────────
# NGUỒN
# ─────────────────────────────────────────────────────────────────────────────

def doc_rss(xml: bytes) -> List[Dict]:
    ra = []
    for i in ET.fromstring(xml).findall(".//item"):
        import email.utils
        try:
            luc = int(email.utils.parsedate_to_datetime(i.findtext("pubDate") or "").timestamp())
        except (TypeError, ValueError):
            luc = 0
        tua = i.findtext("title") or ""
        nguon = i.findtext("source") or ""
        if nguon and tua.endswith(" - " + nguon):
            tua = tua[: -len(nguon) - 3]
        ra.append({"tua": tua, "nguon": nguon, "luc": luc, "link": i.findtext("link") or ""})
    return ra


def lay_bao(tu_khoa: List[str], so_ngay: int, toi_da: int = 40) -> List[Dict]:
    import requests
    ra: Dict[str, Dict] = {}
    for k in tu_khoa[:3]:
        q = quote(f'"{k}" when:{so_ngay}d')
        r = requests.get(f"https://news.google.com/rss/search?q={q}&hl=vi&gl=VN&ceid=VN:vi", timeout=25,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        for b in doc_rss(r.content):
            ra.setdefault(b["link"], b)
    ds = sorted(ra.values(), key=lambda b: -b["luc"])[:toi_da]
    return [_y("bao", "bai_bao", b["link"], b["tua"], b["luc"], 0, b["link"], b["nguon"]) for b in ds]


def _yt_dv():
    try:
        import youtube_upload as yu
        return yu._dich_vu(None)
    except Exception:  # noqa: BLE001
        return None


def _yt_tim_api(dv, k: str, sau: int, so: int) -> List[Dict]:
    r = dv.search().list(part="snippet", q=k, type="video", regionCode="VN", relevanceLanguage="vi", order="relevance",
                         publishedAfter=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sau)), maxResults=so).execute()
    # search.list trả tựa và mô tả còn mã HTML (&quot; &amp;).
    return [{"id": x["id"]["videoId"], "tua": html.unescape(x["snippet"].get("title") or ""), "mo_ta": html.unescape(x["snippet"].get("description") or ""),
             "kenh": html.unescape(x["snippet"].get("channelTitle") or ""), "luc": sr._luc_iso(x["snippet"].get("publishedAt"))}
            for x in r.get("items") or [] if (x.get("id") or {}).get("videoId")]


def _yt_bl_api(dv, vid: str, so: int) -> List[Dict]:
    try:
        r = dv.commentThreads().list(part="snippet", videoId=vid, maxResults=min(100, so), order="relevance",
                                     textFormat="plainText").execute()
    except Exception:  # noqa: BLE001 - video tắt bình luận
        return []
    ra = []
    for x in r.get("items") or []:
        s = ((x.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
        ra.append({"id": x.get("id"), "chu": s.get("textDisplay") or "", "thich": s.get("likeCount") or 0,
                   "luc": sr._luc_iso(s.get("publishedAt"))})
    return ra


def _yt_dlp_tim(k: str, so: int) -> List[Dict]:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}) as y:
        i = y.extract_info(f"ytsearchdate{so}:{k}", download=False) or {}
    return [{"id": e["id"], "tua": e.get("title") or "", "mo_ta": e.get("description") or "", "kenh": e.get("channel") or "",
             "luc": int(e.get("timestamp") or 0)} for e in i.get("entries") or [] if e and e.get("id")]


def _yt_dlp_bl(vid: str, so: int) -> List[Dict]:
    import yt_dlp
    o = {"quiet": True, "no_warnings": True, "skip_download": True, "getcomments": True,
         "extractor_args": {"youtube": {"max_comments": [str(so), str(so), "0", "0"], "comment_sort": ["top"],
                                        "player_client": ["android"]}}}
    try:
        with yt_dlp.YoutubeDL(o) as y:
            i = y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False) or {}
    except Exception:  # noqa: BLE001
        return []
    return [{"id": c.get("id"), "chu": c.get("text") or "", "thich": c.get("like_count") or 0, "luc": int(c.get("timestamp") or 0)}
            for c in i.get("comments") or [] if c.get("parent") in (None, "root")]


def lay_youtube(tu_khoa: List[str], so_ngay: int, so_video: int = SO_BAI_BL, bl_moi_video: int = 40, bao=None, huy=None) -> List[Dict]:
    sau = int(time.time()) - so_ngay * 86400
    dv = _yt_dv()
    video: Dict[str, Dict] = {}
    for k in tu_khoa[:2]:
        try:
            ds = _yt_tim_api(dv, k, sau, 20) if dv else _yt_dlp_tim(k, 20)
        except Exception:  # noqa: BLE001 - hết quota, quyền hỏng: chuyển yt-dlp
            dv = None
            ds = _yt_dlp_tim(k, 20)
        for v in ds:
            # Tìm theo độ liên quan hay ra video không hề nhắc tên: chỉ giữ video có từ khoá trong tựa, mô tả hay tên kênh.
            if co_tu_khoa(" ".join((v["tua"], v["mo_ta"], v["kenh"])), tu_khoa) and (not v["luc"] or v["luc"] >= sau):
                video.setdefault(v["id"], v)
    if not video:
        return []
    so = sr._yt_api(list(video)) if dv else {}
    for vid, x in so.items():
        video[vid]["binh_luan"], video[vid]["xem"] = x.get("binh_luan") or 0, x.get("xem") or 0
    ra = [_y("youtube", "video", v["id"], v["tua"] + ("\n" + v["mo_ta"][:200] if v["mo_ta"] else ""), v["luc"], 0,
             "https://www.youtube.com/watch?v=" + v["id"], v["kenh"]) for v in video.values()]
    chon = sorted(video.values(), key=lambda v: -(v.get("binh_luan") or 0))[:so_video]
    for i, v in enumerate(chon):
        if huy and huy():
            break
        if bao:
            bao(f"Đang đọc bình luận YouTube {i + 1}/{len(chon)}")
        bl = _yt_bl_api(dv, v["id"], bl_moi_video) if dv else _yt_dlp_bl(v["id"], bl_moi_video)
        ra += [_y("youtube", "binh_luan", c["id"] or c["chu"], che_ten(c["chu"]), c["luc"], c["thich"],
                  f"https://www.youtube.com/watch?v={v['id']}&lc={c['id']}" if c.get("id") else "https://www.youtube.com/watch?v=" + v["id"],
                  v["tua"]) for c in bl if c["chu"].strip()]
    return ra


def chon_bai(bai: List[Dict], so_ngay: int, so: int) -> List[Dict]:
    """Bài nhiều bình luận nhất trong khoảng thời gian; thiếu thì thêm bài mới nhất ngoài khoảng (bình luận vẫn có thể mới)."""
    sau = time.time() - so_ngay * 86400
    trong = sorted([b for b in bai if (b.get("luc") or 0) >= sau], key=lambda b: -b["binh_luan"])
    ngoai = sorted([b for b in bai if (b.get("luc") or 0) < sau], key=lambda b: -(b.get("luc") or 0))
    return (trong + ngoai)[:so]


def lay_tiktok(kenh: str, so_ngay: int, so_video: int = SO_BAI_BL, bao=None, huy=None) -> List[Dict]:
    """Bình luận dưới các video nhiều bình luận nhất trong 20 video gần nhất của kênh."""
    import nguon_quang_cao as nq
    from playwright.sync_api import sync_playwright
    kq = nq.lay_kenh_tiktok(kenh, 20)
    ds = chon_bai([v for v in kq["video"] if v["binh_luan"]], so_ngay, so_video)
    if not ds:
        return []
    bl: Dict[str, List] = {}

    def nghe(r):
        if "/api/comment/list" in r.url:
            try:
                for c in json.loads(r.text()).get("comments") or []:
                    bl.setdefault(str(c.get("aweme_id")), []).append(c)
            except Exception:  # noqa: BLE001
                pass
    ra: List[Dict] = []
    with sync_playwright() as p:
        tr, ctx = nq._trinh_duyet(p)
        try:
            pg = ctx.new_page()
            pg.on("response", nghe)
            for i, v in enumerate(ds):
                if huy and huy():
                    break
                if bao:
                    bao(f"Đang đọc bình luận TikTok {i + 1}/{len(ds)}")
                pg.goto(v["link"], wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(5000)
                nut = pg.query_selector("[data-e2e='comment-icon']")
                if nut:
                    try:
                        nut.click(timeout=4000)
                    except Exception:  # noqa: BLE001
                        pass
                pg.wait_for_timeout(3500)
                for _ in range(2):
                    pg.mouse.wheel(0, 3000)
                    pg.wait_for_timeout(1500)
        finally:
            tr.close()
    if not bl:
        raise RuntimeError("TikTok không trả bình luận (có thể đang chặn máy lạ). Thử lại sau.")
    tua = {v["id"]: v["chu"] for v in ds}
    ten = nq.ten_kenh_tiktok(kenh)
    for vid, cs in bl.items():
        for c in cs:
            if (c.get("user") or {}).get("unique_id") == ten:      # chính kênh trả lời
                continue
            ra.append(_y("tiktok", "binh_luan", c.get("cid") or c.get("text"), che_ten(c.get("text") or ""), c.get("create_time"),
                         c.get("digg_count"), f"https://www.tiktok.com/@{ten}/video/{vid}", tua.get(vid, "")))
    return [x for x in ra if x["chu"]]


def doc_bl_facebook(o, ra: Dict[str, Dict]) -> None:
    """Gom nút Comment trong một khối GraphQL."""
    if isinstance(o, dict):
        if o.get("__typename") == "Comment" and o.get("id") and isinstance(o.get("body"), dict):
            ra.setdefault(o["id"], o)
        for v in o.values():
            doc_bl_facebook(v, ra)
    elif isinstance(o, list):
        for v in o:
            doc_bl_facebook(v, ra)


def chuan_bl_facebook(c: Dict) -> Dict:
    fb = c.get("feedback") or {}
    dem = sr._dau(fb, "reactors") or {}
    thich = sr.so_rut_gon(str(dem.get("count_reduced") or dem.get("count") or 0)) or 0
    return {"id": c.get("legacy_fbid") or c["id"], "chu": bo_the_facebook(c.get("body") or {}), "luc": sr._dau(c, "created_time") or 0,
            "thich": thich, "tac_gia": (c.get("author") or {}).get("name") or "", "link": fb.get("url") or ""}


def lay_facebook(trang: str, so_ngay: int, so_bai: int = SO_BAI_BL, bao=None, huy=None) -> List[Dict]:
    import ad_research as ar
    import content_check as cc
    import nguon_quang_cao as nq
    from playwright.sync_api import sync_playwright
    kq = sr.lay_facebook(trang, 15, bao=bao, huy=huy)
    chu_trang = kq["kenh"].get("ten") or ""
    ds = chon_bai([b for b in kq["bai"] if b.get("binh_luan")], so_ngay, so_bai)
    ra: List[Dict] = []
    if not ds:
        return ra
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        try:
            ctx = b.new_context(viewport={"width": 1280, "height": 900}, locale="vi-VN", user_agent=nq.UA)
            ctx.add_cookies(cc.cookie_facebook(sr.COOKIE))
            pg = ctx.new_page()
            hien: Dict[str, Dict] = {}

            def nghe(r):
                if "/api/graphql" in r.url:
                    try:
                        t = r.text()
                    except Exception:  # noqa: BLE001
                        return
                    if '"Comment"' in t:
                        for k in sr.khoi_gql(t):
                            doc_bl_facebook(k, hien)
            pg.on("response", nghe)
            for i, bai in enumerate(ds):
                if huy and huy():
                    break
                if bao:
                    bao(f"Đang đọc bình luận Facebook {i + 1}/{len(ds)}")
                hien.clear()
                pg.goto(bai["link"], wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(5000)
                if "/login" in pg.url or "checkpoint" in pg.url:
                    raise RuntimeError("Phiên Facebook đã hết hạn. Đăng nhập lại Facebook trên Chrome rồi bấm Cài đặt > Lấy lại cookie.")
                for k in ar.doc_khoi_json(pg.content()):
                    doc_bl_facebook(k, hien)
                pg.mouse.wheel(0, 3000)
                pg.wait_for_timeout(1500)
                for c in map(chuan_bl_facebook, list(hien.values())):
                    if not c["chu"].strip() or (chu_trang and c["tac_gia"] == chu_trang):
                        continue
                    ra.append(_y("facebook", "binh_luan", c["id"], c["chu"], c["luc"], c["thich"], c["link"] or bai["link"],
                                 (bai.get("chu") or "").split("\n")[0]))
        finally:
            b.close()
    return ra


# ─────────────────────────────────────────────────────────────────────────────
# AI
# ─────────────────────────────────────────────────────────────────────────────

_HE_PHAN = """You classify Vietnamese social-media comments and news headlines about brands for a marketing team.
Brands (index: name): {brands}. Each item has i, b (brand index it was collected for), n (source), t (text), p (the post/video
it was under, or news outlet). Return ONLY JSON {{"kq": [{{"i": 0, "lq": 1, "cx": "khen|che|trung_lap", "yd": "hoi|muon_mua|than_phien|khen|khac", "cd": "chủ đề"}}]}}
- lq: 1 if the text is about the brand, its products, stores, service, price, ads, staff, or news about the company; 0 if
  off-topic (chatting with friends, tagging only, talking about the video editing, spam, giveaway entries like "username: x").
- cx: sentiment toward the brand. Sarcasm counts as "che". Questions are usually "trung_lap".
- yd: hoi = asks a question; muon_mua = wants to buy / asks price or where to buy; than_phien = complaint; khen = praise.
- cd: 2-5 Vietnamese words naming the aspect (e.g. "giá cao", "giao hàng chậm", "nhân viên thân thiện", "vị ngon",
  "khuyến mãi", "đổi chủ / IPO"). Empty if lq is 0.
Return one object per item, same i."""

_HE_GOM = """You group short Vietnamese topic labels about brands into at most 14 clear groups, Vietnamese names of 2-5
words (e.g. "Giá", "Giao hàng", "Chất lượng sản phẩm", "Nhân viên, dịch vụ", "Khuyến mãi", "Tin doanh nghiệp").
If a group name from the previous run fits, reuse it EXACTLY so runs can be compared. Return ONLY JSON
{"nhom": {"<label>": "<group name>"}} covering every label given."""

_HE_TONG = """You are a social-listening analyst for a Vietnamese marketing team. You get what people said about one brand
(the first, "cua_ban") and up to 2 competitors in the last N days: counts per brand (praise / complaint / neutral, by source,
by topic group), the previous run's counts if any, and real quotes. Quotes are public comments; never invent quotes or
numbers. All text in Vietnamese. Return ONLY JSON:
{"tom_tat": "3-4 sentences: overall mood, the biggest issue, the biggest change vs last run if any",
 "thuong_hieu": [{"ten": "brand", "nhan_xet": "2 sentences with numbers"}],
 "than_phien": [{"van_de": "complaint theme", "thuong_hieu": "brand", "muc_do": "cao|vua|thap", "vi_du": "real quote", "goi_y": "what the brand should do"}],
 "khen": [{"diem": "what people love", "thuong_hieu": "brand", "vi_du": "real quote"}],
 "cau_hoi": [{"cau_hoi": "question people keep asking", "goi_y": "answer or content that addresses it"}],
 "so_sanh": "how our brand compares with competitors on what people say, or empty if no competitor",
 "bao_chi": "1-2 sentences on news coverage, or empty",
 "co_hoi": [{"ten": "short name", "mo_ta": "1-2 sentence content idea for OUR brand built on these insights", "kieu": "ugc|quang_cao|dien_anh"}]}
3-6 items per list, 4-5 co_hoi. muc_do "cao" only when many people say it or it is severe (safety, scam, food poisoning).
If there is too little data for a brand, say so."""


def _ai(he: str, nguoi: Dict, max_tokens: int = 4000) -> Dict:
    kq = lf.ask_json(he, json.dumps(nguoi, ensure_ascii=False), lay_cai_dat_ai() or {}, max_tokens=max_tokens)
    if not kq.get("success"):
        raise RuntimeError(str(kq.get("error"))[:200])
    return kq.get("data") or {}


def phan_loai(y_kien: List[Dict], ten_th: List[str], co: int = 60) -> None:
    """Gắn lq, cx, yd, cd cho từng ý kiến (sửa tại chỗ). Mẻ hỏng thì các ý kiến đó để trung lập, chủ đề rỗng."""
    he = _HE_PHAN.format(brands=", ".join(f"{i}: {t}" for i, t in enumerate(ten_th)))

    def mot(me: List[int]) -> Dict:
        goi = {"items": [{"i": i, "b": y_kien[i]["th"], "n": y_kien[i]["nguon"], "t": y_kien[i]["chu"][:300],
                          "p": y_kien[i]["bai"][:80]} for i in me]}
        try:
            return {int(x["i"]): x for x in _ai(he, goi, 6000).get("kq") or [] if isinstance(x, dict) and "i" in x}
        except Exception:  # noqa: BLE001
            return {}
    me = [list(range(i, min(i + co, len(y_kien)))) for i in range(0, len(y_kien), co)]
    with ThreadPoolExecutor(3) as ex:
        kq: Dict[int, Dict] = {}
        for x in ex.map(mot, me):
            kq.update(x)
    for i, y in enumerate(y_kien):
        x = kq.get(i) or {}
        y["lq"] = 0 if str(x.get("lq")) == "0" else 1
        y["cx"] = x.get("cx") if x.get("cx") in CAM_XUC else "trung_lap"
        y["yd"] = x.get("yd") if x.get("yd") in Y_DINH else "khac"
        y["cd"] = str(x.get("cd") or "").strip()[:40] if y["lq"] else ""
        y["da_phan"] = bool(x)


def gom_nhom(y_kien: List[Dict], nhom_cu: List[str]) -> None:
    nhan = sorted({y["cd"] for y in y_kien if y.get("cd")})
    if not nhan:
        return
    try:
        bang = _ai(_HE_GOM, {"nhan": nhan, "nhom_lan_truoc": nhom_cu}, 4000).get("nhom") or {}
    except Exception:  # noqa: BLE001 - không gom được thì dùng luôn nhãn gốc
        bang = {}
    for y in y_kien:
        if y.get("cd"):
            y["nhom"] = str(bang.get(y["cd"]) or y["cd"]).strip()[:40]


def thong_ke(y_kien: List[Dict], so_th: int) -> List[Dict]:
    ra = []
    for t in range(so_th):
        ys = [y for y in y_kien if y["th"] == t and y.get("lq", 1)]
        cx = Counter(y["cx"] for y in ys)
        co_y = cx["khen"] + cx["che"]
        ra.append({"tong": len(ys), "khen": cx["khen"], "che": cx["che"], "trung_lap": cx["trung_lap"],
                   "ti_le_che": round(100 * cx["che"] / co_y) if co_y else None,
                   "ti_le_khen": round(100 * cx["khen"] / co_y) if co_y else None,
                   "hoi": sum(1 for y in ys if y["yd"] == "hoi"), "muon_mua": sum(1 for y in ys if y["yd"] == "muon_mua"),
                   "nguon": dict(Counter(y["nguon"] for y in ys)),
                   "nhom_che": Counter(y.get("nhom") for y in ys if y["cx"] == "che" and y.get("nhom")).most_common(10),
                   "nhom_khen": Counter(y.get("nhom") for y in ys if y["cx"] == "khen" and y.get("nhom")).most_common(10),
                   "moi": sum(1 for y in ys if y.get("moi"))})
    return ra


def bao_dong(tk: List[Dict], tk_cu: Optional[List[Dict]], ten_th: List[str]) -> List[Dict]:
    """Báo động do code tính. So lần trước nếu có; lần đầu chỉ báo khi lời chê đã nhiều sẵn."""
    ra = []
    for t, x in enumerate(tk):
        ten = ten_th[t]
        cu = (tk_cu or [])[t] if tk_cu and t < len(tk_cu) else None
        if cu is None:
            if x["ti_le_che"] is not None and x["ti_le_che"] >= 40 and x["che"] >= 10:
                ra.append({"th": t, "muc": "cao", "chu": f"{ten}: {x['ti_le_che']}% ý kiến có khen chê là chê ({x['che']} lời chê)."})
        else:
            if x["ti_le_che"] is not None and cu.get("ti_le_che") is not None and x["ti_le_che"] - cu["ti_le_che"] >= 10 and x["che"] >= 5:
                ra.append({"th": t, "muc": "cao", "chu": f"{ten}: tỉ lệ chê tăng từ {cu['ti_le_che']}% lên {x['ti_le_che']}%."})
            if cu.get("che") is not None and x["che"] >= 10 and x["che"] >= 2 * max(1, cu["che"]):
                ra.append({"th": t, "muc": "cao", "chu": f"{ten}: số lời chê tăng từ {cu['che']} lên {x['che']}."})
            if cu.get("tong") and x["tong"] >= 20 and x["tong"] >= 2 * cu["tong"]:
                ra.append({"th": t, "muc": "vua", "chu": f"{ten}: lượng nhắc tăng vọt từ {cu['tong']} lên {x['tong']}."})
            nhom_cu = {n for n, _ in cu.get("nhom_che") or []}
            for n, so in x["nhom_che"]:
                if so >= 3 and n not in nhom_cu:
                    ra.append({"th": t, "muc": "vua", "chu": f"{ten}: chuyện mới bị chê, \"{n}\" ({so} lời)."})
    return ra


def _goi_tong(d: Dict, lan: Dict, tk_cu: Optional[List[Dict]]) -> Dict:
    ten_th = [t["ten"] for t in d["thuong_hieu"]]
    goi: Dict = {"so_ngay": d["so_ngay"], "thuong_hieu": []}
    for t, x in enumerate(lan["thong_ke"]):
        ys = [y for y in lan["y_kien"] if y["th"] == t and y.get("lq", 1)]
        trich = lambda loc, n: [{"t": y["chu"][:220], "n": y["nguon"], "thich": y["thich"]}
                                for y in sorted([y for y in ys if loc(y)], key=lambda y: -y["thich"])[:n]]
        goi["thuong_hieu"].append({
            "ten": ten_th[t], "cua_ban": t == 0, "so_lieu": {k: v for k, v in x.items() if k != "moi"},
            "lan_truoc": ({k: v for k, v in tk_cu[t].items() if k in ("tong", "khen", "che", "ti_le_che", "nhom_che")}
                          if tk_cu and t < len(tk_cu) else None),
            "loi_che": trich(lambda y: y["cx"] == "che", 25), "loi_khen": trich(lambda y: y["cx"] == "khen", 15),
            "cau_hoi": trich(lambda y: y["yd"] in ("hoi", "muon_mua"), 15),
            "tieu_de_bao": [y["chu"][:150] for y in ys if y["nguon"] == "bao"][:12]})
    return goi


# ─────────────────────────────────────────────────────────────────────────────
# CHẠY
# ─────────────────────────────────────────────────────────────────────────────

def chuan_thuong_hieu(x: Dict) -> Optional[Dict]:
    import nguon_quang_cao as nq
    ten = " ".join(str(x.get("ten") or "").split())[:60]
    tu = [" ".join(k.split())[:60] for k in re.split(r"[,;\n]", str(x.get("tu_khoa") or "")) if k.strip()]
    tiktok = nq.ten_kenh_tiktok(x["tiktok"]) if str(x.get("tiktok") or "").strip() else ""
    facebook = sr.chuan_facebook(x["facebook"]) if str(x.get("facebook") or "").strip() else ""
    if not ten and not tu:
        return None
    tu = list(dict.fromkeys(([ten] if ten else []) + tu))[:5]
    return {"ten": ten or tu[0], "tu_khoa": tu, "tiktok": tiktok, "facebook": facebook}


def tao(thuong_hieu: List[Dict], so_ngay: int = 30) -> Dict:
    th = [t for t in (chuan_thuong_hieu(x) for x in (thuong_hieu or [])[:TOI_DA_TH]) if t]
    if not th:
        raise ValueError("Nhập tên thương hiệu hoặc từ khoá cần nghe.")
    so_ngay = int(so_ngay) if int(so_ngay or 0) in SO_NGAY else 30
    d = {"id": time.strftime("sl_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4], "tao_luc": time.time(), "ten": th[0]["ten"],
         "thuong_hieu": th, "so_ngay": so_ngay, "job_id": "", "lan": []}
    _ghi(d)
    return chay(d["id"])


def _gon_lan_cu(d: Dict) -> None:
    """Giữ GIU_LAN lần; bỏ từng ý kiến của các lần cũ hơn lần kề trước (lần kề trước còn cần để đánh dấu ý kiến mới)."""
    d["lan"] = d["lan"][-GIU_LAN:]
    for x in d["lan"][:-2]:
        x.pop("y_kien", None)


def chay(ma: str) -> Dict:
    d = _doc(ma)
    if d is None:
        raise ValueError("Không có mục theo dõi này.")
    if (lf.get_job(d.get("job_id") or "") or {}).get("status") == "running":
        raise ValueError("Mục này đang nghe, chờ xong đã.")
    th = d["thuong_hieu"]
    viec_ds = [(t, n) for t in range(len(th)) for n in NGUON
               if n in ("bao", "youtube") or (n == "tiktok" and th[t]["tiktok"]) or (n == "facebook" and th[t]["facebook"])]
    with _khoa:
        d = _doc(ma)
        d["lan"].append({"luc": time.time(), "buoc": "chay", "loi": "", "so_ngay": d["so_ngay"],
                         "nguon": {f"{t}:{n}": {"tt": "cho", "loi": "", "so": 0} for t, n in viec_ds},
                         "y_kien": [], "thong_ke": [], "bao_dong": [], "phan_tich": None})
        _gon_lan_cu(d)
        _ghi(d)

    def lam(viec: Dict) -> None:
        huy = lambda: bool(viec.get("cancel"))
        gom: List[Dict] = []
        for k, (t, n) in enumerate(viec_ds):
            if huy():
                break
            x = th[t]
            lf.update_job(viec, step=f"{x['ten']}: đang đọc {TEN_NGUON[n]}", progress=int(3 + 57 * k / len(viec_ds)))
            bao = lambda chu, x=x: lf.update_job(viec, step=f"{x['ten']}: {chu[:1].lower()}{chu[1:]}")
            _sua_nguon(ma, f"{t}:{n}", tt="chay")
            try:
                if n == "bao":
                    ys = lay_bao(x["tu_khoa"], d["so_ngay"])
                elif n == "youtube":
                    ys = lay_youtube(x["tu_khoa"], d["so_ngay"], bao=bao, huy=huy)
                elif n == "tiktok":
                    ys = lay_tiktok(x["tiktok"], d["so_ngay"], bao=bao, huy=huy)
                else:
                    ys = lay_facebook(x["facebook"], d["so_ngay"], bao=bao, huy=huy)
                sau = time.time() - d["so_ngay"] * 86400
                ys = [dict(y, th=t) for y in ys if not y["luc"] or y["luc"] >= sau]
                gom += ys
                _sua_nguon(ma, f"{t}:{n}", tt="xong", so=len(ys))
            except Exception as e:  # noqa: BLE001 - một nguồn hỏng không kéo cả lần nghe
                _sua_nguon(ma, f"{t}:{n}", tt="loi", loi=str(e)[:300])
        if huy():
            _sua_lan(ma, buoc="loi", loi="Đã dừng.")
            return
        # Một ý kiến có thể đến từ hai thương hiệu (cùng video YouTube): giữ lần đầu.
        y_kien = list({y["id"] + str(y["th"]): y for y in gom}.values())
        if not y_kien:
            _sua_lan(ma, buoc="loi", loi="Không nghe được ý kiến nào. Xem lỗi từng nguồn ở trên, hoặc nới khoảng thời gian.")
            return
        cu = next((x for x in reversed(_doc(ma)["lan"][:-1]) if x.get("buoc") == "xong"), None)
        id_cu = {y["id"] for y in (cu or {}).get("y_kien") or []}
        for y in y_kien:
            y["moi"] = bool(cu) and y["id"] not in id_cu
        ten_th = [x["ten"] for x in th]
        lf.update_job(viec, step=f"AI đang đọc {len(y_kien)} ý kiến", progress=62)
        phan_loai(y_kien, ten_th)
        if not any(y["da_phan"] for y in y_kien):
            _sua_lan(ma, y_kien=y_kien, buoc="loi", loi="Đã nghe xong nhưng AI chưa phân loại được. Bấm Nghe lại.")
            return
        lf.update_job(viec, step="AI đang gom chủ đề", progress=80)
        nhom_cu = sorted({n for x in (cu or {}).get("thong_ke") or [] for n, _ in (x.get("nhom_che") or []) + (x.get("nhom_khen") or [])})
        gom_nhom(y_kien, nhom_cu)
        tk = thong_ke(y_kien, len(th))
        bd = bao_dong(tk, (cu or {}).get("thong_ke"), ten_th)
        lan = _sua_lan(ma, y_kien=y_kien, thong_ke=tk, bao_dong=bd)["lan"][-1]
        lf.update_job(viec, step="AI đang viết tổng kết", progress=88)
        try:
            pt = _ai(_HE_TONG, _goi_tong(d, lan, (cu or {}).get("thong_ke")), 6000)
        except Exception as e:  # noqa: BLE001
            _sua_lan(ma, buoc="loi", loi="Đã nghe và đếm xong nhưng AI chưa viết được tổng kết: " + str(e)[:200])
            return
        _sua_lan(ma, phan_tich=pt, buoc="xong", loi="", xong_luc=time.time())
        lf.update_job(viec, step="Xong", progress=100)

    def bo(viec: Dict) -> None:
        try:
            lam(viec)
        except Exception as e:  # noqa: BLE001
            _sua_lan(ma, buoc="loi", loi=str(e)[:400])
            raise

    return _sua(ma, job_id=lf.start_job("sl_nghe", bo))


def csv_y_kien(d: Dict, lan: Dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Thương hiệu", "Nguồn", "Loại", "Ngày", "Cảm xúc", "Ý định", "Chủ đề", "Lượt thích", "Mới", "Nội dung", "Dưới bài / báo", "Link"])
    ten = [t["ten"] for t in d["thuong_hieu"]]
    for y in lan.get("y_kien") or []:
        if not y.get("lq", 1):
            continue
        w.writerow([ten[y["th"]] if y["th"] < len(ten) else "", TEN_NGUON.get(y["nguon"], y["nguon"]), y["loai"],
                    time.strftime("%Y-%m-%d %H:%M", time.gmtime(y["luc"] + sr.GIO_VN)) if y["luc"] else "",
                    {"khen": "Khen", "che": "Chê", "trung_lap": "Trung lập"}.get(y.get("cx"), ""), y.get("yd", ""),
                    y.get("nhom") or y.get("cd") or "", y["thich"], "có" if y.get("moi") else "", y["chu"].replace("\n", " "),
                    y["bai"].replace("\n", " "), y["link"]])
    return "﻿" + out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _loi(msg: str, ma: int = 400):
    return jsonify({"ok": False, "error": msg}), ma


@bp.get("/api/listen/phien")
def api_phien():
    dang = [d["job_id"] for d in danh_sach() if (lf.get_job(d.get("job_id") or "") or {}).get("status") == "running"]
    return jsonify({"ok": True, "job_id": dang[0] if dang else ""})


@bp.get("/api/listen/theo_doi")
def api_ds():
    return jsonify({"ok": True, "theo_doi": [ban_gui(d, gon=True) for d in danh_sach()[:30]]})


@bp.get("/api/listen/theo_doi/<ma>")
def api_mot(ma):
    d = _doc(ma)
    return jsonify({"ok": True, "theo_doi": ban_gui(d)}) if d else _loi("Không có mục theo dõi này.", 404)


@bp.post("/api/listen/theo_doi")
def api_tao():
    t = request.get_json(silent=True) or {}
    try:
        d = tao(t.get("thuong_hieu") or [], t.get("so_ngay") or 30)
    except ValueError as e:
        return _loi(str(e))
    return jsonify({"ok": True, "theo_doi": ban_gui(d)})


@bp.post("/api/listen/theo_doi/<ma>/chay")
def api_chay(ma):
    if not _doc(ma):
        return _loi("Không có mục theo dõi này.", 404)
    try:
        d = chay(ma)
    except ValueError as e:
        return _loi(str(e))
    return jsonify({"ok": True, "theo_doi": ban_gui(d)})


@bp.post("/api/listen/theo_doi/<ma>/dung")
def api_dung(ma):
    d = _doc(ma)
    return jsonify({"ok": bool(d and d.get("job_id") and lf.cancel_job(d["job_id"]))})


@bp.delete("/api/listen/theo_doi/<ma>")
def api_xoa(ma):
    d = _doc(ma)
    if not d:
        return _loi("Không có mục theo dõi này.", 404)
    if (lf.get_job(d.get("job_id") or "") or {}).get("status") == "running":
        return _loi("Đang nghe, dừng trước rồi mới xoá.")
    os.remove(_duong(ma))
    return jsonify({"ok": True})


@bp.get("/api/listen/theo_doi/<ma>/csv")
def api_csv(ma):
    d = _doc(ma)
    if not d or not d.get("lan"):
        return _loi("Không có mục theo dõi này.", 404)
    try:
        lan = d["lan"][int(request.args.get("lan", len(d["lan"]) - 1))]
    except (ValueError, IndexError):
        return _loi("Không có lần nghe này.", 404)
    ten = re.sub(r"[^\w\-]+", "_", d["ten"])[:40] or "listening"
    return Response(csv_y_kien(d, lan), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=listening_{ten}.csv"})
