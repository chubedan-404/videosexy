"""
youtube_ingest.py
─────────────────
Bóc lời thoại của một video YouTube để làm nguyên liệu viết kịch bản mới.

Hai đường lấy, thử theo thứ tự rẻ trước:

1. Phụ đề có sẵn. yt_dlp tải riêng file phụ đề, không tải video, mất vài giây.
   Phần lớn video tiếng Việt và tiếng Anh đều có phụ đề tự động.
2. Chép bằng máy. Không có phụ đề thì tải riêng luồng tiếng rồi cho
   faster-whisper chép. Chậm hơn nhiều nên phải chạy nền.

Đây CHỈ là bước lấy nguyên liệu. Việc viết lại thành kịch bản mới nằm ở
longform_studio, và ở đó bắt buộc phải viết bằng lời khác chứ không chép lại
câu chữ của video gốc.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from typing import Callable, Dict, List, Optional

# Thứ tự ưu tiên ngôn ngữ phụ đề. Lấy tiếng Việt trước, rồi tiếng Anh, rồi
# bất cứ thứ gì có, vì nội dung nào cũng dịch được ở bước viết lại.
SUB_LANGS = ["vi", "en", "en-US", "en-GB"]

# YouTube chặn dần các client cũ. Đo ngày 2026-09-12: bỏ trống hoặc dùng "tv"
# đều trả "The page needs to be reloaded", chỉ "android" và "mweb" còn qua được.
# Thử lần lượt thay vì cắm cứng một cái, vì danh sách này đổi theo tháng.
PLAYER_CLIENTS = ["android", "mweb", "ios", "web_safari", ""]


def _client_opts(client: str) -> Dict:
    """Tuỳ chọn yt_dlp cho một client. Chuỗi rỗng nghĩa là để yt_dlp tự chọn."""
    if not client:
        return {}
    return {"extractor_args": {"youtube": {"player_client": [client]}}}


def _try_clients(run) -> Dict:
    """
    Chạy `run(opts)` lần lượt với từng client tới khi có cái chạy được.

    Trả về kết quả đầu tiên thành công, hoặc lỗi của lần cuối cùng.
    """
    last = {"success": False, "error": "Không kết nối được YouTube."}
    for client in PLAYER_CLIENTS:
        try:
            got = run(_client_opts(client))
        except Exception as exc:
            last = {"success": False, "error": str(exc)[:200]}
            continue
        if got.get("success"):
            got["client"] = client or "mặc định"
            return got
        last = got
    return last

_ID_PATTERNS = [
    re.compile(r"(?:youtube\.com|youtu\.be).*?[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})"),
]

# Dòng giờ của VTT, ví dụ 00:01:32.480 --> 00:01:35.120 align:start position:0%
_CUE_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s+-->")
_TAG = re.compile(r"<[^>]+>")


def parse_video_id(url: str) -> str:
    """Lấy mã video từ mọi kiểu link YouTube. Trả chuỗi rỗng nếu không phải."""
    url = (url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    for pattern in _ID_PATTERNS:
        found = pattern.search(url)
        if found:
            return found.group(1)
    return ""


def parse_vtt(raw: str) -> str:
    """
    Đổi file phụ đề thành văn xuôi liền mạch.

    Phụ đề tự động của YouTube cuộn chữ: mỗi khung lặp lại phần cuối của khung
    trước rồi thêm vài từ mới. Chép thẳng ra thì câu nào cũng lặp hai ba lần.
    Ở đây bỏ dòng trùng với dòng ngay trước, và bỏ luôn dòng nằm gọn trong
    phần đuôi của đoạn đã ghi.
    """
    out: List[str] = []
    for line in (raw or "").splitlines():
        line = _TAG.sub("", line).strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if _CUE_TIME.match(line) or line.isdigit():
            continue
        line = line.replace("&nbsp;", " ").strip()
        if not line:
            continue
        if out and line == out[-1]:
            continue
        tail = " ".join(out[-3:])
        if line and line in tail:
            continue
        out.append(line)
    return " ".join(out)


def _newest_subtitle(folder: str) -> str:
    files = [f for f in os.listdir(folder) if f.endswith((".vtt", ".srt"))]
    if not files:
        return ""
    # Ưu tiên đúng thứ tự ngôn ngữ mong muốn, hết mới lấy file bất kỳ.
    for lang in SUB_LANGS:
        for name in files:
            if f".{lang}." in name:
                return os.path.join(folder, name)
    return os.path.join(folder, files[0])


def fetch_metadata(url: str) -> Dict:
    """Tên video, kênh, thời lượng. Không tải gì cả."""
    import yt_dlp

    def run(extra):
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, **extra}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"success": True,
                "video_id": info.get("id", ""),
                "title": info.get("title", ""),
                "channel": info.get("uploader") or info.get("channel") or "",
                "duration": int(info.get("duration") or 0),
                "language": info.get("language") or ""}

    got = _try_clients(run)
    if not got.get("success"):
        got["error"] = f"Không đọc được video này: {got.get('error', '')}"
    return got


def pick_subtitle_langs(manual: List[str], auto: List[str], video_lang: str = "") -> List[Dict]:
    """
    Xếp thứ tự các thứ tiếng đáng thử, thử lần lượt tới khi tải được.

    Hai điều học được khi đo thật. Một, đòi nhiều thứ tiếng trong cùng một lần
    gọi là hỏng: gặp thứ tiếng không có thì yt_dlp bỏ luôn cả mẻ. Hai, danh sách
    phụ đề máy của YouTube liệt kê hàng trăm thứ tiếng vì nó dịch máy từ bản
    gốc, mà bản dịch đó hay bị chặn 429. Vì vậy phải bám lấy TIẾNG GỐC của
    video, không phải tiếng mình muốn đọc. Dịch để ở bước viết lại kịch bản.
    """
    order: List[Dict] = []
    seen = set()

    def add(lang: str, auto_flag: bool):
        key = (lang, auto_flag)
        if lang and key not in seen:
            seen.add(key)
            order.append({"lang": lang, "auto": auto_flag})

    # Phụ đề người làm luôn sạch hơn phụ đề máy, thử trước.
    base = (video_lang or "").split("-")[0]
    for lang in [video_lang, base] + SUB_LANGS:
        if lang in manual:
            add(lang, False)
    for lang in sorted(manual):
        add(lang, False)
    # Phụ đề máy: chỉ bám tiếng gốc, tránh mọi bản dịch máy.
    for lang in [video_lang, base, "en"]:
        if lang in auto:
            add(lang, True)
    return order[:4]


def fetch_subtitle_text(url: str) -> Dict:
    """Đường rẻ: xem video có phụ đề tiếng nào, rồi tải đúng một thứ tiếng đó."""
    import yt_dlp

    def look(extra):
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, **extra}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"success": True,
                "manual": sorted((info.get("subtitles") or {}).keys()),
                "auto": sorted((info.get("automatic_captions") or {}).keys()),
                "video_lang": info.get("language") or "",
                "opts": extra}

    found = _try_clients(look)
    if not found.get("success"):
        return {"success": False, "error": f"Không xem được phụ đề: {found.get('error', '')}"}
    candidates = pick_subtitle_langs(
        [l for l in found["manual"] if l != "live_chat"],
        [l for l in found["auto"] if l != "live_chat"],
        found.get("video_lang", ""))
    if not candidates:
        return {"success": False, "error": "Video này không có phụ đề."}

    last_error = "Không lấy được phụ đề."
    for choice in candidates:
        folder = tempfile.mkdtemp(prefix="ytsub_")
        try:
            opts = {
                "quiet": True, "no_warnings": True, "skip_download": True,
                "writesubtitles": not choice["auto"],
                "writeautomaticsub": choice["auto"],
                "subtitleslangs": [choice["lang"]],
                "subtitlesformat": "vtt/srt/best",
                "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
                **found.get("opts", {}),
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            path = _newest_subtitle(folder)
            if not path:
                last_error = "Tải được video nhưng không lấy được file phụ đề."
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = parse_vtt(fh.read())
            if len(text.split()) < 60:
                last_error = "Phụ đề quá ngắn, không đủ để phân tích."
                continue
            kind = "phụ đề máy tự tạo" if choice["auto"] else "phụ đề có sẵn"
            return {"success": True, "text": text, "origin": f"{kind} ({choice['lang']})",
                    "sub_lang": choice["lang"]}
        except Exception as exc:
            last_error = str(exc)[:200]
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    return {"success": False, "error": f"Không lấy được phụ đề: {last_error}"}


def transcribe_audio_only(url: str, model_name: str = "large-v3-turbo",
                          progress: Optional[Callable[[str], None]] = None) -> Dict:
    """
    Đường chậm: tải riêng luồng tiếng rồi cho faster-whisper chép.

    Chỉ tải tiếng chứ không tải hình, nên nhẹ hơn tải cả video nhiều lần.
    """
    import yt_dlp

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"success": False, "error": "Máy chưa cài faster-whisper nên không chép tiếng được."}

    from remotion_generator import _ffmpeg_bin

    folder = tempfile.mkdtemp(prefix="ytaud_")
    try:
        if progress:
            progress("Đang tải phần tiếng của video")
        base = {
            "quiet": True, "no_warnings": True,
            "format": "bestaudio/best",
            "outtmpl": os.path.join(folder, "audio.%(ext)s"),
            "ffmpeg_location": _ffmpeg_bin(),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        }
        for client in PLAYER_CLIENTS:
            try:
                with yt_dlp.YoutubeDL({**base, **_client_opts(client)}) as ydl:
                    ydl.download([url])
                break
            except Exception:
                continue
        found = [f for f in os.listdir(folder) if f.endswith(".wav")]
        if not found:
            return {"success": False, "error": "Không tách được phần tiếng của video."}
        if progress:
            progress("Đang chép lời bằng máy, bước này lâu nhất")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(os.path.join(folder, found[0]), vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        if len(text.split()) < 60:
            return {"success": False, "error": "Chép ra quá ít chữ, video này chắc không có lời."}
        return {"success": True, "text": text, "origin": "máy chép lại"}
    except Exception as exc:
        return {"success": False, "error": f"Không chép được lời: {exc}"}
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# Cắt bớt cho vừa cửa sổ ngữ cảnh của AI. Khoảng bốn mươi nghìn ký tự đã là
# một video ba mươi phút nói liên tục, đủ để phân tích cấu trúc.
MAX_TRANSCRIPT_CHARS = 40000


def get_transcript(url: str, allow_whisper: bool = True,
                   progress: Optional[Callable[[str], None]] = None) -> Dict:
    """Lấy lời thoại: thử phụ đề trước, không có thì mới chép bằng máy."""
    if not parse_video_id(url):
        return {"success": False, "error": "Đây không phải link YouTube hợp lệ."}
    if progress:
        progress("Đang tìm phụ đề có sẵn")
    res = fetch_subtitle_text(url)
    if not res["success"] and allow_whisper:
        if progress:
            progress("Video không có phụ đề, chuyển sang chép bằng máy")
        res = transcribe_audio_only(url, progress=progress)
    if not res["success"]:
        return res
    text = " ".join(res["text"].split())
    return {"success": True, "origin": res["origin"],
            "text": text[:MAX_TRANSCRIPT_CHARS],
            "truncated": len(text) > MAX_TRANSCRIPT_CHARS,
            "chars": len(text)}
