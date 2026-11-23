"""
REPURPOSE (21/10/2026): một video dài (podcast, livestream, review, bài giảng) thành nhiều clip dọc 9:16 cho
Shorts / Reels / TikTok. Học theo OpenShorts (github.com/mutonby/openshorts, MIT), không chép code của họ.

Trình tự một dự án (việc nền tiền tố `rp_`):
1. Nhận video: tải lên, hoặc dán link (yt-dlp tải về).
2. Nghe lời: faster-whisper từng từ kèm mốc giây (dùng chung mô hình với Edit Studio), cất `loi.json`.
3. AI chọn đoạn (9Router, `lf.ask_json`): đưa các câu đánh số, AI trả câu đầu, câu cuối, tiêu đề, chữ mở đầu (hook),
   lý do, điểm. Mốc cắt luôn bám ranh giới câu, như `cat_doc.py`: clip mở bằng nửa câu là clip bị lướt qua.
4. Dựng từng clip bằng ffmpeg: khung "bám mặt" (OpenCV dò mặt, khung nhảy theo người nói, không trượt lừ đừ), hoặc
   "khung mờ" (cả khung ngang đặt giữa nền mờ, hợp video quay màn hình, slide). ffmpeg của máy không có libass/drawtext,
   nên phụ đề và chữ hook được vẽ bằng Pillow thành ảnh trong suốt, ghép bằng concat demuxer rồi overlay.

Máy chủ: `/api/repurpose/*`. Trang: `static/repurpose.js` + `.css`. Bài kiểm: `tests/test_repurpose.py`.
Dữ liệu: `downloads/repurpose/<mã>/` (du_an.json, goc.*, loi.json, clip_01.mp4, ...).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, send_file, send_from_directory

import longform_studio as lf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "repurpose")
PHONG = os.path.join(BASE_DIR, "remotion", "public", "fonts")
RONG, CAO = 1080, 1920

DO_DAI = {"ngan": (15, 30), "vua": (30, 60), "dai": (60, 90)}
KHUNG = ("tu_dong", "bam_mat", "khung_mo")
PHU_DE = {
    "trang": {"mau": (255, 255, 255), "nhan": (255, 210, 63)},
    "vang": {"mau": (255, 210, 63), "nhan": (255, 255, 255)},
    "tat": None,
}
CAP_Y = 1180          # mép trên vùng phụ đề: trên vùng nút của Shorts/Reels (đáy ~520px bị che, theo cat_doc.py)
CAP_CAO = 360
HOOK_Y = 210
HOOK_GIAY = 3.5
GIAY_TOI_DA = 4 * 3600

lay_cai_dat_ai: Callable[[], Dict] = lambda: {}

_khoa = threading.RLock()
bp = Blueprint("repurpose", __name__)


# ─────────────────────────────────────────────────────────────────────────────
# LƯU DỰ ÁN
# ─────────────────────────────────────────────────────────────────────────────

def _duong(*p: str) -> str:
    return os.path.join(KHO, *p)


def _ma_hop_le(ma: str) -> bool:
    return bool(re.fullmatch(r"rp_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", ma or ""))


def _doc(ma: str) -> Optional[Dict]:
    if not _ma_hop_le(ma):
        return None
    try:
        with open(_duong(ma, "du_an.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _ghi(d: Dict) -> None:
    with _khoa:
        os.makedirs(_duong(d["id"]), exist_ok=True)
        tam = _duong(d["id"], "du_an.json.tam")
        with open(tam, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tam, _duong(d["id"], "du_an.json"))


def _sua(ma: str, **gia_tri) -> Dict:
    with _khoa:
        d = _doc(ma)
        if d is None:
            raise ValueError("Không có dự án này.")
        d.update(gia_tri)
        _ghi(d)
        return d


def danh_sach() -> List[Dict]:
    if not os.path.isdir(KHO):
        return []
    ra = [d for d in (_doc(m) for m in os.listdir(KHO)) if d]
    ra.sort(key=lambda d: d.get("tao_luc") or 0, reverse=True)
    return ra


def dang_chay(d: Dict) -> bool:
    return bool(d.get("job_id")) and (lf.get_job(d["job_id"]) or {}).get("status") == "running"


def ban_gui(d: Dict) -> Dict:
    viec = lf.get_job(d.get("job_id") or "") or {}
    chay = viec.get("status") == "running"
    buoc = d.get("buoc")
    if not chay and buoc in ("tai", "nghe", "chon", "dung"):
        buoc = "loi"    # tool khởi động lại giữa chừng
    return {**d, "buoc": buoc, "dang_chay": chay, "tien_do": viec.get("progress") or 0, "dang_lam": viec.get("step") or "",
            "loi": d.get("loi") or ("Tool khởi động lại giữa chừng. Bấm Làm tiếp." if buoc == "loi" and not d.get("loi") else "")}


def chuan_cai_dat(c: Dict) -> Dict:
    c = c or {}
    return {"so_clip": max(1, min(15, int(c.get("so_clip") or 5))),
            "do_dai": c.get("do_dai") if c.get("do_dai") in DO_DAI else "vua",
            "khung": c.get("khung") if c.get("khung") in KHUNG else "tu_dong",
            "phu_de": c.get("phu_de") if c.get("phu_de") in PHU_DE else "trang",
            "hook": bool(c.get("hook", True)),
            "ngon_ngu": c.get("ngon_ngu") if c.get("ngon_ngu") in ("auto", "vi", "en") else "auto"}


def tao(ten: str, url: str = "", file_tam: str = "", ten_file: str = "", cai_dat: Optional[Dict] = None) -> Dict:
    url = (url or "").strip()
    if not file_tam and not re.match(r"^https?://", url):
        raise ValueError("Chọn một file video, hoặc dán link video (YouTube, Facebook, TikTok...).")
    ma = time.strftime("rp_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    os.makedirs(_duong(ma), exist_ok=True)
    goc = ""
    if file_tam:
        goc = "goc" + (os.path.splitext(ten_file or file_tam)[1].lower() or ".mp4")
        shutil.move(file_tam, _duong(ma, goc))
    d = {"id": ma, "ten": (ten or ten_file or url or "Video")[:120], "tao_luc": time.time(), "url": url, "goc": goc,
         "giay": 0, "rong": 0, "cao": 0, "ngon_ngu": "", "cai_dat": chuan_cai_dat(cai_dat or {}),
         "buoc": "moi", "loi": "", "job_id": "", "clips": []}
    _ghi(d)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# NHẬN VIDEO, NGHE LỜI
# ─────────────────────────────────────────────────────────────────────────────

def _ffmpeg() -> str:
    tai_cho = os.path.join(BASE_DIR, "ffmpeg")
    return tai_cho if os.path.isfile(tai_cho) else (shutil.which("ffmpeg") or "ffmpeg")


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def do_video(duong: str) -> Dict:
    """Độ dài, khổ (đã tính xoay của video quay điện thoại), có tiếng hay không."""
    ra = subprocess.run([_ffprobe(), "-v", "error", "-show_entries",
                         "format=duration:stream=codec_type,width,height:stream_side_data=rotation:stream_tags=rotate",
                         "-of", "json", duong], capture_output=True, text=True, timeout=60)
    j = json.loads(ra.stdout or "{}")
    v = next((s for s in j.get("streams") or [] if s.get("codec_type") == "video"), None)
    if not v:
        raise ValueError("File này không có hình.")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    xoay = str((v.get("tags") or {}).get("rotate") or next((x.get("rotation") for x in v.get("side_data_list") or [] if "rotation" in x), 0))
    if xoay.strip("-") in ("90", "270"):
        w, h = h, w
    return {"giay": float((j.get("format") or {}).get("duration") or 0), "rong": w, "cao": h,
            "co_tieng": any(s.get("codec_type") == "audio" for s in j.get("streams") or [])}


def _tai_link(d: Dict, viec: Dict) -> str:
    import yt_dlp

    def tien(t):
        if t.get("status") == "downloading" and t.get("total_bytes"):
            lf.update_job(viec, progress=int(2 + 13 * t["downloaded_bytes"] / t["total_bytes"]))
    mau = _duong(d["id"], "goc.%(ext)s")
    opt = {"outtmpl": mau, "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
           "merge_output_format": "mp4", "noplaylist": True, "quiet": True, "no_warnings": True, "progress_hooks": [tien],
           "ffmpeg_location": os.path.dirname(shutil.which("ffmpeg") or "") or None}
    with yt_dlp.YoutubeDL(opt) as y:
        info = y.extract_info(d["url"], download=True)
    goc = next((f for f in os.listdir(_duong(d["id"])) if f.startswith("goc.") and not f.endswith((".part", ".ytdl"))), "")
    if not goc:
        raise RuntimeError("Không tải được video từ link này.")
    if info.get("title") and d["ten"] == d["url"][:120]:
        _sua(d["id"], ten=str(info["title"])[:120])
    return goc


def nghe(duong: str, ngon_ngu: str, viec: Optional[Dict] = None, giay: float = 0) -> Tuple[List[Dict], str]:
    """Lời nói từng từ [{"w","a","b"}] và ngôn ngữ nghe ra. Mô hình whisper dùng chung với Edit Studio."""
    import edit_studio as es
    wav = duong + ".wav"
    subprocess.run([_ffmpeg(), "-y", "-v", "error", "-i", duong, "-vn", "-ac", "1", "-ar", "16000", wav], timeout=3600, check=True)
    tu: List[Dict] = []
    try:
        with es._khoa_whisper:
            doan, thong_tin = es._mo_hinh_whisper().transcribe(wav, language=None if ngon_ngu == "auto" else ngon_ngu,
                                                                word_timestamps=True, vad_filter=True)
            for seg in doan:
                if viec is not None:
                    if viec.get("cancel"):
                        raise InterruptedError
                    if giay:
                        lf.update_job(viec, progress=int(15 + 45 * min(1.0, float(seg.end) / giay)),
                                      step=f"Đang nghe lời: {int(seg.end) // 60}:{int(seg.end) % 60:02d} / {int(giay) // 60}:{int(giay) % 60:02d}")
                for w in seg.words or []:
                    chu = (w.word or "").strip()
                    if chu:
                        tu.append({"w": chu, "a": round(float(w.start), 3), "b": round(float(w.end), 3)})
        return tu, getattr(thong_tin, "language", "") or ngon_ngu
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def chia_cau(tu: List[Dict], nghi: float = 0.8, toi_da: int = 30) -> List[Dict]:
    """Gom từ thành câu: ngắt ở dấu kết câu, chỗ nghỉ từ `nghi` giây, hoặc khi câu đã `toi_da` từ."""
    cau, cur = [], []
    for w in tu:
        if cur and (w["a"] - cur[-1]["b"] >= nghi or len(cur) >= toi_da):
            cau.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?…。？！]$", w["w"]):
            cau.append(cur)
            cur = []
    if cur:
        cau.append(cur)
    return [{"a": c[0]["a"], "b": c[-1]["b"], "chu": " ".join(x["w"] for x in c)} for c in cau]


# ─────────────────────────────────────────────────────────────────────────────
# AI CHỌN ĐOẠN
# ─────────────────────────────────────────────────────────────────────────────

_HE_CHON = """You are a senior short-form video editor. From the numbered transcript of a long video, pick the __N__ best
self-contained moments to post as vertical Shorts / Reels / TikTok clips.
Rules:
- Each clip is a continuous range of sentences, __MIN__ to __MAX__ seconds long (timestamps are given).
- It must make sense with NO context: start on a sentence that grabs attention right away (a bold claim, a question, a
  surprising fact, a strong emotion), never on a continuation like "and", "so", "because", "và", "thì", "nên".
- It must end on a complete thought or payoff, never mid-idea.
- Clips must not overlap. Prefer variety of topics over several clips about the same point.
- "hook" is the on-screen text shown during the first 3 seconds: max 8 words, in the SAME language as the transcript,
  punchy, makes people stop scrolling, no emoji, no hashtags.
- "tieu_de" is a post title in the same language as the transcript, max 70 characters, no emoji.
- "ly_do" is ONE Vietnamese sentence explaining why this moment works as a short.
- "diem" is 1-100: how likely the clip is to keep viewers watching and get shared.
Return ONLY JSON: {"clips": [{"tu": <first sentence number>, "den": <last sentence number>, "tieu_de": "...", "hook": "...", "ly_do": "...", "diem": <int>}]}"""


def _mmss(g: float) -> str:
    return f"{int(g) // 60}:{int(g) % 60:02d}"


def sua_doan(cau: List[Dict], tu_: int, den: int, ngan: float, dai: float) -> Optional[Tuple[int, int]]:
    """Kéo khoảng câu [tu_, den] về trong [ngan, dai] giây: dài quá thì bớt câu cuối, ngắn quá thì thêm câu sau rồi câu trước."""
    n = len(cau)
    if not (0 <= tu_ < n):
        return None
    den = max(tu_, min(n - 1, den))
    dai_cua = lambda a, b: cau[b]["b"] - cau[a]["a"]
    while den > tu_ and dai_cua(tu_, den) > dai:
        den -= 1
    if dai_cua(tu_, den) > dai:      # một câu đã dài quá: vẫn lấy, cắt ở `dai` lúc dựng
        return tu_, den
    while dai_cua(tu_, den) < ngan:
        if den + 1 < n and dai_cua(tu_, den + 1) <= dai:
            den += 1
        elif tu_ > 0 and dai_cua(tu_ - 1, den) <= dai:
            tu_ -= 1
        else:
            break
    return (tu_, den) if dai_cua(tu_, den) >= ngan * 0.6 else None


def chon_doan(cau: List[Dict], so_clip: int, do_dai: str, cai_dat_ai: Optional[Dict] = None) -> List[Dict]:
    """AI chọn đoạn, máy kiểm: bám câu, đúng độ dài, không chồng lên nhau, xếp theo điểm."""
    ngan, dai = DO_DAI[do_dai]
    if not cau:
        raise ValueError("Video không có lời nói nào nghe được, chưa chọn đoạn được.")
    if cau[-1]["b"] - cau[0]["a"] < ngan:
        raise ValueError(f"Lời nói trong video chưa tới {ngan} giây, ngắn hơn một clip.")
    # Video rất dài: chia khúc khoảng 45.000 chữ, mỗi khúc xin thêm vài đoạn rồi lấy những đoạn điểm cao nhất.
    dong = [f"[{i}] ({_mmss(c['a'])}-{_mmss(c['b'])}) {c['chu']}" for i, c in enumerate(cau)]
    khuc, cur, dem = [], [], 0
    for d in dong:
        if cur and dem + len(d) > 45000:
            khuc.append(cur)
            cur, dem = [], 0
        cur.append(d)
        dem += len(d) + 1
    khuc.append(cur)
    moi_khuc = max(2, -(-so_clip * 3 // (2 * len(khuc))))
    ung_vien = []
    for k in khuc:
        he = (_HE_CHON.replace("__N__", str(so_clip if len(khuc) == 1 else moi_khuc))
              .replace("__MIN__", str(ngan)).replace("__MAX__", str(dai)))
        kq = lf.ask_json(he, "Transcript:\n" + "\n".join(k), cai_dat_ai if cai_dat_ai is not None else (lay_cai_dat_ai() or {}),
                         max_tokens=4000)
        if not kq.get("success"):
            raise RuntimeError(kq.get("error") or "AI không trả lời.")
        ung_vien += [c for c in (kq.get("data") or {}).get("clips") or [] if isinstance(c, dict)]
    ra: List[Dict] = []
    for c in sorted(ung_vien, key=lambda c: -int(c.get("diem") or 0)):
        try:
            khoang = sua_doan(cau, int(c.get("tu")), int(c.get("den")), ngan, dai)
        except (TypeError, ValueError):
            continue
        if not khoang:
            continue
        a, b = cau[khoang[0]]["a"], min(cau[khoang[1]]["b"], cau[khoang[0]]["a"] + dai)
        if any(a < x["ket_thuc"] and b > x["bat_dau"] for x in ra):
            continue
        ra.append({"bat_dau": round(max(0.0, a - 0.15), 2), "ket_thuc": round(b + 0.25, 2),
                   "tieu_de": str(c.get("tieu_de") or "")[:100], "hook": str(c.get("hook") or "")[:80],
                   "ly_do": str(c.get("ly_do") or "")[:300], "diem": max(1, min(100, int(c.get("diem") or 50)))})
        if len(ra) >= so_clip:
            break
    if not ra:
        raise RuntimeError("AI chưa chọn được đoạn nào hợp lệ, bấm Làm lại.")
    return ra


# ─────────────────────────────────────────────────────────────────────────────
# KHUNG HÌNH: BÁM MẶT HOẶC KHUNG MỜ
# ─────────────────────────────────────────────────────────────────────────────

def do_mat(duong: str, a: float, b: float, buoc: float = 0.5) -> List[Optional[float]]:
    """Tâm ngang (pixel khung gốc) của khuôn mặt lớn nhất ở từng mốc `buoc` giây trong [a, b]; None nếu không thấy."""
    import cv2
    bo = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    cap = cv2.VideoCapture(duong)
    ra: List[Optional[float]] = []
    try:
        t = a
        while t < b:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, khung = cap.read()
            if not ok:
                ra.append(None)
                t += buoc
                continue
            h, w = khung.shape[:2]
            ti = 640 / w if w > 640 else 1.0
            nho = cv2.resize(khung, (int(w * ti), int(h * ti))) if ti < 1 else khung
            xam = cv2.cvtColor(nho, cv2.COLOR_BGR2GRAY)
            mat = bo.detectMultiScale(xam, scaleFactor=1.1, minNeighbors=6, minSize=(int(36 * ti) or 24, int(36 * ti) or 24))
            if len(mat):
                x, _y, mw, _mh = max(mat, key=lambda m: m[2] * m[3])
                ra.append((x + mw / 2) / ti)
            else:
                ra.append(None)
            t += buoc
    finally:
        cap.release()
    return ra


def duong_khung(tam: List[Optional[float]], rong: int, rong_cat: int, buoc: float = 0.5, nguong: float = 0.10,
                giu: int = 2) -> List[Tuple[float, int]]:
    """
    Từ tâm mặt theo thời gian ra các đoạn (giây bắt đầu, x mép trái của khung cắt). Khung đứng yên, chỉ NHẢY sang chỗ
    mới khi mặt lệch quá `nguong` bề ngang trong `giu` giây liền (đổi người nói), như dựng tay; trượt theo từng nhịp
    thì nhìn như máy quay say sóng.
    """
    co = [x for x in tam if x is not None]
    if not co:
        return [(0.0, max(0, (rong - rong_cat) // 2))]
    # Lấp chỗ trống bằng giá trị gần nhất, rồi lấy trung vị từng giây cho khỏi giật vì một khung dò sai.
    day, gan = [], next(x for x in tam if x is not None)
    for x in tam:
        gan = x if x is not None else gan
        day.append(gan)
    moi_giay = max(1, int(round(1 / buoc)))
    giay = [statistics.median(day[i:i + moi_giay]) for i in range(0, len(day), moi_giay)]
    kep = lambda c: int(max(0, min(rong - rong_cat, c - rong_cat / 2)))
    doan = [(0.0, kep(giay[0]))]
    cur, lech = giay[0], 0
    for i, c in enumerate(giay[1:], 1):
        if abs(c - cur) > nguong * rong:
            lech += 1
            if lech >= giu:
                cur = statistics.median(giay[i - giu + 1:i + 1])
                doan.append((float(i - giu + 1), kep(cur)))
                lech = 0
        else:
            lech = 0
    return doan


def bieu_thuc_x(doan: List[Tuple[float, int]]) -> str:
    """Biểu thức x cho bộ lọc crop của ffmpeg: if(lt(t,T1),X0,if(lt(t,T2),X1,...X_cuối))."""
    bt = str(doan[-1][1])
    for i in range(len(doan) - 2, -1, -1):
        bt = f"if(lt(t\\,{doan[i + 1][0]:.2f})\\,{doan[i][1]}\\,{bt})"
    return bt


# ─────────────────────────────────────────────────────────────────────────────
# CHỮ: PHỤ ĐỀ TỪNG TỪ VÀ HOOK, VẼ BẰNG PILLOW
# ─────────────────────────────────────────────────────────────────────────────

def _phong(co: int, dam: str = "ExtraBold"):
    from PIL import ImageFont
    for ten in (f"BeVietnamPro-{dam}.ttf", "BeVietnamPro-Bold.ttf", "Montserrat-ExtraBold.ttf"):
        p = os.path.join(PHONG, ten)
        if os.path.isfile(p):
            return ImageFont.truetype(p, co)
    return ImageFont.load_default()


def _xuong_dong(chu: List[str], phong, rong: int) -> List[List[int]]:
    """Chia danh sách từ thành dòng vừa bề rộng, trả về chỉ số từ của từng dòng."""
    dong, cur = [], []
    for i, w in enumerate(chu):
        thu = " ".join(chu[j] for j in cur + [i])
        if cur and phong.getlength(thu) > rong:
            dong.append(cur)
            cur = []
        cur.append(i)
    if cur:
        dong.append(cur)
    return dong


def ve_dong(chu: List[str], nhan: int, kieu: Dict, duong: str, co: int = 70) -> None:
    """Một ảnh trong suốt RONG x CAP_CAO: dòng phụ đề, từ thứ `nhan` (đang nói) tô màu nhấn; -1 là không tô."""
    from PIL import Image, ImageDraw
    phong = _phong(co)
    vien = max(4, co // 11)
    while True:
        dong = _xuong_dong(chu, phong, RONG - 140)
        if len(dong) <= 2 or co <= 44:
            break
        co -= 6
        phong = _phong(co)
        vien = max(4, co // 11)
    anh = Image.new("RGBA", (RONG, CAP_CAO), (0, 0, 0, 0))
    ve = ImageDraw.Draw(anh)
    cao_dong = int(co * 1.25)
    y = (CAP_CAO - cao_dong * len(dong)) // 2
    cach = phong.getlength(" ")
    for d in dong:
        rong_dong = phong.getlength(" ".join(chu[i] for i in d))
        x = (RONG - rong_dong) / 2
        for i in d:
            mau = kieu["nhan"] if i == nhan else kieu["mau"]
            ve.text((x, y), chu[i], font=phong, fill=mau + (255,), stroke_width=vien, stroke_fill=(0, 0, 0, 255))
            x += phong.getlength(chu[i]) + cach
        y += cao_dong
    anh.save(duong)


def ve_hook(chu: str, duong: str) -> int:
    """Chữ hook đầu clip: chữ đen trên nền trắng bo góc, tối đa 3 dòng. Trả về chiều cao ảnh."""
    from PIL import Image, ImageDraw
    co = 64
    while True:
        phong = _phong(co)
        dong = [[chu.split()[i] for i in d] for d in _xuong_dong(chu.split(), phong, RONG - 220)]
        if len(dong) <= 3 or co <= 40:
            break
        co -= 4
    cao_dong = int(co * 1.28)
    rong_hop = int(max(phong.getlength(" ".join(d)) for d in dong) + 64)
    cao_hop = cao_dong * len(dong) + 40
    anh = Image.new("RGBA", (RONG, cao_hop), (0, 0, 0, 0))
    ve = ImageDraw.Draw(anh)
    x0 = (RONG - rong_hop) // 2
    ve.rounded_rectangle((x0, 0, x0 + rong_hop, cao_hop), radius=22, fill=(255, 255, 255, 245))
    y = 20
    for d in dong:
        s = " ".join(d)
        ve.text(((RONG - phong.getlength(s)) / 2, y), s, font=phong, fill=(0, 0, 0, 255))
        y += cao_dong
    anh.save(duong)
    return cao_hop


def su_kien_phu_de(tu: List[Dict], a: float, b: float, toi_da_tu: int = 4) -> List[Dict]:
    """
    Các ảnh phụ đề theo thời gian của clip (giây tính từ đầu clip): mỗi từ đang nói là một ảnh (cả dòng, từ đó tô
    màu), khoảng lặng là ảnh trống. Dòng ngắn 4 từ kiểu CapCut, gom bằng edit_studio.chia_cau.
    """
    import edit_studio as es
    trong = [{"w": w["w"], "a": max(0.0, w["a"] - a), "b": min(b, w["b"]) - a} for w in tu if w["a"] >= a - 0.05 and w["b"] <= b + 0.2]
    ra: List[Dict] = []
    t = 0.0
    for dong in es.chia_cau(trong, toi_da_tu=toi_da_tu, toi_da_giay=2.2):
        chu = [x["w"] for x in dong["tu"]]
        bd = dong["bat_dau"]
        het = bd + dong["giay"]
        if bd > t + 0.02:
            ra.append({"chu": [], "nhan": -1, "a": t, "b": bd})
        for k, x in enumerate(dong["tu"]):
            ka = bd + x["t"]
            kb = bd + dong["tu"][k + 1]["t"] if k + 1 < len(dong["tu"]) else het
            if kb - ka > 0.02:
                ra.append({"chu": chu, "nhan": k, "a": max(ka, t), "b": kb})
        t = max(t, het)
    if b - a > t + 0.02:
        ra.append({"chu": [], "nhan": -1, "a": t, "b": b - a})
    return [s for s in ra if s["b"] - s["a"] > 0.01]


# ─────────────────────────────────────────────────────────────────────────────
# DỰNG MỘT CLIP
# ─────────────────────────────────────────────────────────────────────────────

def bo_loc_khung(kieu: str, rong: int, cao: int, doan: Optional[List[Tuple[float, int]]] = None) -> str:
    """Chuỗi bộ lọc ffmpeg từ [0:v] ra [khung] 1080x1920."""
    if cao >= rong * 1.5:       # video đã dọc
        return (f"[0:v]scale={RONG}:{CAO}:force_original_aspect_ratio=decrease,pad={RONG}:{CAO}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1[khung]")
    if kieu == "bam_mat":
        rc = int(cao * 9 / 16) // 2 * 2
        x = bieu_thuc_x(doan or [(0.0, (rong - rc) // 2)])
        return f"[0:v]crop={rc}:{cao // 2 * 2}:'{x}':0,scale={RONG}:{CAO},setsar=1[khung]"
    return (f"[0:v]split[n][t];[n]scale={RONG}:{CAO}:force_original_aspect_ratio=increase,crop={RONG}:{CAO},boxblur=24:2,"
            f"eq=brightness=-0.08[nen];[t]scale={RONG}:-2[truoc];[nen][truoc]overlay=(W-w)/2:(H-h)/2,setsar=1[khung]")


def dung_clip(ma: str, i: int, co_chu: bool = True, ra_file: str = "") -> Dict:
    """Dựng clip thứ i của dự án. `co_chu=False`: chỉ khung hình (cho Video Editor). Trả về thông tin clip đã dựng."""
    d = _doc(ma)
    if not d:
        raise ValueError("Không có dự án này.")
    c = d["clips"][i]
    goc = _duong(ma, d["goc"])
    a, b = float(c["bat_dau"]), float(c["ket_thuc"])
    dai = round(b - a, 3)
    kieu = d["cai_dat"]["khung"]
    doan = None
    if d["cao"] < d["rong"] * 1.5 and kieu in ("tu_dong", "bam_mat"):
        tam = do_mat(goc, a, b)
        thay = sum(1 for x in tam if x is not None) / max(1, len(tam))
        if kieu == "bam_mat" or thay >= 0.35:
            kieu = "bam_mat"
            rc = int(d["cao"] * 9 / 16) // 2 * 2
            doan = duong_khung(tam, d["rong"], rc)
        else:
            kieu = "khung_mo"
    tam_dir = _duong(ma, f"tam_{i:02d}")
    shutil.rmtree(tam_dir, ignore_errors=True)
    os.makedirs(tam_dir)
    try:
        lenh = [_ffmpeg(), "-y", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{dai:.3f}", "-i", goc]
        loc = bo_loc_khung(kieu, d["rong"], d["cao"], doan)
        cuoi = "khung"
        so_vao = 1
        kieu_pd = PHU_DE.get(d["cai_dat"]["phu_de"]) if co_chu else None
        if kieu_pd:
            tu = _doc_json(_duong(ma, "loi.json")) or []
            sk = su_kien_phu_de(tu, a, b)
            if sk:
                ds = []
                for k, s in enumerate(sk):
                    p = os.path.join(tam_dir, f"c{k:04d}.png")
                    ve_dong(s["chu"], s["nhan"], kieu_pd, p)
                    ds.append(f"file '{p}'\nduration {max(0.02, s['b'] - s['a']):.3f}")
                ds.append(f"file '{os.path.join(tam_dir, f'c{len(sk) - 1:04d}.png')}'")
                with open(os.path.join(tam_dir, "phu_de.txt"), "w", encoding="utf-8") as f:
                    f.write("\n".join(ds) + "\n")
                lenh += ["-f", "concat", "-safe", "0", "-i", os.path.join(tam_dir, "phu_de.txt")]
                loc += f";[{so_vao}:v]format=rgba[pd];[{cuoi}][pd]overlay=0:{CAP_Y}:eof_action=pass[co_pd]"
                cuoi = "co_pd"
                so_vao += 1
        if co_chu and d["cai_dat"]["hook"] and c.get("hook"):
            p = os.path.join(tam_dir, "hook.png")
            ve_hook(c["hook"], p)
            lenh += ["-loop", "1", "-t", f"{min(HOOK_GIAY, dai):.2f}", "-i", p]
            loc += f";[{so_vao}:v]format=rgba[hk];[{cuoi}][hk]overlay=0:{HOOK_Y}:eof_action=pass[co_hk]"
            cuoi = "co_hk"
            so_vao += 1
        ra_file = ra_file or _duong(ma, f"clip_{i + 1:02d}.mp4")
        lenh += ["-filter_complex", loc, "-map", f"[{cuoi}]", "-map", "0:a?", "-t", f"{dai:.3f}", "-r", "30",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", ra_file]
        kq = subprocess.run(lenh, capture_output=True, text=True, timeout=1800)
        if kq.returncode != 0 or not os.path.isfile(ra_file):
            raise RuntimeError("ffmpeg dựng clip lỗi: " + (kq.stderr or "")[-300:])
    finally:
        shutil.rmtree(tam_dir, ignore_errors=True)
    if not co_chu:
        return {"file": ra_file}
    anh = _duong(ma, f"clip_{i + 1:02d}.jpg")
    subprocess.run([_ffmpeg(), "-y", "-v", "error", "-ss", f"{min(1.5, dai / 2):.2f}", "-i", ra_file, "-frames:v", "1",
                    "-vf", "scale=360:-2", anh], timeout=60)
    return {"file": os.path.basename(ra_file), "anh_nho": os.path.basename(anh) if os.path.isfile(anh) else "",
            "khung_dung": kieu, "dung_luc": time.time()}


def _doc_json(p: str):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# VIỆC NỀN
# ─────────────────────────────────────────────────────────────────────────────

def chay(ma: str, chon_lai: bool = False, chi_clip: Optional[List[int]] = None) -> str:
    """
    Làm tiếp dự án từ bước còn dở: tải, nghe, chọn đoạn, dựng. `chon_lai`: bỏ các đoạn cũ, nhờ AI chọn lại.
    `chi_clip`: chỉ dựng lại các clip này (sau khi người dùng sửa mốc hay chữ hook).
    """
    d = _doc(ma)
    if not d:
        raise ValueError("Không có dự án này.")
    if dang_chay(d):
        raise ValueError("Dự án đang chạy.")

    def lam(viec: Dict) -> None:
        try:
            dd = _doc(ma)
            if not dd["goc"]:
                _sua(ma, buoc="tai", loi="")
                lf.update_job(viec, step="Đang tải video từ link", progress=2)
                dd = _sua(ma, goc=_tai_link(dd, viec))
            if not dd["giay"]:
                do = do_video(_duong(ma, dd["goc"]))
                if do["giay"] > GIAY_TOI_DA:
                    raise ValueError("Video dài quá 4 tiếng, cắt bớt rồi tải lên lại.")
                if not do["co_tieng"]:
                    raise ValueError("Video không có tiếng, chưa chọn được đoạn theo lời nói.")
                dd = _sua(ma, giay=do["giay"], rong=do["rong"], cao=do["cao"])
            if not os.path.isfile(_duong(ma, "loi.json")):
                _sua(ma, buoc="nghe", loi="")
                lf.update_job(viec, step="Đang nghe lời trong video", progress=15)
                tu, nn = nghe(_duong(ma, dd["goc"]), dd["cai_dat"]["ngon_ngu"], viec, dd["giay"])
                with open(_duong(ma, "loi.json"), "w", encoding="utf-8") as f:
                    json.dump(tu, f, ensure_ascii=False)
                dd = _sua(ma, ngon_ngu=nn)
            if chon_lai or not dd["clips"]:
                _sua(ma, buoc="chon", loi="")
                lf.update_job(viec, step="AI đang chọn những đoạn hay nhất", progress=62)
                cau = chia_cau(_doc_json(_duong(ma, "loi.json")) or [])
                for c in dd["clips"]:
                    for k in ("file", "anh_nho"):
                        if c.get(k):
                            try:
                                os.remove(_duong(ma, c[k]))
                            except OSError:
                                pass
                dd = _sua(ma, clips=chon_doan(cau, dd["cai_dat"]["so_clip"], dd["cai_dat"]["do_dai"]))
            can = chi_clip if chi_clip is not None else [i for i, c in enumerate(dd["clips"]) if not c.get("file")]
            _sua(ma, buoc="dung", loi="")
            for k, i in enumerate(can):
                if viec.get("cancel"):
                    raise InterruptedError
                lf.update_job(viec, step=f"Đang dựng clip {i + 1}/{len(dd['clips'])}", progress=int(70 + 29 * k / max(1, len(can))))
                c_moi = dung_clip(ma, i)
                with _khoa:
                    x = _doc(ma)
                    x["clips"][i].update(c_moi)
                    _ghi(x)
            _sua(ma, buoc="xong", loi="")
            lf.update_job(viec, step="Xong", progress=100)
        except InterruptedError:
            _sua(ma, buoc="loi", loi="Đã dừng. Bấm Làm tiếp để chạy nốt.")
        except Exception as e:  # noqa: BLE001
            _sua(ma, buoc="loi", loi=str(e)[:400])
            raise

    ma_viec = lf.start_job("rp_lam", lam)
    _sua(ma, job_id=ma_viec)
    return ma_viec


def sua_clip(ma: str, i: int, bat_dau=None, ket_thuc=None, hook=None, tieu_de=None) -> Dict:
    """Người dùng sửa mốc cắt / chữ hook / tiêu đề của một clip. Đổi mốc hay hook thì clip cần dựng lại."""
    with _khoa:
        d = _doc(ma)
        if not d or not (0 <= i < len(d["clips"])):
            raise ValueError("Không có clip này.")
        c = d["clips"][i]
        a = float(bat_dau) if bat_dau is not None else c["bat_dau"]
        b = float(ket_thuc) if ket_thuc is not None else c["ket_thuc"]
        if not (0 <= a < b <= (d["giay"] or b)) or b - a < 3 or b - a > 180:
            raise ValueError("Mốc cắt không hợp lệ (clip từ 3 giây tới 3 phút, nằm trong video).")
        doi = abs(a - c["bat_dau"]) > 0.01 or abs(b - c["ket_thuc"]) > 0.01 or (hook is not None and hook != c.get("hook"))
        c.update(bat_dau=round(a, 2), ket_thuc=round(b, 2))
        if hook is not None:
            c["hook"] = str(hook)[:80]
        if tieu_de is not None:
            c["tieu_de"] = str(tieu_de)[:100]
        if doi:
            c["can_dung_lai"] = True
        _ghi(d)
        return d


def sang_edit(ma: str, i: int) -> str:
    """Mở clip thứ i trong Video Editor: dựng bản chỉ có khung hình (không chữ) để người dùng tự thêm chữ, nhạc."""
    import edit_studio as es
    d = _doc(ma)
    if not d or not (0 <= i < len(d["clips"])):
        raise ValueError("Không có clip này.")
    tam = _duong(ma, f"sach_{i + 1:02d}.mp4")
    dung_clip(ma, i, co_chu=False, ra_file=tam)
    try:
        c = d["clips"][i]
        du = es.tao_du_an((c.get("tieu_de") or d["ten"])[:60], "9:16")
        m = es.nhap_media(du["id"], f"clip_{i + 1:02d}.mp4", lambda p: shutil.copy2(tam, p))
        du["media"] = [m]
        du["lop"][0]["clips"] = [{"id": es._ma_moi("c"), "media": m["id"], "dau": 0, "cuoi": m.get("giay") or (c["ket_thuc"] - c["bat_dau"])}]
        du = {**es.chuan_du_an(du, du), "ban_xuat": []}
        es._ghi_json(os.path.join(es._thu_muc(du["id"]), "du_an.json"), du)
        return du["id"]
    finally:
        try:
            os.remove(tam)
        except OSError:
            pass


def xoa(ma: str) -> None:
    d = _doc(ma)
    if not d:
        raise ValueError("Không có dự án này.")
    if dang_chay(d):
        raise ValueError("Dự án đang chạy, dừng trước rồi mới xoá.")
    shutil.rmtree(_duong(ma), ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# ĐƯỜNG VÀO
# ─────────────────────────────────────────────────────────────────────────────

def _loi(msg: str, ma: int = 400):
    return jsonify({"ok": False, "error": msg}), ma


@bp.get("/api/repurpose/phien")
def api_phien():
    dang = [d["job_id"] for d in danh_sach() if dang_chay(d)]
    return jsonify({"ok": True, "job_id": dang[0] if dang else ""})


@bp.get("/api/repurpose/du_an")
def api_ds():
    return jsonify({"ok": True, "du_an": [ban_gui(d) for d in danh_sach()[:40]]})


@bp.post("/api/repurpose/du_an")
def api_tao():
    f = request.form
    tep = request.files.get("video")
    tam, ten_file = "", ""
    if tep and tep.filename:
        ten_file = os.path.basename(tep.filename)
        duoi = os.path.splitext(ten_file)[1].lower()
        if duoi not in (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"):
            return _loi("File phải là video (MP4, MOV, MKV, WEBM).")
        os.makedirs(_duong("tam"), exist_ok=True)
        tam = _duong("tam", uuid.uuid4().hex + duoi)
        tep.save(tam)
    try:
        cai = json.loads(f.get("cai_dat") or "{}")
    except Exception:  # noqa: BLE001
        cai = {}
    try:
        d = tao(f.get("ten") or "", f.get("url") or "", tam, ten_file, cai)
        chay(d["id"])
    except ValueError as e:
        if tam and os.path.isfile(tam):
            os.remove(tam)
        return _loi(str(e))
    return jsonify({"ok": True, "du_an": ban_gui(_doc(d["id"]))})


@bp.post("/api/repurpose/du_an/<ma>/chay")
def api_chay(ma):
    t = request.get_json(silent=True) or {}
    d = _doc(ma)
    if not d:
        return _loi("Không có dự án này.", 404)
    try:
        if t.get("cai_dat"):
            _sua(ma, cai_dat=chuan_cai_dat({**d["cai_dat"], **t["cai_dat"]}))
        chi = None
        if t.get("dung_lai"):
            chi = [i for i, c in enumerate(d["clips"]) if c.get("can_dung_lai") or not c.get("file")]
            with _khoa:
                x = _doc(ma)
                for c in x["clips"]:
                    c.pop("can_dung_lai", None)
                _ghi(x)
        chay(ma, chon_lai=bool(t.get("chon_lai")), chi_clip=chi)
    except ValueError as e:
        return _loi(str(e))
    return jsonify({"ok": True, "du_an": ban_gui(_doc(ma))})


@bp.post("/api/repurpose/du_an/<ma>/dung")
def api_dung(ma):
    d = _doc(ma)
    return jsonify({"ok": bool(d and d.get("job_id") and lf.cancel_job(d["job_id"]))})


@bp.put("/api/repurpose/du_an/<ma>/clip/<int:i>")
def api_sua_clip(ma, i):
    t = request.get_json(silent=True) or {}
    try:
        d = sua_clip(ma, i, t.get("bat_dau"), t.get("ket_thuc"), t.get("hook"), t.get("tieu_de"))
    except (ValueError, TypeError) as e:
        return _loi(str(e))
    return jsonify({"ok": True, "du_an": ban_gui(d)})


@bp.post("/api/repurpose/du_an/<ma>/clip/<int:i>/sang_edit")
def api_sang_edit(ma, i):
    try:
        return jsonify({"ok": True, "du_an": sang_edit(ma, i)})
    except ValueError as e:
        return _loi(str(e))
    except Exception as e:  # noqa: BLE001
        return _loi(f"Chưa mở được trong Video Editor: {str(e)[:200]}", 500)


@bp.delete("/api/repurpose/du_an/<ma>")
def api_xoa(ma):
    try:
        xoa(ma)
    except ValueError as e:
        return _loi(str(e))
    return jsonify({"ok": True})


@bp.get("/api/repurpose/tep/<ma>/<ten>")
def api_tep(ma, ten):
    if not _ma_hop_le(ma) or not re.fullmatch(r"clip_\d{2}\.(mp4|jpg)", ten or ""):
        return _loi("Không có file này.", 404)
    return send_from_directory(_duong(ma), ten, as_attachment=request.args.get("tai") == "1")


@bp.get("/api/repurpose/du_an/<ma>/zip")
def api_zip(ma):
    import tempfile
    import zipfile
    d = _doc(ma)
    if not d:
        return _loi("Không có dự án này.", 404)
    co = [(c, _duong(ma, c["file"])) for c in d["clips"] if c.get("file") and os.path.isfile(_duong(ma, c["file"]))]
    if not co:
        return _loi("Chưa có clip nào dựng xong.")
    f = tempfile.TemporaryFile()
    with zipfile.ZipFile(f, "w", zipfile.ZIP_STORED) as z:
        for k, (c, p) in enumerate(co, 1):
            z.write(p, f"{k:02d}_{re.sub(r'[^0-9A-Za-zÀ-ỹ _-]', '', c.get('tieu_de') or '')[:50].strip() or 'clip'}.mp4")
        z.writestr("tieu_de.txt", "\n\n".join(f"{k:02d}. {c.get('tieu_de') or ''}\nHook: {c.get('hook') or ''}" for k, (c, _) in enumerate(co, 1)))
    f.seek(0)
    return send_file(f, mimetype="application/zip", as_attachment=True, download_name=f"{ma}.zip")
