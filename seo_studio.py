"""
SEO STUDIO (21/10/2026): ba việc SEO làm được mà không cần công cụ trả phí.

1. SITE AUDIT: bò tối đa N trang của một trang web (tôn trọng robots.txt, đọc sitemap), kiểm từng trang theo các luật SEO
   kỹ thuật chuẩn (tiêu đề, mô tả, H1, canonical, noindex, chuyển hướng, link hỏng, ảnh thiếu alt, nội dung mỏng, dữ liệu
   có cấu trúc, trùng lặp...), rồi chạy Lighthouse trên máy (cùng bộ đo của PageSpeed Insights) cho trang chủ và vài trang
   chính. AI viết kế hoạch sửa theo thứ tự ưu tiên.
2. KEYWORD RESEARCH: một từ khoá gốc -> mở rộng bằng gợi ý tìm kiếm của Google và YouTube (bảng chữ cái, từ hỏi) -> AI
   gom cụm theo chủ đề và ý định tìm kiếm, gợi ý loại trang nên làm. Nút "Viết brief" ra dàn bài chuẩn SEO cho một cụm.
   KHÔNG có lượng tìm kiếm tháng: Google chỉ cho số đó qua Keyword Planner (cần tài khoản Google Ads). Tool chỉ ghi số lần
   từ khoá xuất hiện trong gợi ý và vị trí của nó, không bịa số.
3. SO VỚI ĐỐI THỦ: bò nhẹ (tiêu đề, H1) trang của mình và trang đối thủ, AI tìm chủ đề đối thủ có mà mình chưa có.

Không làm: kiểm thứ hạng trên Google (Google không có API công khai cho việc này; Custom Search JSON API đóng với khách mới
và tắt hẳn 01/01/2027; cào trang kết quả Google thì gặp CAPTCHA và trái điều khoản). Thứ hạng thật của trang mình xem bằng
Google Search Console.

Máy chủ: `/api/seo/*`. Trang: `static/seo_studio.js` + `.css`. Bài kiểm: `tests/test_seo_studio.py`.
Dữ liệu: `downloads/seo/<mã>.json`. Việc nền tiền tố `seo_`. Lighthouse: `cong_cu_ngoai/lighthouse` (npm install).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

from flask import Blueprint, jsonify, request

import longform_studio as lf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "seo")
LH_DIR = os.path.join(BASE_DIR, "cong_cu_ngoai", "lighthouse")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 AlexStudioSEO/1.0"
TRANG_TOI_DA = 500

lay_cai_dat_ai: Callable[[], Dict] = lambda: {}
_khoa = threading.RLock()
bp = Blueprint("seo_studio", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# LƯU
# ─────────────────────────────────────────────────────────────────────────────

def _ma_hop_le(ma: str) -> bool:
    return bool(re.fullmatch(r"seo_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", ma or ""))


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
            raise ValueError("Không có mục này.")
        d.update(gia_tri)
        _ghi(d)
        return d


def _moi(loai: str, **gia_tri) -> Dict:
    d = {"id": time.strftime("seo_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4], "loai": loai, "tao_luc": time.time(),
         "buoc": "chay", "loi": "", "job_id": "", **gia_tri}
    _ghi(d)
    return d


def danh_sach(loai: str = "") -> List[Dict]:
    if not os.path.isdir(KHO):
        return []
    ra = [d for d in (_doc(f[:-5]) for f in os.listdir(KHO) if f.endswith(".json")) if d and (not loai or d["loai"] == loai)]
    ra.sort(key=lambda d: d.get("tao_luc") or 0, reverse=True)
    return ra


def ban_gui(d: Dict, gon: bool = False) -> Dict:
    v = lf.get_job(d.get("job_id") or "") or {}
    chay = v.get("status") == "running"
    ra = {**d, "dang_chay": chay, "tien_do": v.get("progress") or 0, "dang_lam": v.get("step") or ""}
    if not chay and d.get("buoc") == "chay":
        ra["buoc"], ra["loi"] = "loi", d.get("loi") or "Tool khởi động lại giữa chừng. Chạy lại."
    if gon:
        for k in ("trang", "van_de", "lighthouse", "cum", "tu_khoa", "goi_y", "minh", "doi_thu", "brief"):
            ra.pop(k, None)
    return ra


def _chay_nen(d: Dict, kind: str, lam: Callable[[Dict], Dict]) -> Dict:
    def bo(viec: Dict) -> None:
        try:
            kq = lam(viec) or {}
            _sua(d["id"], buoc="xong", loi="", xong_luc=time.time(), **kq)
            lf.update_job(viec, step="Xong", progress=100)
        except Exception as e:  # noqa: BLE001
            _sua(d["id"], buoc="loi", loi=str(e)[:400])
            raise
    return _sua(d["id"], job_id=lf.start_job(kind, bo))


# ─────────────────────────────────────────────────────────────────────────────
# BÒ TRANG
# ─────────────────────────────────────────────────────────────────────────────

def chuan_dia_chi(dia_chi: str) -> str:
    """'cocoon.vn' / 'https://cocoon.vn/abc' -> 'https://cocoon.vn/'. Báo lỗi nếu không phải tên miền."""
    s = (dia_chi or "").strip()
    if not s:
        raise ValueError("Nhập địa chỉ trang web, ví dụ cocoonvietnam.com.")
    if not re.match(r"^https?://", s):
        s = "https://" + s
    u = urlparse(s)
    if not u.netloc or "." not in u.netloc or re.search(r"[\s<>\"']", u.netloc):
        raise ValueError("Địa chỉ trang web không hợp lệ.")
    if u.hostname in ("localhost", "127.0.0.1") or re.fullmatch(r"[\d.]+", u.hostname or ""):
        raise ValueError("Chỉ kiểm trang web công khai có tên miền.")
    # Tên miền trỏ vào mạng nội bộ (máy trong nhà, router): không bò, kẻo tool bị dùng để dò mạng của người dùng.
    import ipaddress
    import socket
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(u.hostname))
    except (OSError, ValueError):
        raise ValueError(f"Không tìm thấy tên miền {u.hostname}.")
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("Tên miền này trỏ vào mạng nội bộ, không kiểm.")
    return f"{u.scheme}://{u.netloc}/"


def _cung_web(a: str, b: str) -> bool:
    ha, hb = (urlparse(a).hostname or "").lower(), (urlparse(b).hostname or "").lower()
    return ha.removeprefix("www.") == hb.removeprefix("www.")


class Robots:
    """
    Đọc robots.txt theo cách Google đọc (RFC 9309): nhóm theo User-agent, ký tự * và $, luật dài nhất thắng, Allow thắng
    khi dài bằng nhau. urllib.robotparser của Python 3.9 không hiểu * nên bỏ sót luật kiểu "Disallow: *utm_source".
    """
    def __init__(self, chu: str = ""):
        self.nhom: Dict[str, List[Tuple[bool, str]]] = {}
        ua_dang, vua_ua = [], False
        for dong in (chu or "").splitlines():
            dong = dong.split("#", 1)[0].strip()
            if ":" not in dong:
                continue
            k, v = (x.strip() for x in dong.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if not vua_ua:
                    ua_dang = []
                ua_dang.append(v.lower())
                for ua in ua_dang:
                    self.nhom.setdefault(ua, [])
                vua_ua = True
            elif k in ("allow", "disallow"):
                vua_ua = False
                if v or k == "allow":
                    for ua in ua_dang:
                        self.nhom.setdefault(ua, []).append((k == "allow", v))

    @staticmethod
    def _khop(mau: str, duong: str) -> bool:
        bt = re.escape(mau).replace(r"\*", ".*")
        if bt.endswith(r"\$"):
            bt = bt[:-2] + "$"
        return re.match(bt, duong) is not None

    def cho_phep(self, url: str, ua: str = "*") -> bool:
        p = urlparse(url)
        duong = (p.path or "/") + (f"?{p.query}" if p.query else "")
        ua = ua.lower()
        luat = next((v for k, v in self.nhom.items() if k != "*" and k in ua), None)
        luat = self.nhom.get("*", []) if luat is None else luat
        tot = None
        for cho, mau in luat:
            if mau and self._khop(mau, duong):
                if tot is None or len(mau) > len(tot[1]) or (len(mau) == len(tot[1]) and cho):
                    tot = (cho, mau)
        return True if tot is None else tot[0]


_BAM = re.compile(r"^(utm_[a-z]+|fbclid|gclid|gad_source|gbraid|wbraid|_ga|mc_[a-z]+|zarsrc|ref)$", re.I)


def _sach_url(u: str) -> str:
    """Bỏ #, bỏ tham số theo dõi (utm, fbclid, gclid...), bỏ file không phải trang."""
    from urllib.parse import parse_qsl, urlencode
    u = urldefrag(u)[0]
    p = urlparse(u)
    if p.query:
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _BAM.match(k)]
        u = p._replace(query=urlencode(q)).geturl()
        p = urlparse(u)
    if p.scheme not in ("http", "https"):
        return ""
    # /cdn-cgi/: đường dẫn nội bộ của Cloudflare (bảo vệ email, thử thách bot), không phải trang. Đo 21/10/2026: cocoonvietnam.com
    # có 37 link "email-protection" trả 404 cho máy, tính là link hỏng thì báo nhầm.
    if p.path.startswith("/cdn-cgi/"):
        return ""
    if re.search(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp4|mp3|webm|ico|css|js|xml|txt|docx?|xlsx?)$", p.path, re.I):
        return ""
    return u


def doc_trang(html: str, url: str) -> Dict:
    """Bóc các yếu tố SEO của một trang HTML."""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "html.parser")
    meta = lambda ten: next((m.get("content", "").strip() for m in s.find_all("meta") if (m.get("name") or m.get("property") or "").lower() == ten), None)
    canonical = next((l.get("href", "").strip() for l in s.find_all("link") if "canonical" in [x.lower() for x in (l.get("rel") or [])]), "")
    schema = []
    for sc in s.find_all("script", type="application/ld+json"):
        try:
            j = json.loads(sc.string or "{}")
            for x in (j if isinstance(j, list) else j.get("@graph", [j]) if isinstance(j, dict) else []):
                t = x.get("@type") if isinstance(x, dict) else None
                schema += t if isinstance(t, list) else [t] if t else []
        except Exception:  # noqa: BLE001
            schema.append("(JSON-LD lỗi)")
    lien_ket, ngoai, nofollow = [], 0, 0
    for a in s.find_all("a", href=True):
        dich = _sach_url(urljoin(url, a["href"].strip()))
        if not dich:
            continue
        if "nofollow" in (a.get("rel") or []):
            nofollow += 1
        if _cung_web(dich, url):
            lien_ket.append(dich)
        else:
            ngoai += 1
    anh = s.find_all("img")
    for x in s(["script", "style", "noscript", "svg", "template"]):
        x.decompose()
    chu = " ".join(s.get_text(" ").split())
    tieu_de = (s.title.string or "").strip() if s.title and s.title.string else ""
    return {
        "tieu_de": " ".join(tieu_de.split()), "mo_ta": meta("description"), "robots": (meta("robots") or "").lower(),
        "canonical": urljoin(url, canonical) if canonical else "", "h1": [" ".join(h.get_text(" ").split())[:200] for h in s.find_all("h1")],
        "so_h2": len(s.find_all("h2")), "so_chu": len(chu.split()), "lang": (s.html.get("lang") if s.html else "") or "",
        "viewport": meta("viewport") is not None, "og_anh": bool(meta("og:image")), "schema": sorted({str(x) for x in schema}),
        "so_anh": len(anh), "anh_thieu_alt": sum(1 for i in anh if not (i.get("alt") or "").strip()),
        "lien_ket": sorted(set(lien_ket)), "link_ngoai": ngoai, "nofollow": nofollow,
        "hreflang": sum(1 for l in s.find_all("link", hreflang=True)),
    }


def doc_sitemap(phien, goc: str, robots_txt: str, toi_da: int = 2000, hong: Optional[List[str]] = None) -> List[str]:
    """URL trong sitemap (đọc từ robots.txt hoặc /sitemap.xml), đi sâu tối đa 2 tầng sitemap index. Sitemap lỗi ghi vào `hong`."""
    from bs4 import BeautifulSoup
    hang = [l.split(":", 1)[1].strip() for l in robots_txt.splitlines() if l.lower().startswith("sitemap:")] or [urljoin(goc, "/sitemap.xml")]
    ra, da = [], set()
    tang = 0
    while hang and tang < 3 and len(ra) < toi_da:
        tiep = []
        for sm in hang[:20]:
            if sm in da:
                continue
            da.add(sm)
            try:
                r = phien.get(sm, timeout=20)
                if r.status_code != 200:
                    if hong is not None:
                        hong.append(f"{sm} ({r.status_code})")
                    continue
                x = BeautifulSoup(r.content, "html.parser")
            except Exception:  # noqa: BLE001
                continue
            for loc in x.find_all("loc"):
                u = (loc.text or "").strip()
                (tiep if loc.parent and loc.parent.name == "sitemap" else ra).append(u)
        hang, tang = tiep, tang + 1
    return [u for u in dict.fromkeys(ra) if _cung_web(u, goc)][:toi_da]


def bo_trang(goc: str, toi_da: int = 100, bao=None, huy=None, nhe: bool = False, dung: Optional[bool] = None) -> Dict:
    """
    Bò trang web từ trang chủ và sitemap, tối đa `toi_da` trang cùng tên miền, 4 luồng, tôn trọng robots.txt.
    `nhe`: chỉ lấy tiêu đề/H1 (dùng cho So với đối thủ). `dung`: dựng bằng Chrome (None = tự quyết).
    Trả về {"goc", "co_robots", "sitemap", "sitemap_hong", "dung_js", "trang": [...]}.
    """
    import requests
    phien = requests.Session()
    phien.headers.update({"User-Agent": UA, "Accept-Language": "vi,en;q=0.8"})
    try:
        r0 = phien.get(goc, timeout=20, allow_redirects=True)
        goc = f"{urlparse(r0.url).scheme}://{urlparse(r0.url).netloc}/"     # http -> https, thêm/bỏ www theo trang thật
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Không mở được {goc}: {str(e)[:120]}")
    robots_txt, co_robots = "", False
    try:
        rr = phien.get(urljoin(goc, "/robots.txt"), timeout=15)
        if rr.status_code == 200 and "text" in rr.headers.get("content-type", "text"):
            robots_txt, co_robots = rr.text, True
    except Exception:  # noqa: BLE001
        pass
    rp = Robots(robots_txt)
    sitemap_hong: List[str] = []
    sitemap = doc_sitemap(phien, goc, robots_txt, hong=sitemap_hong)
    # Trang dựng nội dung bằng JavaScript (Next.js, React...): HTML thô gần như rỗng. Googlebot chạy JavaScript rồi mới
    # đọc, nên tool cũng dựng bằng Chrome ẩn. Đo 21/10/2026: trang chủ hasaki.vn thô chỉ 48 chữ, 1 link.
    if dung is None:
        try:
            tho = doc_trang(r0.text, r0.url)
            dung = tho["so_chu"] < 150 or len(tho["lien_ket"]) < 5
        except Exception:  # noqa: BLE001
            dung = False
    hang = deque([goc] + sitemap[: toi_da * 2])
    da_xep = set(hang)
    trang: Dict[str, Dict] = {}
    khoa = threading.Lock()

    def mot(u: str) -> Optional[Dict]:
        if not (rp.cho_phep(u, UA)):
            return {"url": u, "ma": 0, "chan_robots": True}
        bd = time.time()
        try:
            r = phien.get(u, timeout=20, allow_redirects=True)
        except Exception as e:  # noqa: BLE001
            return {"url": u, "ma": -1, "loi": str(e)[:100]}
        x = {"url": u, "googlebot_chan": not rp.cho_phep(u, "Googlebot"), "ma": r.status_code, "url_cuoi": r.url, "so_chuyen": len(r.history),
             "giay": round(time.time() - bd, 2), "kb": round(len(r.content) / 1024),
             "loai": r.headers.get("content-type", "").split(";")[0], "x_robots": r.headers.get("x-robots-tag", "").lower()}
        if r.status_code == 200 and "html" in x["loai"]:
            try:
                x.update(doc_trang(r.text, r.url))
            except Exception as e:  # noqa: BLE001
                x["loi"] = "Không đọc được HTML: " + str(e)[:80]
        return x

    if dung:
        _bo_bang_chrome(goc, toi_da, hang, da_xep, trang, rp, bao, huy)
        return {"goc": goc, "co_robots": co_robots, "sitemap": len(sitemap), "sitemap_url": set(sitemap),
                "sitemap_hong": sitemap_hong, "dung_js": True, "trang": list(trang.values())}
    with ThreadPoolExecutor(max_workers=4) as ex:
        while hang and len(trang) < toi_da:
            if huy and huy():
                break
            lo = []
            while hang and len(lo) < 8 and len(trang) + len(lo) < toi_da:
                lo.append(hang.popleft())
            for x in ex.map(mot, lo):
                if not x:
                    continue
                with khoa:
                    trang[x["url"]] = x
                    for l in x.get("lien_ket") or []:
                        if l not in da_xep and len(da_xep) < toi_da * 4:
                            da_xep.add(l)
                            hang.append(l)
            if bao:
                bao(len(trang))
            time.sleep(0.2)
    return {"goc": goc, "co_robots": co_robots, "sitemap": len(sitemap), "sitemap_url": set(sitemap),
            "sitemap_hong": sitemap_hong, "dung_js": False, "trang": list(trang.values())}


def _bo_bang_chrome(goc, toi_da, hang, da_xep, trang, rp, bao, huy) -> None:
    """Bò bằng Chrome ẩn (chạy JavaScript như Googlebot), lần lượt từng trang, khoảng 2-4 giây một trang."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        tr = p.chromium.launch(channel="chrome", headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = tr.new_context(user_agent=UA, locale="vi-VN", viewport={"width": 412, "height": 915}, is_mobile=True)
            ctx.route("**/*", lambda rt: rt.abort() if rt.request.resource_type in ("image", "media", "font") else rt.continue_())
            pg = ctx.new_page()
            while hang and len(trang) < toi_da:
                if huy and huy():
                    break
                u = hang.popleft()
                if not (rp.cho_phep(u, UA)):
                    trang[u] = {"url": u, "ma": 0, "chan_robots": True}
                    continue
                bd = time.time()
                try:
                    res = pg.goto(u, wait_until="domcontentloaded", timeout=30000)
                    try:
                        pg.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:  # noqa: BLE001
                        pass
                    html = pg.content()
                    x = {"url": u, "googlebot_chan": not rp.cho_phep(u, "Googlebot"), "ma": res.status if res else 0, "url_cuoi": pg.url, "so_chuyen": 0 if pg.url.rstrip("/") == u.rstrip("/") else 1,
                         "giay": round(time.time() - bd, 2), "kb": round(len(html) / 1024), "loai": "text/html",
                         "x_robots": ((res.headers.get("x-robots-tag") if res else "") or "").lower()}
                    if x["ma"] == 200:
                        x.update(doc_trang(html, pg.url))
                        # Thời gian dựng bằng Chrome không phải thời gian máy chủ trả trang: không dùng cho luật "chậm".
                        x["giay"] = None
                except Exception as e:  # noqa: BLE001
                    x = {"url": u, "ma": -1, "loi": str(e)[:100]}
                trang[u] = x
                for l in x.get("lien_ket") or []:
                    if l not in da_xep and len(da_xep) < toi_da * 4:
                        da_xep.add(l)
                        hang.append(l)
                if bao:
                    bao(len(trang))
        finally:
            tr.close()


# ─────────────────────────────────────────────────────────────────────────────
# LUẬT KIỂM (mỗi luật: mức, tên, cách sửa)
# ─────────────────────────────────────────────────────────────────────────────

LUAT = {
    "loi_4xx_5xx": ("cao", "Trang lỗi (4xx, 5xx)", "Sửa hoặc chuyển hướng 301 về trang đúng; bỏ link nội bộ trỏ tới trang lỗi."),
    "link_hong": ("cao", "Link nội bộ trỏ tới trang lỗi", "Sửa link trong các trang nguồn cho trỏ đúng trang còn sống."),
    "noindex": ("cao", "Trang bị chặn lập chỉ mục (noindex)", "Nếu trang cần lên Google thì bỏ thẻ noindex (meta robots hoặc X-Robots-Tag)."),
    "thieu_tieu_de": ("cao", "Thiếu thẻ tiêu đề (title)", "Mỗi trang cần một title riêng, 30-60 ký tự, có từ khoá chính ở đầu."),
    "trung_tieu_de": ("cao", "Tiêu đề trùng nhau giữa các trang", "Viết title riêng cho từng trang để Google hiểu mỗi trang nói về gì."),
    "thieu_h1": ("vua", "Thiếu H1", "Mỗi trang cần đúng một H1 nói rõ chủ đề trang."),
    "nhieu_h1": ("thap", "Nhiều H1 trên một trang", "Giữ một H1, các tiêu đề phụ chuyển thành H2/H3."),
    "tieu_de_dai": ("vua", "Tiêu đề dài quá 60 ký tự", "Google cắt tiêu đề dài trên kết quả tìm kiếm; rút còn 30-60 ký tự."),
    "tieu_de_ngan": ("thap", "Tiêu đề ngắn dưới 30 ký tự", "Thêm từ khoá và lợi ích để tiêu đề đủ hấp dẫn."),
    "thieu_mo_ta": ("vua", "Thiếu mô tả (meta description)", "Viết mô tả 70-160 ký tự, có từ khoá và lời mời bấm vào."),
    "trung_mo_ta": ("vua", "Mô tả trùng nhau giữa các trang", "Viết mô tả riêng cho từng trang."),
    "mo_ta_dai": ("thap", "Mô tả dài quá 160 ký tự", "Rút gọn còn 70-160 ký tự để không bị cắt."),
    "noi_dung_mong": ("vua", "Nội dung mỏng (dưới 300 chữ)", "Trang cần đủ nội dung trả lời câu hỏi người tìm; gộp hoặc viết thêm."),
    "canonical_khac": ("vua", "Canonical trỏ sang trang khác", "Kiểm lại: canonical sai làm trang này không được xếp hạng."),
    "thieu_canonical": ("thap", "Thiếu thẻ canonical", "Thêm canonical tự trỏ về chính trang để tránh trùng lặp do tham số URL."),
    "chuyen_huong_chuoi": ("vua", "Chuyển hướng nhiều bước", "Trỏ thẳng về đích cuối, một lần chuyển 301 là đủ."),
    "cham": ("vua", "Máy chủ trả trang chậm (trên 1,5 giây)", "Bật cache, CDN, giảm truy vấn; Google tính tốc độ vào xếp hạng."),
    "anh_thieu_alt": ("thap", "Ảnh thiếu chữ thay thế (alt)", "Thêm alt mô tả ảnh: giúp Google Ảnh và người khiếm thị."),
    "thieu_viewport": ("cao", "Thiếu thẻ viewport (không thân thiện điện thoại)", "Thêm <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">."),
    "thieu_lang": ("thap", "Thiếu khai báo ngôn ngữ (lang)", "Thêm <html lang=\"vi\">."),
    "thieu_og": ("thap", "Thiếu ảnh chia sẻ (og:image)", "Thêm og:image để link chia sẻ Facebook, Zalo có ảnh đẹp."),
    "thieu_schema": ("thap", "Trang chủ chưa có dữ liệu có cấu trúc", "Thêm JSON-LD Organization/WebSite (và Product, Article ở trang tương ứng)."),
    "khong_sitemap": ("vua", "Không tìm thấy sitemap", "Tạo sitemap.xml và khai báo trong robots.txt cùng Google Search Console."),
    "sitemap_hong": ("vua", "Sitemap khai trong robots.txt bị lỗi", "Sửa đường dẫn sitemap trong robots.txt hoặc làm cho sitemap đó trả về được."),
    "khong_robots": ("thap", "Không có robots.txt", "Thêm robots.txt, ghi đường dẫn sitemap."),
    "khong_https": ("cao", "Trang chạy http, chưa https", "Cài chứng chỉ SSL và chuyển hướng 301 toàn bộ sang https."),
    "html_nang": ("thap", "HTML nặng trên 1 MB", "Bớt mã nhúng trong trang, tải lười phần dưới."),
    "chan_robots": ("thap", "Trang bị robots.txt chặn", "Bình thường nếu cố ý; nếu trang cần lên Google thì mở chặn."),
}
DIEM = {"cao": 6, "vua": 3, "thap": 1}


def _khong_tham_so(u: str) -> str:
    """Canonical trỏ về chính trang bỏ tham số (?page, ?sort...) là đúng chuẩn, không tính là trỏ sang trang khác."""
    p = urlparse(urldefrag(u)[0])
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}"


def kiem(ket: Dict) -> Tuple[List[Dict], int]:
    """Áp các luật lên kết quả bò trang. Trả về (danh sách vấn đề, điểm 0-100)."""
    trang = ket["trang"]
    html = [t for t in trang if t.get("ma") == 200 and "html" in (t.get("loai") or "") and "tieu_de" in t]
    chi_muc = [t for t in html if "noindex" not in (t.get("robots") or "") + (t.get("x_robots") or "")]
    van: Dict[str, List[str]] = {k: [] for k in LUAT}
    them = lambda k, u: van[k].append(u)
    for t in trang:
        if t.get("chan_robots") or t.get("googlebot_chan"):
            them("chan_robots", t["url"])
        elif isinstance(t.get("ma"), int) and t["ma"] >= 400:
            them("loi_4xx_5xx", f"{t['url']} ({t['ma']})")
        if (t.get("so_chuyen") or 0) > 1:
            them("chuyen_huong_chuoi", t["url"])
        if (t.get("giay") or 0) > 1.5:
            them("cham", f"{t['url']} ({t['giay']} giây)")
        if (t.get("kb") or 0) > 1024:
            them("html_nang", f"{t['url']} ({t['kb']} KB)")
    hong = {t["url"] for t in trang if isinstance(t.get("ma"), int) and t["ma"] >= 400}
    for t in html:
        for l in t.get("lien_ket") or []:
            if l in hong:
                them("link_hong", f"{t['url']} -> {l}")
        if "noindex" in (t.get("robots") or "") + (t.get("x_robots") or ""):
            them("noindex", t["url"])
    for t in chi_muc:
        u, td, mt = t["url"], t.get("tieu_de") or "", t.get("mo_ta")
        if not td:
            them("thieu_tieu_de", u)
        elif len(td) > 60:
            them("tieu_de_dai", f"{u} ({len(td)} ký tự)")
        elif len(td) < 30:
            them("tieu_de_ngan", f"{u} ({len(td)} ký tự)")
        if not mt:
            them("thieu_mo_ta", u)
        elif len(mt) > 160:
            them("mo_ta_dai", f"{u} ({len(mt)} ký tự)")
        if not t.get("h1"):
            them("thieu_h1", u)
        elif len(t["h1"]) > 1:
            them("nhieu_h1", f"{u} ({len(t['h1'])} H1)")
        if (t.get("so_chu") or 0) < 300:
            them("noi_dung_mong", f"{u} ({t.get('so_chu') or 0} chữ)")
        cn = t.get("canonical") or ""
        if not cn:
            them("thieu_canonical", u)
        elif _khong_tham_so(cn) != _khong_tham_so(t.get("url_cuoi") or u):
            them("canonical_khac", f"{u} -> {cn}")
        if t.get("anh_thieu_alt"):
            them("anh_thieu_alt", f"{u} ({t['anh_thieu_alt']}/{t['so_anh']} ảnh)")
        if not t.get("viewport"):
            them("thieu_viewport", u)
        if not t.get("lang"):
            them("thieu_lang", u)
        if not t.get("og_anh"):
            them("thieu_og", u)
    for k, tr in (("trung_tieu_de", "tieu_de"), ("trung_mo_ta", "mo_ta")):
        dem = Counter((t.get(tr) or "").strip().lower() for t in chi_muc if (t.get(tr) or "").strip())
        for gia, n in dem.items():
            if n > 1:
                them(k, f"\"{gia[:80]}\" ({n} trang)")
    trang_chu = next((t for t in html if t["url"].rstrip("/") == ket["goc"].rstrip("/")), html[0] if html else None)
    if trang_chu and not trang_chu.get("schema"):
        them("thieu_schema", trang_chu["url"])
    for sm in ket.get("sitemap_hong") or []:
        them("sitemap_hong", sm)
    if not ket["sitemap"]:
        them("khong_sitemap", ket["goc"])
    if not ket["co_robots"]:
        them("khong_robots", ket["goc"])
    if ket["goc"].startswith("http://"):
        them("khong_https", ket["goc"])
    ra, tru = [], 0
    n = max(1, len(chi_muc))
    for k, ds in van.items():
        if not ds:
            continue
        muc, ten, sua = LUAT[k]
        # Trừ điểm theo mức nặng và tỉ lệ trang dính lỗi (lỗi toàn trang web tính như dính hết).
        ti_le = 1.0 if k in ("khong_sitemap", "khong_robots", "khong_https", "thieu_schema") else min(1.0, len(ds) / n)
        tru += DIEM[muc] * (0.4 + 0.6 * ti_le)
        ra.append({"ma": k, "muc": muc, "ten": ten, "sua": sua, "so": len(ds), "vi_du": ds[:25]})
    ra.sort(key=lambda v: ({"cao": 0, "vua": 1, "thap": 2}[v["muc"]], -v["so"]))
    return ra, max(0, round(100 - tru))


# ─────────────────────────────────────────────────────────────────────────────
# LIGHTHOUSE TRÊN MÁY
# ─────────────────────────────────────────────────────────────────────────────

def co_lighthouse() -> bool:
    return os.path.isfile(os.path.join(LH_DIR, "node_modules", ".bin", "lighthouse"))


def lighthouse(url: str, may: str = "mobile") -> Dict:
    """Chạy Lighthouse (cùng bộ đo PageSpeed Insights) bằng Chrome trên máy. Khoảng 20 giây một trang."""
    if not co_lighthouse():
        raise RuntimeError("Chưa cài Lighthouse. Chạy: cd cong_cu_ngoai/lighthouse && npm install")
    os.makedirs(os.path.join(KHO, "tam"), exist_ok=True)
    ra = os.path.join(KHO, "tam", uuid.uuid4().hex + ".json")
    lenh = [os.path.join(LH_DIR, "node_modules", ".bin", "lighthouse"), url, "--quiet", "--output=json", f"--output-path={ra}",
            "--only-categories=performance,seo,accessibility,best-practices", f"--form-factor={may}",
            "--chrome-flags=--headless=new --no-first-run --disable-extensions"]
    if may == "desktop":
        lenh.append("--preset=desktop")
    env = {**os.environ, "CHROME_PATH": CHROME} if os.path.isfile(CHROME) else os.environ
    try:
        kq = subprocess.run(lenh, capture_output=True, text=True, timeout=180, env=env)
        if not os.path.isfile(ra):
            raise RuntimeError("Lighthouse không chạy được: " + (kq.stderr or kq.stdout or "")[-200:])
        with open(ra, encoding="utf-8") as f:
            j = json.load(f)
    finally:
        try:
            os.remove(ra)
        except OSError:
            pass
    if j.get("runtimeError"):
        raise RuntimeError("Lighthouse: " + str(j["runtimeError"].get("message") or "")[:200])
    a = j.get("audits") or {}
    diem = {k: (round(v["score"] * 100) if v.get("score") is not None else None) for k, v in (j.get("categories") or {}).items()}
    chi_so = {k: a.get(k, {}).get("displayValue") for k in ("largest-contentful-paint", "cumulative-layout-shift",
                                                          "total-blocking-time", "first-contentful-paint", "speed-index")}
    hong = []
    for cat in ("seo", "performance", "accessibility", "best-practices"):
        for r in (j.get("categories") or {}).get(cat, {}).get("auditRefs") or []:
            x = a.get(r["id"]) or {}
            if x.get("score") is not None and x["score"] < 0.9 and x.get("scoreDisplayMode") not in ("informative", "notApplicable", "manual"):
                hong.append({"nhom": cat, "ten": x.get("title") or r["id"], "gia_tri": x.get("displayValue") or ""})
    return {"url": url, "may": may, "diem": diem, "chi_so": chi_so, "hong": hong[:25]}


# ─────────────────────────────────────────────────────────────────────────────
# 1. SITE AUDIT
# ─────────────────────────────────────────────────────────────────────────────

_HE_AUDIT = """You are a senior technical SEO consultant. You get the results of a site crawl (issue list with counts and
examples) and Lighthouse lab results. Write, in Vietnamese, for a marketer who is not a developer:
{"tom_tat": "2-3 sentences: overall state of the site and the biggest risk",
 "ke_hoach": [{"viec": "concrete action", "vi_sao": "impact on Google ranking or users", "uu_tien": "cao|vừa|thấp", "ai_lam": "marketer|developer"}]}
5-8 actions, highest impact first. Do not invent problems that are not in the data. Return ONLY JSON."""


def audit(dia_chi: str, so_trang: int = 100) -> Dict:
    goc = chuan_dia_chi(dia_chi)
    so_trang = max(10, min(TRANG_TOI_DA, int(so_trang or 100)))
    d = _moi("audit", dia_chi=goc, so_trang=so_trang)

    def lam(viec: Dict) -> Dict:
        lf.update_job(viec, step="Đang đọc robots.txt và sitemap", progress=2)
        ket = bo_trang(goc, so_trang, bao=lambda n: lf.update_job(viec, step=f"Đã đọc {n}/{so_trang} trang",
                                                                 progress=int(3 + 62 * n / so_trang)),
                       huy=lambda: bool(viec.get("cancel")))
        van_de, diem = kiem(ket)
        _sua(d["id"], van_de=van_de, diem=diem, goc_that=ket["goc"], dung_js=ket.get("dung_js"))
        html = sorted([t for t in ket["trang"] if t.get("ma") == 200 and t.get("tieu_de") is not None],
                      key=lambda t: (t["url"].rstrip("/") != ket["goc"].rstrip("/"), len(t["url"])))
        lh = []
        for k, t in enumerate(html[:3]):
            if viec.get("cancel"):
                break
            lf.update_job(viec, step=f"Lighthouse đang đo trang {k + 1}/3 (khoảng 20 giây mỗi trang)", progress=68 + 8 * k)
            try:
                lh.append(lighthouse(t["url"]))
            except Exception as e:  # noqa: BLE001
                lh.append({"url": t["url"], "loi": str(e)[:200]})
        lf.update_job(viec, step="AI đang viết kế hoạch sửa", progress=93)
        goi = {"trang_web": ket["goc"], "so_trang_doc": len(ket["trang"]), "diem": diem,
               "van_de": [{k: v[k] for k in ("muc", "ten", "so")} | {"vi_du": v["vi_du"][:3]} for v in van_de],
               "lighthouse": [{k: x.get(k) for k in ("url", "diem", "chi_so", "hong", "loi")} for x in lh]}
        kq = lf.ask_json(_HE_AUDIT, json.dumps(goi, ensure_ascii=False), lay_cai_dat_ai() or {}, max_tokens=3000)
        ai = kq.get("data") if kq.get("success") else {"tom_tat": "", "ke_hoach": [], "loi": kq.get("error")}
        trang_gon = [{k: t.get(k) for k in ("url", "ma", "tieu_de", "mo_ta", "h1", "so_chu", "giay", "canonical", "robots", "schema", "so_chuyen")}
                     for t in ket["trang"]]
        return {"trang": trang_gon, "lighthouse": lh, "ai": ai, "so_doc": len(ket["trang"]), "sitemap": ket["sitemap"]}

    return _chay_nen(d, "seo_audit", lam)


# ─────────────────────────────────────────────────────────────────────────────
# 2. KEYWORD RESEARCH
# ─────────────────────────────────────────────────────────────────────────────

DUOI_HOI = ["là gì", "cách", "có tốt không", "giá", "review", "loại nào tốt", "cho", "mua ở đâu", "so sánh", "bao nhiêu",
            "tốt nhất", "hay", "nào", "khi nào", "tại sao"]


def goi_y(q: str, nguon: str = "google", hl: str = "vi", gl: str = "vn") -> List[str]:
    """Gợi ý tìm kiếm công khai của Google (hoặc YouTube). Lỗi thì trả rỗng."""
    import requests
    p = {"client": "firefox", "hl": hl, "gl": gl, "q": q}
    if nguon == "youtube":
        p["ds"] = "yt"
    try:
        r = requests.get("https://suggestqueries.google.com/complete/search", params=p, timeout=10, headers={"User-Agent": UA})
        j = r.json()
        return [str(x) for x in j[1]] if isinstance(j, list) and len(j) > 1 else []
    except Exception:  # noqa: BLE001
        return []


def mo_rong(goc: str, bao=None, huy=None, nghi: float = 0.12) -> Dict[str, Dict]:
    """
    Mở rộng từ khoá: gốc, gốc + từ hỏi, gốc + a..z, trên Google và YouTube. Mỗi từ khoá ghi: số lần xuất hiện, vị trí tốt
    nhất trong danh sách gợi ý (1 là đầu), nguồn nào. Đây là tín hiệu phổ biến tương đối, KHÔNG phải lượng tìm kiếm.
    """
    hoi = [goc] + [f"{goc} {d}" for d in DUOI_HOI] + [f"{goc} {c}" for c in "abcdeghiklmnopqrstuvxy"]
    ra: Dict[str, Dict] = {}
    viec_ds = [(q, "google") for q in hoi] + [(q, "youtube") for q in hoi[:1 + len(DUOI_HOI)]]
    for k, (q, nguon) in enumerate(viec_ds):
        if huy and huy():
            break
        for vt, tu in enumerate(goi_y(q, nguon), 1):
            tu = " ".join(tu.lower().split())
            x = ra.setdefault(tu, {"tu": tu, "lan": 0, "vi_tri": 99, "nguon": []})
            x["lan"] += 1
            x["vi_tri"] = min(x["vi_tri"], vt)
            if nguon not in x["nguon"]:
                x["nguon"].append(nguon)
        if bao:
            bao(k + 1, len(viec_ds), len(ra))
        time.sleep(nghi)
    return ra


_HE_CUM = """You are a senior SEO strategist for the Vietnamese market. You get keywords collected from Google and YouTube
autocomplete for one seed topic, each with how many times it appeared and its best position in the suggestion list
(a relative popularity signal only, NOT search volume). Group them into topic clusters for a content plan.
Return ONLY JSON, all text in Vietnamese:
{"tom_tat": "2 sentences on what people search around this topic",
 "cum": [{"ten": "cluster name", "y_dinh": "thông tin|thương mại|mua hàng|điều hướng", "uu_tien": "cao|vừa|thấp",
          "loai_trang": "bài blog|trang danh mục|trang sản phẩm|trang so sánh|video YouTube|trang hỏi đáp",
          "tieu_de_goi_y": "suggested page title", "tu_chinh": "main keyword", "tu_phu": ["keyword", ...]}]}
Every keyword from the input must appear in exactly one cluster (tu_chinh or tu_phu). 5-15 clusters. Never invent search volumes."""

_HE_BRIEF = """You are a senior SEO content strategist for the Vietnamese market. Write a content brief for ONE page that
should rank for the given keyword cluster. All text in Vietnamese. Return ONLY JSON:
{"tieu_de": "title tag, 50-60 chars, main keyword first", "mo_ta": "meta description 140-160 chars with a call to action",
 "duong_dan": "short-url-slug-without-accents", "h1": "...", "y_dinh": "what the searcher wants to achieve",
 "dan_y": [{"h2": "...", "h3": ["...", ...], "y_chinh": "what to cover in this section"}],
 "cau_hoi": ["FAQ questions to answer (from the keywords)"], "do_dai": "recommended word count range with a reason",
 "schema": ["JSON-LD types to add"], "lien_ket_noi_bo": ["internal link ideas"], "goi_y_anh": ["image/video ideas with alt text"],
 "luu_y": ["E-E-A-T / trust signals to include"]}"""


def keyword(tu_goc: str) -> Dict:
    tu_goc = " ".join((tu_goc or "").strip().lower().split())
    if not tu_goc or len(tu_goc) > 80:
        raise ValueError("Nhập một từ khoá gốc, ví dụ: kem chống nắng.")
    d = _moi("keyword", tu_goc=tu_goc)

    def lam(viec: Dict) -> Dict:
        ds = mo_rong(tu_goc, bao=lambda k, n, c: lf.update_job(viec, step=f"Đang lấy gợi ý {k}/{n} ({c} từ khoá)",
                                                               progress=int(70 * k / n)),
                     huy=lambda: bool(viec.get("cancel")))
        if not ds:
            raise RuntimeError("Google không trả gợi ý nào cho từ khoá này.")
        tu = sorted(ds.values(), key=lambda x: (-x["lan"], x["vi_tri"]))[:220]
        lf.update_job(viec, step=f"AI đang gom {len(tu)} từ khoá thành cụm", progress=75)
        kq = lf.ask_json(_HE_CUM, json.dumps({"tu_goc": tu_goc, "tu_khoa": [[x["tu"], x["lan"], x["vi_tri"]] for x in tu]},
                                              ensure_ascii=False), lay_cai_dat_ai() or {}, max_tokens=8000)
        if not kq.get("success"):
            raise RuntimeError(kq.get("error") or "AI không trả lời.")
        a = kq.get("data") or {}
        co = {x["tu"] for x in tu}
        cum = []
        for c in a.get("cum") or []:
            if not isinstance(c, dict):
                continue
            ds_tu = [t for t in [c.get("tu_chinh")] + list(c.get("tu_phu") or []) if isinstance(t, str) and t.lower() in co]
            if ds_tu:
                cum.append({k: c.get(k) for k in ("ten", "y_dinh", "uu_tien", "loai_trang", "tieu_de_goi_y")} | {
                    "tu": [ds[t.lower()] for t in dict.fromkeys(t.lower() for t in ds_tu)]})
        da = {x["tu"] for c in cum for x in c["tu"]}
        con = [x for x in tu if x["tu"] not in da]
        if con:
            cum.append({"ten": "Chưa xếp cụm", "y_dinh": "", "uu_tien": "thấp", "loai_trang": "", "tieu_de_goi_y": "", "tu": con})
        return {"tu_khoa": tu, "cum": cum, "tom_tat": str(a.get("tom_tat") or "")[:600]}

    return _chay_nen(d, "seo_keyword", lam)


def viet_brief(ma: str, i: int) -> Dict:
    d = _doc(ma)
    if not d or d["loai"] != "keyword" or not (0 <= i < len(d.get("cum") or [])):
        raise ValueError("Không có cụm này.")
    c = d["cum"][i]
    kq = lf.ask_json(_HE_BRIEF, json.dumps({"tu_goc": d["tu_goc"], "cum": c["ten"], "y_dinh": c.get("y_dinh"),
                                            "loai_trang": c.get("loai_trang"), "tu_khoa": [x["tu"] for x in c["tu"]]},
                                           ensure_ascii=False), lay_cai_dat_ai() or {}, max_tokens=4000)
    if not kq.get("success"):
        raise RuntimeError(kq.get("error") or "AI không trả lời.")
    with _khoa:
        d = _doc(ma)
        d["cum"][i]["brief"] = kq.get("data") or {}
        _ghi(d)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# 3. SO VỚI ĐỐI THỦ
# ─────────────────────────────────────────────────────────────────────────────

_HE_GAP = """You are a senior SEO strategist. You get the page titles and H1s of MY site and a COMPETITOR site (from a
crawl). Find the content gap. All text in Vietnamese. Return ONLY JSON:
{"tom_tat": "2-3 sentences comparing the two content strategies",
 "doi_thu_manh": ["topics/page types the competitor covers well"],
 "khoang_trong": [{"chu_de": "topic the competitor has and I do not", "vi_du_doi_thu": ["their page titles"], "goi_y": "page I should create", "uu_tien": "cao|vừa|thấp"}],
 "minh_manh": ["topics I cover that they do not"]}
Base everything on the titles given; do not invent pages."""


def so_sanh(minh: str, doi_thu: str, so_trang: int = 150) -> Dict:
    a, b = chuan_dia_chi(minh), chuan_dia_chi(doi_thu)
    if _cung_web(a, b):
        raise ValueError("Hai địa chỉ là cùng một trang web.")
    so_trang = max(20, min(300, int(so_trang or 150)))
    d = _moi("gap", minh=a, doi_thu=b, so_trang=so_trang)

    def lam(viec: Dict) -> Dict:
        ra = {}
        for k, (ten, u) in enumerate((("minh", a), ("doi_thu", b))):
            ket = bo_trang(u, so_trang, nhe=True, huy=lambda: bool(viec.get("cancel")),
                           bao=lambda n, k=k: lf.update_job(viec, step=f"Đang đọc {'trang của bạn' if k == 0 else 'trang đối thủ'}: {n} trang",
                                                            progress=int(5 + 40 * k + 40 * n / so_trang)))
            ra[ten] = [{"url": t["url"], "tieu_de": t.get("tieu_de") or "", "h1": (t.get("h1") or [""])[0]}
                       for t in ket["trang"] if t.get("ma") == 200 and t.get("tieu_de") is not None]
        if not ra["minh"] or not ra["doi_thu"]:
            raise RuntimeError("Không đọc được trang nào của " + ("bạn" if not ra["minh"] else "đối thủ") + ".")
        lf.update_job(viec, step="AI đang so nội dung hai bên", progress=88)
        goi = {k: [f"{x['tieu_de']} | {x['h1']}" for x in v][:300] for k, v in ra.items()}
        kq = lf.ask_json(_HE_GAP, json.dumps(goi, ensure_ascii=False), lay_cai_dat_ai() or {}, max_tokens=6000)
        if not kq.get("success"):
            raise RuntimeError(kq.get("error") or "AI không trả lời.")
        return {**ra, "ai": kq.get("data") or {}}

    return _chay_nen(d, "seo_gap", lam)


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _loi(msg: str, ma: int = 400):
    return jsonify({"ok": False, "error": msg}), ma


@bp.get("/api/seo/phien")
def api_phien():
    dang = [d["job_id"] for d in danh_sach() if (lf.get_job(d.get("job_id") or "") or {}).get("status") == "running"]
    return jsonify({"ok": True, "job_id": dang[0] if dang else ""})


@bp.get("/api/seo/muc")
def api_ds():
    return jsonify({"ok": True, "muc": [ban_gui(d, gon=True) for d in danh_sach(request.args.get("loai") or "")[:40]],
                    "co_lighthouse": co_lighthouse()})


@bp.get("/api/seo/muc/<ma>")
def api_mot(ma):
    d = _doc(ma)
    return jsonify({"ok": True, "muc": ban_gui(d)}) if d else _loi("Không có mục này.", 404)


@bp.post("/api/seo/audit")
def api_audit():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "muc": ban_gui(audit(t.get("dia_chi") or "", t.get("so_trang") or 100))})
    except ValueError as e:
        return _loi(str(e))


@bp.post("/api/seo/keyword")
def api_keyword():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "muc": ban_gui(keyword(t.get("tu_goc") or ""))})
    except ValueError as e:
        return _loi(str(e))


@bp.post("/api/seo/muc/<ma>/brief/<int:i>")
def api_brief(ma, i):
    try:
        return jsonify({"ok": True, "muc": ban_gui(viet_brief(ma, i))})
    except ValueError as e:
        return _loi(str(e))
    except Exception as e:  # noqa: BLE001
        return _loi(str(e)[:300], 500)


@bp.post("/api/seo/gap")
def api_gap():
    t = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "muc": ban_gui(so_sanh(t.get("minh") or "", t.get("doi_thu") or "", t.get("so_trang") or 150))})
    except ValueError as e:
        return _loi(str(e))


@bp.post("/api/seo/muc/<ma>/dung")
def api_dung(ma):
    d = _doc(ma)
    return jsonify({"ok": bool(d and d.get("job_id") and lf.cancel_job(d["job_id"]))})


@bp.delete("/api/seo/muc/<ma>")
def api_xoa(ma):
    d = _doc(ma)
    if not d:
        return _loi("Không có mục này.", 404)
    if (lf.get_job(d.get("job_id") or "") or {}).get("status") == "running":
        return _loi("Đang chạy, dừng trước rồi mới xoá.")
    os.remove(_duong(ma))
    return jsonify({"ok": True})


@bp.get("/api/seo/muc/<ma>/csv")
def api_csv(ma):
    """Xuất danh sách trang (audit) hoặc từ khoá (keyword) ra CSV mở bằng Excel."""
    import csv
    import io
    from flask import Response
    d = _doc(ma)
    if not d:
        return _loi("Không có mục này.", 404)
    b = io.StringIO()
    w = csv.writer(b)
    if d["loai"] == "audit":
        w.writerow(["URL", "Mã", "Tiêu đề", "Độ dài tiêu đề", "Mô tả", "H1", "Số chữ", "Giây", "Canonical", "Robots"])
        for t in d.get("trang") or []:
            w.writerow([t.get("url"), t.get("ma"), t.get("tieu_de") or "", len(t.get("tieu_de") or ""), t.get("mo_ta") or "",
                        " | ".join(t.get("h1") or []), t.get("so_chu") or "", t.get("giay") or "", t.get("canonical") or "", t.get("robots") or ""])
    elif d["loai"] == "keyword":
        w.writerow(["Cụm", "Ý định", "Ưu tiên", "Từ khoá", "Số lần xuất hiện", "Vị trí gợi ý tốt nhất", "Nguồn"])
        for c in d.get("cum") or []:
            for x in c["tu"]:
                w.writerow([c["ten"], c.get("y_dinh") or "", c.get("uu_tien") or "", x["tu"], x["lan"], x["vi_tri"], ", ".join(x["nguon"])])
    else:
        return _loi("Mục này không xuất CSV.")
    return Response("﻿" + b.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={ma}.csv"})
