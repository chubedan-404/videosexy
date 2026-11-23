"""
Tải một video từ đường dẫn về máy (PlayAh): TikTok, YouTube, Facebook, Instagram và các trang khác bộ tải đọc được.

Dùng chung cho Kho giọng (lấy tiếng làm mẫu giọng) và Soi video mẫu (cắt khung hình). TikTok đi qua tikwm trước vì
bộ tải yt-dlp cài trong tool đã cũ, đọc trang TikTok hay hỏng; tikwm hỏng thì mới thử yt-dlp.
"""
import ipaddress
import os
import re
import socket
import urllib.parse
from typing import Dict

import requests

import xuong_chung as pc

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0 Safari/537.36")
# Video nguồn chỉ để lấy mẫu giọng hoặc cắt khung hình, không cần nét. Quá cỡ này là người dùng dán nhầm phim dài.
NANG_TOI_DA = 400 * 1024 * 1024


def la_tiktok(url: str) -> bool:
    return bool(re.search(r"https?://([\w-]+\.)*(tiktok\.com|douyin\.com)/", url or "", re.I))


def la_youtube(url: str) -> bool:
    return bool(re.search(r"https?://([\w-]+\.)*(youtube\.com|youtu\.be)/", url or "", re.I))


def ten_nen(url: str) -> str:
    may = (urllib.parse.urlparse(url or "").hostname or "").lower()
    for khoa, ten in (("tiktok", "TikTok"), ("douyin", "Douyin"), ("youtu", "YouTube"), ("facebook", "Facebook"),
                      ("fb.watch", "Facebook"), ("instagram", "Instagram"), ("threads", "Threads")):
        if khoa in may:
            return ten
    return may or "Link"


def kiem_link(url: str) -> str:
    """Trả về đường dẫn đã gọt. Chỉ nhận http(s) ra ngoài mạng: không cho tool tự gọi vào máy trong mạng nội bộ."""
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", url):
        raise ValueError("Đường dẫn chưa đúng. Dán nguyên link video, bắt đầu bằng https://")
    may = urllib.parse.urlparse(url).hostname or ""
    try:
        for dia_chi in {x[4][0] for x in socket.getaddrinfo(may, None)}:
            ip = ipaddress.ip_address(dia_chi)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise ValueError("Đường dẫn này trỏ vào mạng nội bộ, tool không tải.")
    except socket.gaierror:
        raise ValueError("Không tìm thấy trang này. Xem lại đường dẫn.")
    return url[:600]


def _tai_tep(url: str, dich: str) -> None:
    # Không gửi Referer: máy chủ video của TikTok chặn (lỗi 503) khi thấy Referer là trang khác.
    with requests.get(url, headers={"User-Agent": _UA}, stream=True, timeout=60) as tra:
        if tra.status_code != 200:
            raise RuntimeError(f"máy chủ video trả mã {tra.status_code}")
        da = 0
        with open(dich, "wb") as f:
            for mieng in tra.iter_content(1 << 18):
                da += len(mieng)
                if da > NANG_TOI_DA:
                    raise RuntimeError("video nặng quá 400 MB")
                f.write(mieng)
    if da < 20000:
        raise RuntimeError("file tải về rỗng")


def _tiktok_tikwm(url: str, thu_muc: str) -> Dict:
    loi = "không rõ"
    for may in ("www.tikwm.com", "tikwm.com"):
        try:
            tra = requests.get(f"https://{may}/api/?url={urllib.parse.quote(url, safe='')}",
                               headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=20)
            du_lieu = tra.json() if tra.status_code == 200 else {}
            d = du_lieu.get("data") or {}
            xem = d.get("play") or d.get("hdplay") or d.get("wmplay")
            if du_lieu.get("code") != 0 or not xem:
                loi = str(du_lieu.get("msg") or f"mã {tra.status_code}")[:120]
                continue
            xem = "https:" + xem if xem.startswith("//") else f"https://{may}{xem}" if xem.startswith("/") else xem
            dich = os.path.join(thu_muc, "nguon.mp4")
            _tai_tep(xem, dich)
            return {"file": dich, "ten": (d.get("title") or "").strip()[:120],
                    "tac_gia": "@" + ((d.get("author") or {}).get("unique_id") or "").strip() if (d.get("author") or {}).get("unique_id") else ""}
        except Exception as exc:  # noqa: BLE001
            loi = str(exc)[:120]
    raise RuntimeError(loi)


def _yt_dlp(url: str, thu_muc: str) -> Dict:
    import yt_dlp
    goc = {"quiet": True, "no_warnings": True, "noplaylist": True, "ffmpeg_location": pc.ffmpeg(),
           "format": "best[height<=720][ext=mp4]/best[height<=720]/best", "max_filesize": NANG_TOI_DA,
           "outtmpl": os.path.join(thu_muc, "nguon.%(ext)s")}
    cach = [{}]
    if la_youtube(url):
        from youtube_ingest import PLAYER_CLIENTS, _client_opts
        cach = [_client_opts(c) for c in PLAYER_CLIENTS]
    loi = "không rõ"
    for them in cach:
        try:
            with yt_dlp.YoutubeDL({**goc, **them}) as ydl:
                tin = ydl.extract_info(url, download=True) or {}
            co = [f for f in os.listdir(thu_muc) if f.startswith("nguon.") and not f.endswith(".part")]
            if co:
                return {"file": os.path.join(thu_muc, co[0]), "ten": (tin.get("title") or "").strip()[:120],
                        "tac_gia": (tin.get("uploader") or tin.get("channel") or "").strip()[:60]}
        except Exception as exc:  # noqa: BLE001
            loi = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))[:160]
    raise RuntimeError(loi)


def tai_video(url: str, thu_muc: str) -> Dict:
    """Tải video của `url` vào `thu_muc`. Trả {"file", "ten", "tac_gia", "nen"}; hỏng thì ném RuntimeError đọc được."""
    url = kiem_link(url)
    os.makedirs(thu_muc, exist_ok=True)
    for cu in [f for f in os.listdir(thu_muc) if f.startswith("nguon.")]:
        os.remove(os.path.join(thu_muc, cu))
    loi = []
    for ten, cach in ((("tikwm", _tiktok_tikwm),) if la_tiktok(url) else ()) + (("bộ tải", _yt_dlp),):
        try:
            return {**cach(url, thu_muc), "nen": ten_nen(url), "link": url}
        except Exception as exc:  # noqa: BLE001
            loi.append(f"{ten}: {exc}")
    raise RuntimeError(f"Không tải được video từ {ten_nen(url)} (video riêng tư, đã xoá, hoặc trang chặn tải). "
                       "Hãy tải video về máy rồi đưa file lên. Chi tiết: " + "; ".join(loi)[:300])
