"""
SOCIAL RESEARCH (22/10/2026): soi bài đăng (không phải quảng cáo) của một thương hiệu trên TikTok, YouTube và Fanpage Facebook.

Lấy bài gần nhất của từng kênh, tính "hệ số": lượt xem (Facebook: tương tác) của bài chia cho trung vị của chính kênh đó,
bài từ 2 lần trở lên là bài "nổ". Nghe lời mở đầu các video nổ nhất, rồi AI rút ra chủ đề ăn khách, mẫu hook, định dạng,
lịch đăng, vì sao bài kém, và ý tưởng cho sản phẩm của người dùng (nút sang Bulk Generate).

Nguồn:
- TikTok: kênh công khai, không đăng nhập (nguon_quang_cao.lay_kenh_tiktok).
- YouTube: danh sách video + Shorts bằng yt-dlp; số liệu chính xác và ngày đăng qua YouTube Data API bằng kênh đã nối của
  người dùng (videos.list, 1 đơn vị quota mỗi 50 video). Chưa nối kênh thì yt-dlp đọc từng video (chậm hơn).
- Facebook: Chrome ẩn mang phiên Facebook của người dùng (cookies.txt, như Content Check), mở fanpage, đọc bài từ phản hồi
  GraphQL khi cuộn. Facebook không cho thấy lượt xem video ở đây: chỉ có cảm xúc, bình luận, chia sẻ. Trang cá nhân thì
  từ chối (chỉ fanpage thương hiệu). KHÔNG chụp màn hình phiên Facebook.
Chỉ dành cho thương hiệu, doanh nghiệp. KHÔNG theo dõi cá nhân.

Máy chủ: `/api/social/*`. Trang: `static/social_research.js` + `.css`. Bài kiểm: `tests/test_social_research.py`.
Dữ liệu: `downloads/social_research/<mã>.json`. Việc nền tiền tố `sr_`.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from flask import Blueprint, Response, jsonify, request

import longform_studio as lf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "social_research")
COOKIE = os.path.join(BASE_DIR, "cookies.txt")
NEN = ("tiktok", "youtube", "facebook")
TEN_NEN = {"tiktok": "TikTok", "youtube": "YouTube", "facebook": "Facebook"}
SO_BAI = (20, 30, 50)
NO = 2.0            # hệ số từ đây trở lên là bài nổ
SO_NGHE = 8         # số video nổ nhất được nghe lời mở đầu
GIO_VN = 7 * 3600

lay_cai_dat_ai: Callable[[], Dict] = lambda: {}
_khoa = threading.RLock()
bp = Blueprint("social_research", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# KHO
# ─────────────────────────────────────────────────────────────────────────────

def _ma_hop_le(ma: str) -> bool:
    return bool(re.fullmatch(r"sr_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", ma or ""))


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
            raise ValueError("Không có lượt soi này.")
        d.update(gia_tri)
        _ghi(d)
        return d


def _sua_nen(ma: str, nen: str, **gia_tri) -> None:
    with _khoa:
        d = _doc(ma)
        d["nen"][nen].update(gia_tri)
        _ghi(d)


def danh_sach() -> List[Dict]:
    if not os.path.isdir(KHO):
        return []
    ra = [d for d in (_doc(f[:-5]) for f in os.listdir(KHO) if f.endswith(".json")) if d]
    ra.sort(key=lambda d: d.get("tao_luc") or 0, reverse=True)
    return ra


def ban_gui(d: Dict, gon: bool = False) -> Dict:
    v = lf.get_job(d.get("job_id") or "") or {}
    chay = v.get("status") == "running"
    ra = {**d, "dang_chay": chay, "tien_do": v.get("progress") or 0, "dang_lam": v.get("step") or ""}
    if not chay and d.get("buoc") == "chay":
        ra["buoc"], ra["loi"] = "loi", d.get("loi") or "Tool khởi động lại giữa chừng. Chạy lại."
    if gon:
        ra.pop("nen", None)
        ra.pop("phan_tich", None)
    return ra


# ─────────────────────────────────────────────────────────────────────────────
# TÊN KÊNH
# ─────────────────────────────────────────────────────────────────────────────

def chuan_youtube(s: str) -> str:
    """@handle, link kênh, hay mã UC... thành địa chỉ gốc của kênh."""
    s = (s or "").strip()
    m = re.search(r"youtube\.com/((?:@[\w.\-]+)|(?:channel/UC[\w\-]{20,})|(?:c/[\w.\-]+)|(?:user/[\w.\-]+))", s)
    if m:
        return "https://www.youtube.com/" + m.group(1)
    if re.fullmatch(r"UC[\w\-]{20,}", s):
        return "https://www.youtube.com/channel/" + s
    s = s.lstrip("@")
    if re.fullmatch(r"[\w.\-]{3,40}", s):
        return "https://www.youtube.com/@" + s
    raise ValueError("Nhập kênh YouTube, ví dụ @vinamilk hoặc link kênh.")


def chuan_facebook(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"facebook\.com/profile\.php\?id=(\d+)", s)
    if m:
        return f"https://www.facebook.com/profile.php?id={m.group(1)}"
    m = re.search(r"(?:facebook|fb)\.com/(?:pg/)?([\w.\-]+)", s)
    s = m.group(1) if m else s.lstrip("@")
    if re.fullmatch(r"\d{6,20}", s):
        return f"https://www.facebook.com/profile.php?id={s}"
    if re.fullmatch(r"[\w.\-]{3,60}", s) and s.lower() not in ("profile.php", "watch", "reel", "groups", "events", "share"):
        return "https://www.facebook.com/" + s
    raise ValueError("Nhập fanpage Facebook, ví dụ facebook.com/ShopeeVN.")


def so_rut_gon(s: str) -> Optional[int]:
    """'33M', '1,2K', '2,5 triệu', '12 N' thành số."""
    m = re.match(r"\s*([\d.,]+)\s*(k|n|nghìn|ngàn|m|tr|triệu|b|tỷ)?\b", (s or "").lower())
    if not m:
        return None
    so = m.group(1)
    if m.group(2):
        so = so.replace(",", ".")
        if so.count(".") > 1:
            so = so.replace(".", "")
    else:
        so = so.replace(",", "").replace(".", "")
    try:
        x = float(so)
    except ValueError:
        return None
    nhan = {"k": 1e3, "n": 1e3, "nghìn": 1e3, "ngàn": 1e3, "m": 1e6, "tr": 1e6, "triệu": 1e6, "b": 1e9, "tỷ": 1e9}.get(m.group(2) or "", 1)
    return int(round(x * nhan))


# ─────────────────────────────────────────────────────────────────────────────
# LẤY BÀI
# Mỗi bài: nen, id, chu, luc (giây unix), xem (None khi nền không cho), thich, binh_luan, chia_se, luu, giay, anh, link,
# dinh_dang (video | short | anh | album | chu).
# ─────────────────────────────────────────────────────────────────────────────

def lay_tiktok(ten: str, so_bai: int = 30, bao=None) -> Dict:
    import nguon_quang_cao as nq
    kq = nq.lay_kenh_tiktok(ten, so_bai, bao=(lambda n: bao(f"Đang đọc TikTok: {n} video")) if bao else None)
    bai = [{"nen": "tiktok", "id": v["id"], "chu": v["chu"], "luc": v["luc"], "xem": v["xem"], "thich": v["thich"],
            "binh_luan": v["binh_luan"], "chia_se": v["chia_se"], "luu": v["luu"], "giay": v["giay"], "anh": v["anh"],
            "link": v["link"], "dinh_dang": "video", "ghim": v.get("ghim", False)} for v in kq["video"]]
    k = kq.get("kenh") or {}
    return {"kenh": {"ten": k.get("ten") or ten, "ma": "@" + (k.get("ma") or ten), "theo_doi": k.get("theo_doi"),
                     "mo_ta": k.get("mo_ta") or "", "anh": k.get("anh") or "", "tong_bai": k.get("so_video"),
                     "link": f"https://www.tiktok.com/@{k.get('ma') or ten}"}, "bai": bai}


def _giay_iso(s: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _luc_iso(s: str) -> int:
    import calendar
    try:
        return calendar.timegm(time.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def _yt_api(ids: List[str]) -> Dict[str, Dict]:
    """Số liệu chính xác qua YouTube Data API bằng kênh người dùng đã nối. Không nối thì trả rỗng."""
    try:
        import youtube_upload as yu
        dv = yu._dich_vu(None)
    except Exception:  # noqa: BLE001
        return {}
    if not dv:
        return {}
    ra = {}
    for i in range(0, len(ids), 50):
        r = dv.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for x in r.get("items") or []:
            sn, st = x.get("snippet") or {}, x.get("statistics") or {}
            tn = sn.get("thumbnails") or {}
            ra[x["id"]] = {"chu": (sn.get("title") or "") + ("\n" + sn["description"][:300] if sn.get("description") else ""),
                           "luc": _luc_iso(sn.get("publishedAt")), "xem": int(st.get("viewCount") or 0),
                           "thich": int(st["likeCount"]) if st.get("likeCount") else None,
                           "binh_luan": int(st.get("commentCount") or 0), "giay": _giay_iso((x.get("contentDetails") or {}).get("duration")),
                           "anh": (tn.get("high") or tn.get("medium") or tn.get("default") or {}).get("url", "")}
    return ra


def _yt_dlp_mot(vid: str) -> Optional[Dict]:
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            i = y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False)
    except Exception:  # noqa: BLE001
        return None
    return {"chu": (i.get("title") or "") + ("\n" + (i.get("description") or "")[:300] if i.get("description") else ""),
            "luc": int(i.get("timestamp") or 0), "xem": int(i.get("view_count") or 0), "thich": i.get("like_count"),
            "binh_luan": int(i.get("comment_count") or 0), "giay": int(i.get("duration") or 0), "anh": i.get("thumbnail") or ""}


def lay_youtube(kenh: str, so_bai: int = 30, bao=None, huy=None) -> Dict:
    import yt_dlp
    goc = chuan_youtube(kenh)
    ds: Dict[str, str] = {}          # id -> "video" | "short"
    tt: Dict = {}
    for tab, loai in (("/videos", "video"), ("/shorts", "short")):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": so_bai,
                                   "skip_download": True}) as y:
                i = y.extract_info(goc + tab, download=False) or {}
        except Exception:  # noqa: BLE001 - kênh không có tab Shorts hay tab Video
            continue
        tt = tt or i
        for e in i.get("entries") or []:
            if e and e.get("id"):
                ds.setdefault(e["id"], loai)
    if not ds:
        raise RuntimeError("Không đọc được kênh YouTube này (sai tên kênh, hoặc kênh chưa có video).")
    if bao:
        bao(f"Đang đọc số liệu {len(ds)} video YouTube")
    so = _yt_api(list(ds))
    cach = "api"
    if not so:
        cach = "yt_dlp"
        with ThreadPoolExecutor(4) as ex:
            for vid, x in zip(ds, ex.map(_yt_dlp_mot, list(ds))):
                if x:
                    so[vid] = x
    bai = []
    for vid, loai in ds.items():
        x = so.get(vid)
        if not x:
            continue
        bai.append({"nen": "youtube", "id": vid, **x, "chia_se": None, "luu": None, "dinh_dang": loai,
                    "link": ("https://www.youtube.com/shorts/" if loai == "short" else "https://www.youtube.com/watch?v=") + vid})
    bai.sort(key=lambda b: -b["luc"])
    return {"kenh": {"ten": tt.get("channel") or tt.get("uploader") or kenh, "ma": tt.get("uploader_id") or "",
                     "theo_doi": tt.get("channel_follower_count"), "link": goc, "cach_doc": cach}, "bai": bai[:so_bai]}


def _dau(o, khoa: str):
    """Giá trị đầu tiên của khoá `khoa` khi đi sâu vào o."""
    if isinstance(o, dict):
        if khoa in o:
            return o[khoa]
        for v in o.values():
            r = _dau(v, khoa)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _dau(v, khoa)
            if r is not None:
                return r
    return None


def _theo(o, *duong):
    for k in duong:
        if isinstance(o, list):
            o = o[k] if isinstance(k, int) and len(o) > k else None
        elif isinstance(o, dict):
            o = o.get(k)
        else:
            return None
    return o


def tim_bai_facebook(o, ra: Dict[str, Dict]) -> None:
    """Gom mọi nút Story (có post_id và comet_sections) trong một khối JSON GraphQL."""
    if isinstance(o, dict):
        if o.get("post_id") and "comet_sections" in o:
            ra.setdefault(str(o["post_id"]), o)
            return
        for v in o.values():
            tim_bai_facebook(v, ra)
    elif isinstance(o, list):
        for v in o:
            tim_bai_facebook(v, ra)


def _dem(x) -> int:
    if isinstance(x, dict):
        x = x.get("count")
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def chuan_bai_facebook(s: Dict) -> Dict:
    noi = _theo(s, "comet_sections", "content", "story") or s
    chu = _theo(noi, "comet_sections", "message", "story", "message", "text") or ""
    if not chu:
        m = _dau(noi, "message")
        chu = m.get("text", "") if isinstance(m, dict) else ""
    ufi = _dau(s.get("comet_sections", {}).get("feedback"), "comet_ufi_summary_and_actions_renderer") or {}
    fb = ufi.get("feedback") or {}
    tep = s.get("attachments") or noi.get("attachments") or []
    media = _theo(tep, 0, "styles", "attachment", "media") or _theo(tep, 0, "media") or {}
    loai = media.get("__typename") if isinstance(media, dict) else None
    so_anh = _dau(tep, "all_subattachments")
    dd = "chu"
    if loai == "Video":
        dd = "video"
    elif loai == "Photo":
        dd = "album" if isinstance(so_anh, dict) and _dem(so_anh) > 1 else "anh"
    elif tep:
        dd = "album" if isinstance(so_anh, dict) else "anh"
    anh = ""
    for k in ("preferred_thumbnail", "photo_image", "image", "placeholder_image"):
        v = _dau(tep, k)
        if isinstance(v, dict):
            anh = v.get("uri") or _theo(v, "image", "uri") or ""
            if anh:
                break
    link = noi.get("wwwURL") or s.get("wwwURL") or _theo(noi, "comet_sections", "message", "story", "url") or \
        f"https://www.facebook.com/{s.get('post_id')}"
    bl = _dau(fb, "comments") or _dau(s.get("comet_sections", {}).get("feedback"), "comments") or {}
    binh_luan = _dem(bl.get("total_count") if isinstance(bl, dict) else 0)
    tac_gia = _theo(noi, "actors", 0, "name") or ""
    # Đường mp4 thẳng của video (yt-dlp 2025.10 không đọc được video Facebook nữa: "Cannot parse data"). Đường này hết hạn sau
    # vài giờ, chỉ dùng để nghe lời mở đầu ngay trong lượt.
    mp4 = _dau(tep, "progressive_url") if dd == "video" else None
    return {"nen": "facebook", "id": str(s.get("post_id")), "chu": str(chu)[:1200], "luc": int(_dau(s, "creation_time") or 0),
            "xem": None, "thich": _dem(_dau(fb, "reaction_count")), "binh_luan": binh_luan,
            "chia_se": _dem(_dau(fb, "share_count")), "luu": None,
            "giay": int((_dau(tep, "playable_duration_in_ms") or 0) / 1000), "anh": anh, "link": link, "dinh_dang": dd,
            "tac_gia": tac_gia, "video": mp4 if isinstance(mp4, str) and mp4.startswith("https://") else ""}


def khoi_gql(t: str) -> List:
    """Phản hồi GraphQL của Facebook: mỗi dòng một khối JSON (khối sau mang "label" khi Facebook gửi dần)."""
    ra = []
    for dong in t.splitlines():
        dong = dong.strip()
        if dong.startswith("{"):
            try:
                ra.append(json.loads(dong))
            except ValueError:
                pass
    return ra


_CA_NHAN = re.compile(r"\b(Add friend|Thêm bạn bè|Kết bạn)\b")


def lay_facebook(trang: str, so_bai: int = 30, bao=None, huy=None) -> Dict:
    import ad_research as ar
    import content_check as cc
    from playwright.sync_api import sync_playwright
    url = chuan_facebook(trang)
    cookie = cc.cookie_facebook(COOKIE)
    if not any(c["name"] == "c_user" for c in cookie):
        raise RuntimeError("Chưa có phiên Facebook trong cookies.txt. Đăng nhập Facebook trên Chrome rồi bấm Cài đặt > Lấy lại cookie.")
    tho: Dict[str, Dict] = {}

    def nghe(r):
        if "/api/graphql" not in r.url:
            return
        try:
            t = r.text()
        except Exception:  # noqa: BLE001
            return
        if '"post_id"' in t:
            for k in khoi_gql(t):
                tim_bai_facebook(k, tho)

    import nguon_quang_cao as nq
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        try:
            ctx = b.new_context(viewport={"width": 1280, "height": 900}, locale="vi-VN", user_agent=nq.UA)
            ctx.add_cookies(cookie)
            pg = ctx.new_page()
            pg.on("response", nghe)
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(5000)
            if "/login" in pg.url or "checkpoint" in pg.url:
                raise RuntimeError("Phiên Facebook đã hết hạn. Đăng nhập lại Facebook trên Chrome rồi bấm Cài đặt > Lấy lại cookie.")
            chinh = pg.query_selector("div[role='main']")
            dau = chinh.inner_text()[:1500] if chinh else ""
            if _CA_NHAN.search(dau):
                raise RuntimeError("Đây là trang cá nhân, không phải fanpage. Social Research chỉ soi fanpage thương hiệu.")
            m = re.search(r"([\d.,]+\s?(?:K|M|B|N|Tr|triệu|nghìn)?)\s+(?:followers|người theo dõi)", dau)
            theo_doi = so_rut_gon(m.group(1)) if m else None
            ten = (pg.title() or "").replace("| Facebook", "").strip()
            if not ten or ten == "Facebook":
                h = pg.query_selector("div[role='main'] h1")
                ten = h.inner_text().strip() if h else ""
            for k in ar.doc_khoi_json(pg.content()):
                tim_bai_facebook(k, tho)
            dung = 0
            while len(tho) < so_bai and dung < 4 and not (huy and huy()):
                truoc = len(tho)
                pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(1800)
                dung = dung + 1 if len(tho) == truoc else 0
                if bao:
                    bao(f"Đang đọc fanpage Facebook: {len(tho)} bài")
        finally:
            b.close()
    if not tho:
        raise RuntimeError("Không thấy bài nào trên trang này (sai địa chỉ fanpage, trang chưa đăng bài, hoặc Facebook đang chặn).")
    bai = [chuan_bai_facebook(s) for s in tho.values()]
    # Bài người khác viết lên trang (khách check-in, đánh giá) không phải bài của thương hiệu.
    tac = Counter(x["tac_gia"] for x in bai if x["tac_gia"]).most_common(1)
    chu_trang = tac[0][0] if tac else ""
    bai = [x for x in bai if not chu_trang or not x["tac_gia"] or x["tac_gia"] == chu_trang]
    bai.sort(key=lambda x: -x["luc"])
    return {"kenh": {"ten": chu_trang or ten, "ma": url.rsplit("/", 1)[-1], "theo_doi": theo_doi, "link": url}, "bai": bai[:so_bai]}


# ─────────────────────────────────────────────────────────────────────────────
# TÍNH
# ─────────────────────────────────────────────────────────────────────────────

def tuong_tac(b: Dict) -> int:
    return sum(int(b.get(k) or 0) for k in ("thich", "binh_luan", "chia_se", "luu"))


def thuoc_do(b: Dict) -> float:
    """Con số để so bài với bài trong cùng kênh: lượt xem nếu nền cho, không thì tương tác."""
    return float(b["xem"]) if b.get("xem") is not None else float(tuong_tac(b))


def chay_qc(b: Dict) -> bool:
    """YouTube: lượt xem rất cao mà gần như không ai thích, bình luận thường là video được chạy quảng cáo (lượt xem trả tiền).
    Thử 22/10/2026 kênh Shopee: 14 triệu lượt xem, 12 lượt thích."""
    return b.get("nen") == "youtube" and (b.get("xem") or 0) >= 20000 and \
        ((b.get("thich") or 0) + (b.get("binh_luan") or 0)) / b["xem"] < 0.0005


def cham_diem(bai: List[Dict]) -> List[Dict]:
    """Gắn he_so (so với trung vị của kênh) và no (bài nổ). Video chạy quảng cáo không tính vào trung vị và không bao giờ là
    bài nổ (lượt xem mua bằng tiền), kẻo chúng đè hết bài tự nhiên."""
    for b in bai:
        b["co_chay_qc"] = chay_qc(b)
    gia = [thuoc_do(b) for b in bai if not b["co_chay_qc"]] or [thuoc_do(b) for b in bai]
    tv = statistics.median(gia) if gia else 0
    for b in bai:
        b["tuong_tac"] = tuong_tac(b)
        b["he_so"] = round(thuoc_do(b) / tv, 2) if tv > 0 else 0.0
        b["no"] = tv > 0 and b["he_so"] >= NO and not b["co_chay_qc"]
    return bai


def _gio(luc: int) -> time.struct_time:
    return time.gmtime(luc + GIO_VN)


THU = ("Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy", "Chủ nhật")


def tom_nen(kenh: Dict, bai: List[Dict]) -> Dict:
    if not bai:
        return {"so_bai": 0, "theo_doi": kenh.get("theo_doi")}
    luc = sorted(b["luc"] for b in bai if b["luc"])
    tuan = (luc[-1] - luc[0]) / 86400 / 7 if len(luc) > 1 else 0
    co_xem = [b for b in bai if b.get("xem") is not None and not b.get("co_chay_qc")]
    ra = {"so_bai": len(bai), "theo_doi": kenh.get("theo_doi"), "bai_moi_tuan": round(len(bai) / tuan, 1) if tuan >= 0.3 else None,
          "tu_ngay": luc[0] if luc else 0, "den_ngay": luc[-1] if luc else 0,
          "tuong_tac_trung_vi": round(statistics.median([b["tuong_tac"] for b in bai])), "so_no": sum(1 for b in bai if b["no"]),
          "so_chay_qc": sum(1 for b in bai if b.get("co_chay_qc"))}
    if co_xem:
        ra["xem_trung_vi"] = round(statistics.median([b["xem"] for b in co_xem]))
        ra["xem_cao_nhat"] = max(b["xem"] for b in co_xem)
        ra["ti_le_tuong_tac"] = round(100 * statistics.median([b["tuong_tac"] / max(1, b["xem"]) for b in co_xem]), 2)
    elif kenh.get("theo_doi"):
        ra["ti_le_tuong_tac"] = round(100 * ra["tuong_tac_trung_vi"] / max(1, kenh["theo_doi"]), 4)
    nhom: Dict[str, List[float]] = {}
    tu_nhien = [b for b in bai if not b.get("co_chay_qc")] or bai
    for b in tu_nhien:
        nhom.setdefault(b["dinh_dang"], []).append(b["he_so"])
    ra["dinh_dang"] = {k: {"so": len(v), "he_so": round(statistics.median(v), 2)} for k, v in nhom.items()}
    gio: Dict[int, List[float]] = {}
    thu: Dict[int, List[float]] = {}
    for b in tu_nhien:
        if b["luc"]:
            g = _gio(b["luc"])
            gio.setdefault(g.tm_hour, []).append(b["he_so"])
            thu.setdefault(g.tm_wday, []).append(b["he_so"])
    ra["gio_dang"] = {str(k): {"so": len(v), "he_so": round(statistics.median(v), 2)} for k, v in sorted(gio.items())}
    ra["thu_dang"] = {THU[k]: {"so": len(v), "he_so": round(statistics.median(v), 2)} for k, v in sorted(thu.items())}
    giay = [b["giay"] for b in bai if b.get("giay")]
    if giay:
        ra["giay_trung_vi"] = round(statistics.median(giay))
    return ra


# ─────────────────────────────────────────────────────────────────────────────
# NGHE LỜI MỞ ĐẦU
# ─────────────────────────────────────────────────────────────────────────────

NGON_NGU = ("vi", "en")


def _nghe_wav(wav: str) -> str:
    """Nghe một file wav. Video toàn nhạc thì whisper hay đoán ra tiếng khác và chép bậy (thử 22/10/2026 ra tiếng Hàn):
    chỉ nhận tiếng Việt, tiếng Anh."""
    import edit_studio as es
    with es._khoa_whisper:
        doan, tt = es._mo_hinh_whisper().transcribe(wav, vad_filter=True)
        chu = " ".join(x.text.strip() for x in doan)[:800]
    return chu if getattr(tt, "language", "vi") in NGON_NGU else ""


def nghe_dau(link: str, giay: int = 30, truc_tiep: str = "") -> str:
    """Tải tiếng `giay` giây đầu của một video (TikTok, YouTube, Facebook) rồi nghe. `truc_tiep`: đường mp4 thẳng (Facebook),
    có thì ffmpeg đọc luôn, không qua yt-dlp. Lỗi thì trả rỗng."""
    import yt_dlp
    tam = os.path.join(KHO, "tam", uuid.uuid4().hex)
    os.makedirs(tam, exist_ok=True)
    try:
        if truc_tiep:
            wav = os.path.join(tam, "a.wav")
            subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-v", "error", "-t", str(giay), "-i", truc_tiep, "-vn", "-ac", "1",
                            "-ar", "16000", wav], timeout=180, check=True)
            return _nghe_wav(wav)
        opt = {"outtmpl": os.path.join(tam, "a.%(ext)s"), "format": "ba/b[filesize<80M]/b", "quiet": True, "no_warnings": True,
               "noplaylist": True, "download_ranges": yt_dlp.utils.download_range_func(None, [(0, giay)]),
               "ffmpeg_location": os.path.dirname(shutil.which("ffmpeg") or "") or None}
        if "facebook.com" in link and os.path.isfile(COOKIE):
            opt["cookiefile"] = COOKIE
        # yt-dlp 2025.10 với YouTube 10/2026: máy khách mặc định báo "The page needs to be reloaded", máy khách android tải được.
        for them in ([{}, {"extractor_args": {"youtube": {"player_client": ["android"]}}}] if "youtube.com" in link else [{}]):
            try:
                with yt_dlp.YoutubeDL({**opt, **them}) as y:
                    y.download([link])
                break
            except Exception:  # noqa: BLE001
                continue
        tep = [f for f in os.listdir(tam) if f.startswith("a.") and not f.endswith((".part", ".ytdl"))]
        if not tep:
            return ""
        wav = os.path.join(tam, "a.wav")
        subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-v", "error", "-t", str(giay), "-i", os.path.join(tam, tep[0]),
                        "-vn", "-ac", "1", "-ar", "16000", wav], timeout=180, check=True)
        return _nghe_wav(wav)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        shutil.rmtree(tam, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# AI
# ─────────────────────────────────────────────────────────────────────────────

_HE = """You are a senior social-media strategist for a Vietnamese marketing team. You get the recent ORGANIC posts (not ads)
of ONE brand on TikTok, YouTube and/or its Facebook fanpage. Each post has "he_so" = its views (Facebook: reactions +
comments + shares, Facebook does not show views) divided by the channel's own median; >= 2 means it overperformed ("nổ").
Some videos have "loi_mo_dau" = transcript of the first 30 seconds. Learn what makes THIS brand's posts overperform and
underperform. All text in Vietnamese. Return ONLY JSON:
{"tom_tat": "3-4 sentences: content strategy, which platform works best for them, the single biggest lesson",
 "nen": [{"nen": "TikTok|YouTube|Facebook", "nhan_xet": "2 sentences with numbers", "cong_thuc": "the recipe of their winning posts on this platform in 1 sentence"}],
 "chu_de_an": [{"chu_de": "topic/content pillar", "vi_sao": "evidence: which posts, he_so"}],
 "hook_mau": [{"mau": "hook pattern (generalized)", "vi_du": "real example from their posts", "vi_sao": "why it works"}],
 "dinh_dang": ["format observations: length, video vs photo vs album, series, trends, collabs, KOL/KOC..."],
 "lich_dang": "posting cadence and the days/hours that work best, only if the data supports it",
 "bai_kem": ["patterns shared by underperforming posts"],
 "bai_top": [{"id": "post id", "vi_sao": "why this post overperformed"}],
 "y_tuong": [{"ten": "short name", "mo_ta": "1-2 sentence post/video idea for OUR product using their winning patterns", "nen": "TikTok|YouTube|Facebook", "kieu": "ugc|quang_cao|dien_anh"}]}
Use only the data given and quote numbers (he_so, views). 3-6 items per list, 5-8 bai_top, 5-6 y_tuong. Do not copy their
posts: adapt the pattern. If a platform has few posts or a channel looks unofficial (very few followers for a big brand),
say so instead of drawing conclusions. "co_chay_qc": true means a YouTube video with huge views but almost no likes or
comments, which almost always means the views were bought as ads: do NOT treat it as organic success, say it was likely
promoted."""


def _goi_ai(d: Dict) -> Dict:
    goi: Dict = {"thuong_hieu": d["ten"], "san_pham_cua_chung_ta": d.get("san_pham") or None}
    for n, v in d["nen"].items():
        if v["tt"] != "xong":
            if v["tt"] == "loi":
                goi[n] = {"loi": v.get("loi")}
            continue
        bai = v.get("bai") or []
        rut = lambda b: {k: b.get(k) for k in ("id", "dinh_dang", "xem", "thich", "binh_luan", "chia_se", "luu", "giay", "he_so", "loi_mo_dau", "co_chay_qc") if b.get(k) not in (None, "")} | \
            {"chu": (b.get("chu") or "")[:300], "ngay": time.strftime("%d/%m %H:%M", _gio(b["luc"])) if b.get("luc") else ""}
        xep = sorted(bai, key=lambda b: (bool(b.get("co_chay_qc")), -b["he_so"]))
        qc = [rut(b) for b in bai if b.get("co_chay_qc")][:5]
        goi[n] = {"kenh": {k: v["kenh"].get(k) for k in ("ten", "ma", "theo_doi")}, "tom": {k: x for k, x in (v.get("tom") or {}).items() if k not in ("tu_ngay", "den_ngay")},
                  "bai_tot": [rut(b) for b in xep[:12] if not b.get("co_chay_qc")],
                  "bai_kem": [rut(b) for b in [z for z in xep if not z.get("co_chay_qc")][-6:]] if len(xep) > 12 else [],
                  "video_chay_quang_cao": qc}
    return goi


# ─────────────────────────────────────────────────────────────────────────────
# CHẠY
# ─────────────────────────────────────────────────────────────────────────────

def chon_nghe(d: Dict, toi_da: int = SO_NGHE) -> List:
    """Video nổi bật để nghe lời mở đầu, chia đều các nền (mỗi nền lần lượt một video, hệ số cao trước)."""
    hang = {n: sorted([b for b in v.get("bai") or [] if b["dinh_dang"] in ("video", "short") and b["he_so"] > 1 and not b.get("co_chay_qc")],
                      key=lambda b: -b["he_so"]) for n, v in d["nen"].items() if v.get("tt") == "xong"}
    ra: List = []
    while len(ra) < toi_da and any(hang.values()):
        for n in list(hang):
            if hang[n] and len(ra) < toi_da:
                ra.append((n, hang[n].pop(0)))
    return ra


def tao(ten: str, tiktok: str = "", youtube: str = "", facebook: str = "", san_pham: str = "", so_bai: int = 30,
        nghe: bool = True) -> Dict:
    import nguon_quang_cao as nq
    ten = " ".join((ten or "").split())[:80]
    kenh = {"tiktok": nq.ten_kenh_tiktok(tiktok) if (tiktok or "").strip() else "",
            "youtube": chuan_youtube(youtube) if (youtube or "").strip() else "",
            "facebook": chuan_facebook(facebook) if (facebook or "").strip() else ""}
    if not any(kenh.values()):
        raise ValueError("Nhập ít nhất một kênh: TikTok, YouTube hoặc fanpage Facebook.")
    if not ten:
        ten = kenh["tiktok"] or kenh["facebook"].rsplit("/", 1)[-1] or kenh["youtube"].rsplit("/", 1)[-1].lstrip("@")
    so_bai = int(so_bai) if int(so_bai or 0) in SO_BAI else 30
    d = {"id": time.strftime("sr_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4], "tao_luc": time.time(), "ten": ten, "kenh": kenh,
         "san_pham": (san_pham or "").strip()[:300], "so_bai": so_bai, "nghe": bool(nghe), "buoc": "chay", "loi": "", "job_id": "",
         "nen": {n: {"tt": "cho" if kenh[n] else "bo_qua", "loi": ""} for n in NEN}, "phan_tich": None}
    _ghi(d)
    ma = d["id"]

    def lam(viec: Dict) -> None:
        huy = lambda: bool(viec.get("cancel"))
        viec_ds = [n for n in NEN if kenh[n]]
        for k, n in enumerate(viec_ds):
            if huy():
                break
            lf.update_job(viec, step=f"Đang đọc {TEN_NEN[n]}", progress=int(3 + 50 * k / len(viec_ds)))
            bao = lambda chu: lf.update_job(viec, step=chu)
            _sua_nen(ma, n, tt="chay")
            try:
                if n == "tiktok":
                    kq = lay_tiktok(kenh[n], so_bai, bao=bao)
                elif n == "youtube":
                    kq = lay_youtube(kenh[n], so_bai, bao=bao, huy=huy)
                else:
                    kq = lay_facebook(kenh[n], so_bai, bao=bao, huy=huy)
                bai = cham_diem(kq["bai"])
                _sua_nen(ma, n, tt="xong", kenh=kq["kenh"], bai=bai, tom=tom_nen(kq["kenh"], bai))
            except Exception as e:  # noqa: BLE001 - một nền hỏng không kéo cả lượt
                _sua_nen(ma, n, tt="loi", loi=str(e)[:300])
        if huy():
            _sua(ma, buoc="loi", loi="Đã dừng.")
            return
        x = _doc(ma)
        if not any(v["tt"] == "xong" for v in x["nen"].values()):
            _sua(ma, buoc="loi", loi="Không đọc được kênh nào. Xem lỗi từng kênh ở trên.")
            return
        if x["nghe"]:
            video = chon_nghe(x)
            hong: Counter = Counter()     # nền hỏng 2 lần thì bỏ các video còn lại của nền đó (YouTube hay đòi đăng nhập khi gọi dồn)
            for i, (n, b) in enumerate(video):
                if huy():
                    break
                if hong[n] >= 2:
                    continue
                lf.update_job(viec, step=f"Đang nghe lời mở đầu video nổi bật {i + 1}/{len(video)}", progress=int(55 + 25 * i / max(1, len(video))))
                loi = nghe_dau(b["link"], truc_tiep=b.get("video") or "")
                if not loi:
                    hong[n] += 1
                    continue
                with _khoa:
                    y = _doc(ma)
                    for bb in y["nen"][n]["bai"]:
                        if bb["id"] == b["id"]:
                            bb["loi_mo_dau"] = loi
                    _ghi(y)
        lf.update_job(viec, step="AI đang phân tích", progress=85)
        kq = lf.ask_json(_HE, json.dumps(_goi_ai(_doc(ma)), ensure_ascii=False), lay_cai_dat_ai() or {}, max_tokens=6000)
        if not kq.get("success"):
            _sua(ma, buoc="loi", loi="Đã đọc xong các kênh nhưng AI chưa phân tích được: " + str(kq.get("error"))[:200])
            return
        _sua(ma, phan_tich=kq.get("data") or {}, buoc="xong", loi="", xong_luc=time.time())
        lf.update_job(viec, step="Xong", progress=100)

    def bo(viec: Dict) -> None:
        try:
            lam(viec)
        except Exception as e:  # noqa: BLE001
            _sua(ma, buoc="loi", loi=str(e)[:400])
            raise

    return _sua(ma, job_id=lf.start_job("sr_soi", bo))


def csv_bai(d: Dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Nền", "Ngày đăng", "Định dạng", "Lượt xem", "Thích/cảm xúc", "Bình luận", "Chia sẻ", "Lưu", "Giây", "Hệ số", "Nổ",
                "Nội dung", "Lời mở đầu", "Link"])
    for n, v in d["nen"].items():
        for b in v.get("bai") or []:
            w.writerow([TEN_NEN[n], time.strftime("%Y-%m-%d %H:%M", _gio(b["luc"])) if b["luc"] else "", b["dinh_dang"],
                        "" if b.get("xem") is None else b["xem"], b.get("thich") if b.get("thich") is not None else "", b.get("binh_luan") or 0,
                        "" if b.get("chia_se") is None else b["chia_se"], "" if b.get("luu") is None else b["luu"], b.get("giay") or "",
                        b["he_so"], "có" if b["no"] else "", (b.get("chu") or "").replace("\n", " "), b.get("loi_mo_dau") or "", b["link"]])
    return "﻿" + out.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _loi(msg: str, ma: int = 400):
    return jsonify({"ok": False, "error": msg}), ma


@bp.get("/api/social/phien")
def api_phien():
    dang = [d["job_id"] for d in danh_sach() if (lf.get_job(d.get("job_id") or "") or {}).get("status") == "running"]
    return jsonify({"ok": True, "job_id": dang[0] if dang else ""})


@bp.get("/api/social/luot")
def api_ds():
    return jsonify({"ok": True, "luot": [ban_gui(d, gon=True) for d in danh_sach()[:30]]})


@bp.get("/api/social/luot/<ma>")
def api_mot(ma):
    d = _doc(ma)
    return jsonify({"ok": True, "luot": ban_gui(d)}) if d else _loi("Không có lượt soi này.", 404)


@bp.post("/api/social/luot")
def api_tao():
    t = request.get_json(silent=True) or {}
    try:
        d = tao(t.get("ten") or "", t.get("tiktok") or "", t.get("youtube") or "", t.get("facebook") or "", t.get("san_pham") or "",
                t.get("so_bai") or 30, bool(t.get("nghe", True)))
    except ValueError as e:
        return _loi(str(e))
    return jsonify({"ok": True, "luot": ban_gui(d)})


@bp.post("/api/social/luot/<ma>/dung")
def api_dung(ma):
    d = _doc(ma)
    return jsonify({"ok": bool(d and d.get("job_id") and lf.cancel_job(d["job_id"]))})


@bp.delete("/api/social/luot/<ma>")
def api_xoa(ma):
    d = _doc(ma)
    if not d:
        return _loi("Không có lượt soi này.", 404)
    if (lf.get_job(d.get("job_id") or "") or {}).get("status") == "running":
        return _loi("Đang chạy, dừng trước rồi mới xoá.")
    os.remove(_duong(ma))
    return jsonify({"ok": True})


@bp.get("/api/social/luot/<ma>/csv")
def api_csv(ma):
    d = _doc(ma)
    if not d:
        return _loi("Không có lượt soi này.", 404)
    ten = re.sub(r"[^\w\-]+", "_", d["ten"])[:40] or "social"
    return Response(csv_bai(d), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=social_{ten}.csv"})
