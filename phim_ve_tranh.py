"""
Phim vẽ tranh: video kiểu bảng trắng, một bàn tay cầm bút vẽ dần bức tranh nét
theo lời kể, trên nền giấy màu kem. Xưởng riêng, không dùng chung trạng thái với
Video hoạt hình hay Video dài.

Một lượt làm phim:
1. Viết kịch bản bằng bộ máy kịch bản chung của tool (dàn ý, chương, đọc lại),
   theo khung giải thích sự kiện; gộp thành cảnh 25 tới 35 giây.
2. AI tả từng tranh (2 tới 3 nhóm hình tách rời, xếp theo thứ tự lời kể), rồi máy
   vẽ của tool (9Router hoặc Draw Things) vẽ tranh nét đúng phong cách.
3. Đọc lời bằng giọng đã chọn (Edge hoặc giọng nhân bản).
4. AI nhìn tranh và chia vùng theo lời kể; mỗi vùng bắt đầu vẽ đúng lúc lời kể
   nhắc tới.
5. Bộ vẽ bảng trắng (vendor/srt_whiteboard, MIT) vẽ từng cảnh; ghép tiếng, phụ
   đề, nối các cảnh, trộn nhạc.

Phiên lưu ở downloads/phim_ve_tranh/phien.json; tranh, tiếng, đoạn phim của mỗi
cảnh nằm trong downloads/phim_ve_tranh/<mã phim>/.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Dict, List, Optional

import longform_studio as lf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(BASE_DIR, "vendor", "srt_whiteboard")
DOWNLOADS = os.path.join(BASE_DIR, "downloads")
KHO = os.path.join(DOWNLOADS, "phim_ve_tranh")
PHIEN = os.path.join(KHO, "phien.json")

DO_DAI = (2, 3, 5, 8, 10, 15, 20)
TU_MOI_GIAY = 2.6          # tốc độ đọc tiếng Việt đo được của giọng Edge, chậm vừa
CANH_GIAY = (25, 35)       # mỗi cảnh vẽ một tranh, như repo gốc khuyên
FPS = 30
SONG_SONG = 3              # số cảnh vẽ cùng lúc (bộ vẽ chạy một lõi)

PHONG_CACH = (
    "Minimalist hand-drawn whiteboard sketch illustration, pure line-art doodle style like Notion "
    "illustrations, warm beige paper background #F5EBD7, dark grey sketch lines, only a few small red, "
    "orange and blue accents, clean background, lots of empty space, simple outlines, no shading "
    "texture, no 3D, no photo realism, absolutely no text, no letters, no numbers, no labels, no "
    "watermark. 16:9 wide. 2 or 3 groups of elements clearly separated with empty space between them. "
    "Scene: ")

# Khung kịch bản: kênh giải thích sự kiện (cùng tinh thần kênh hoạt hình, viết riêng để
# hai xưởng chỉnh lời dặn độc lập).
LUAT_KICH_BAN = (
    "Đây là phim vẽ tranh giải thích một chủ đề cho người xem phổ thông, không kể tiểu sử ai. Trả lời "
    "đủ: vì đâu xảy ra, những bên nào liên quan và mỗi bên muốn gì, diễn ra thế nào, bước ngoặt, kết "
    "thúc ra sao, hệ quả. Mở bằng một câu hỏi gay cấn, cuối phim trả lời câu hỏi đó. Câu văn tả "
    "những hình ảnh dễ vẽ thành tranh nét đơn giản. Mỗi sự việc chỉ kể một lần.")
LOI_DAN = "Kiểu phim: tranh vẽ tay kể chuyện, câu ngắn, tả hình ảnh cụ thể. Chuyện chưa chắc ghi Giả thuyết."


# ─── Phiên ────────────────────────────────────────────────────────────────
def doc_phien() -> Dict:
    try:
        with open(PHIEN, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def luu_phien(d: Dict) -> None:
    os.makedirs(KHO, exist_ok=True)
    tam = PHIEN + ".tmp"
    with open(tam, "w", encoding="utf-8") as fh:
        json.dump({**d, "at": time.time()}, fh, ensure_ascii=False)
    os.replace(tam, PHIEN)


def phut_hop_le(phut) -> int:
    try:
        p = float(phut)
    except (TypeError, ValueError):
        p = 5
    return next((d for d in DO_DAI if d >= p), DO_DAI[-1])


# ─── Kho đề tài gợi ý ────────────────────────────────────────────────────
DE_TAI_FILE = os.path.join(BASE_DIR, "phim_ve_tranh_de_tai.json")


def doc_kho_de_tai() -> Dict:
    """Đọc danh mục và danh sách chủ đề được cấu hình cho phim vẽ tranh."""
    try:
        with open(DE_TAI_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"danh_muc": [], "chu_de": []}


def luu_them_de_tai(ten: str, chu_de: str, nhom: str = "chien_tranh", phut: int = 5) -> Dict:
    """Thêm một chủ đề mới vào kho đề tài đã cấu hình."""
    data = doc_kho_de_tai()
    ds = data.setdefault("chu_de", [])
    moi = {
        "id": f"pvt_custom_{len(ds) + 1}_{int(time.time())}",
        "nhom": nhom or "chien_tranh",
        "ten": ten[:80],
        "chu_de": chu_de[:400],
        "phut": int(phut or 5),
    }
    ds.insert(0, moi)
    try:
        with open(DE_TAI_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return moi


# ─── Bàn tay cầm bút có tên kênh ─────────────────────────────────────────
def tao_tay_but(ten: str = "") -> str:
    """
    Ảnh tay cầm bút; có tên kênh thì in tên đó dọc theo thân bút (trắng). Hướng
    thân bút đo từ chính ảnh (trục chính của vùng trắng), nên chữ nằm thẳng trên bút.
    """
    goc = os.path.join(VENDOR, "tay_but.png")
    ten = (ten or "").strip()[:28]
    if not ten:
        return goc
    ra = os.path.join(KHO, f"tay_{hashlib.md5(ten.encode()).hexdigest()[:10]}.png")
    if os.path.isfile(ra):
        return ra
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(goc).convert("RGBA")
    a = np.array(im).astype(int)
    trang = (a[..., 0] > 235) & (a[..., 1] > 235) & (a[..., 2] > 235) & (a[..., 3] > 200)
    ys, xs = np.nonzero(trang)
    cx, cy = xs.mean(), ys.mean()
    cov = np.cov(np.vstack([xs - cx, ys - cy]))
    vx, vy = np.linalg.eigh(cov)[1][:, -1]
    goc_do = math.degrees(math.atan2(vy, vx))
    if goc_do > 90:
        goc_do -= 180
    if goc_do < -90:
        goc_do += 180
    dai = 2 * math.sqrt(np.linalg.eigh(cov)[0][-1]) * 1.7
    rong = 2 * math.sqrt(np.linalg.eigh(cov)[0][0]) * 1.5
    font_duong = next((f for f in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                                   "/Library/Fonts/Arial Unicode.ttf",
                                   "/System/Library/Fonts/Supplemental/Arial.ttf") if os.path.isfile(f)), None)
    co = int(rong * 0.62)
    while co > 10:
        font = ImageFont.truetype(font_duong, co) if font_duong else ImageFont.load_default()
        w = ImageDraw.Draw(Image.new("L", (1, 1))).textlength(ten, font=font)
        if w <= dai * 0.85:
            break
        co -= 2
    lop = Image.new("RGBA", (int(dai) + 40, int(rong) + 40), (0, 0, 0, 0))
    ve = ImageDraw.Draw(lop)
    ve.text((lop.width / 2, lop.height / 2), ten, font=font, fill=(30, 26, 24, 255), anchor="mm")
    lop = lop.rotate(-goc_do, expand=True, resample=Image.BICUBIC)
    im.alpha_composite(lop, (int(cx - lop.width / 2), int(cy - lop.height / 2)))
    os.makedirs(KHO, exist_ok=True)
    im.save(ra)
    return ra


# ─── Kịch bản thành cảnh ─────────────────────────────────────────────────
def gop_canh(scenes: List[Dict], giay=CANH_GIAY) -> List[Dict]:
    """
    Chia lại lời của cả phim thành các cảnh dài đều nhau, mỗi cảnh một tranh, cắt ở cuối câu.

    Không giữ ranh giới chương: phim vẽ tranh không hiện thẻ chương, mà giữ ranh giới thì
    chương nào cũng dư ra một mẩu. Đo 03/10/2026 trên phim 2 phút bốn chương: cảnh ra
    51, 86, 26, 81, 27, 110 từ, tức có tranh đứng 8 giây và có tranh đứng 39 giây.
    """
    cau: List[str] = []
    for s in scenes:
        cau += [c for c in re.split(r"(?<=[.!?…])\s+", (s.get("text") or "").strip()) if c]
    if not cau:
        return []
    dem = [len(c.split()) for c in cau]
    tong = sum(dem)
    so_canh = max(1, round(tong / (sum(giay) / 2 * TU_MOI_GIAY)))
    ra: List[Dict] = []
    gom: List[str] = []
    da = 0
    for c, n in zip(cau, dem):
        moc = tong * (len(ra) + 1) / so_canh
        # Câu này đưa cảnh vượt mốc: cắt trước hay sau nó, bên nào gần mốc hơn thì chọn.
        if gom and len(ra) < so_canh - 1 and da + n - moc > moc - da:
            ra.append({"text": " ".join(gom), "chapter": ""})
            gom = []
        gom.append(c)
        da += n
    ra.append({"text": " ".join(gom), "chapter": ""})
    for i, s in enumerate(ra):
        s["id"] = i + 1
    return ra


_MO_TA_HE = (
    "You design whiteboard sketch illustrations for a narrated history explainer video. For EACH scene, "
    "write one English description of a single 16:9 drawing: 2 or 3 clearly separated groups of simple "
    "objects or people, placed left to right in the SAME ORDER the narration mentions them, with "
    "historically correct uniforms, vehicles, buildings and landscape for the era. No text, signs, "
    "flags with writing, letters or numbers in the drawing. Keep it simple enough to draw as line art.\n"
    'Return JSON: {"canh":[{"i":0,"mo_ta":"left: ...; center: ...; right: ..."}]}')


def mo_ta_tranh(topic: str, scenes: List[Dict], settings: Dict) -> None:
    """Điền `mo_ta` (tả tranh, tiếng Anh) cho mọi cảnh, mỗi lượt AI tám cảnh."""
    for dau in range(0, len(scenes), 8):
        lo = scenes[dau:dau + 8]
        user = f"Topic: {topic}\n" + "\n".join(f"{dau + k}: {s['text'][:500]}" for k, s in enumerate(lo))
        got = lf.ask_json(_MO_TA_HE, user, settings, max_tokens=2500)
        for x in ((got.get("data") or {}).get("canh") or []) if got.get("success") else []:
            try:
                i = int(x.get("i"))
            except (TypeError, ValueError, AttributeError):
                continue
            if dau <= i < dau + len(lo) and str(x.get("mo_ta") or "").strip():
                scenes[i]["mo_ta"] = str(x["mo_ta"]).strip()[:900]
    for s in scenes:
        s.setdefault("mo_ta", "a simple illustration of: " + s["text"][:300])


# Lấy khổ ngang của bộ máy kịch bản chung làm gốc rồi mới đè phần riêng. Tự kê
# tay từng khoá là thiếu ngay khi bộ máy chung cần thêm khoá mới: ngày 02/10/2026
# lượt chạy đầu chết ở bước cắt cảnh vì thiếu "seconds_per_image".
FORMAT_LONG = {
    **lf.get_format("long"),
    "label": "Ngang 16:9",
    "durations": DO_DAI,
    "default_duration": 5,
    # Cắt sẵn cảnh chừng 30 giây theo tốc độ đọc của xưởng này, gop_canh chỉ còn vá mẩu lẻ.
    "seconds_per_image": int(round(sum(CANH_GIAY) / 2 * TU_MOI_GIAY / (lf.WORDS_PER_MINUTE / 60.0))),
}


class _PhanViec(dict):
    """Bản ghi việc cho một bước con: phần trăm được co vào một khoảng của việc lớn."""

    def __init__(self, cha: Dict, tu: int, den: int):
        super().__init__()
        self.cha, self.tu, self.den = cha, tu, den

    def __getitem__(self, k):
        return self.cha[k]

    def get(self, k, mac=None):
        return self.cha.get(k, mac)

    def __contains__(self, k):
        return k in self.cha

    def __setitem__(self, k, v):
        if k == "status":
            return
        if k == "progress":
            v = int(self.tu + (self.den - self.tu) * max(0, min(100, int(v))) / 100)
        self.cha[k] = v


def noi_so_tach(scenes: List[Dict]) -> List[Dict]:
    for s in scenes:
        if s.get("text"):
            s["text"] = re.sub(r"(\d)([.,]) (\d{3})\b", r"\1\2\3", s["text"])
    return scenes


def ask_json_anh(system: str, user: str, anh: List[bytes], settings: Dict, max_tokens: int = 1500) -> Dict:
    import base64
    import requests
    b64 = [base64.b64encode(a).decode() for a in anh]
    base_url = settings.get("base_url") or ""
    if base_url:
        try:
            res = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.get('api_key') or '9router-key'}",
                         "Content-Type": "application/json"},
                json={"model": settings.get("model") or "combo1", "temperature": 0.2,
                      "max_tokens": max_tokens, "stream": False,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": [{"type": "text", "text": user}] + [
                                       {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{x}"}}
                                       for x in b64]}]},
                timeout=120)
            if res.status_code == 200:
                noi_dung = ((res.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                return {"success": True, "data": lf._extract_json(noi_dung), "source": "9router", "error": ""}
        except Exception:
            pass
    # Không lùi về AI chỉ đọc chữ: nó không thấy tranh nên toạ độ vùng trả về là bịa.
    # Báo hỏng để chia_vung chia đều từ trái qua phải.
    return {"success": False, "data": None, "error": "Không có AI nhận dạng ảnh"}


def chuan_khung(duong: str) -> None:
    """
    Cắt tranh về đúng 16:9. 9Router trả tranh 1792x1024, bộ vẽ giữ nguyên tỉ lệ nên
    phim ra 1280x730; tranh người dùng tải lên lại là 16:9, nối hai loại là hỏng phim.
    """
    from PIL import Image
    im = Image.open(duong)
    W, H = im.size
    w = min(W, H * 16 // 9) // 16 * 16
    h = w * 9 // 16
    if (w, h) == (W, H):
        return
    x, y = (W - w) // 2, (H - h) // 2
    im.convert("RGB").crop((x, y, x + w, y + h)).save(duong)


def ve_tranh(mo_ta: str, ra: str, settings: Dict, may: str = "router") -> Dict:
    """Vẽ một tranh nét bằng máy vẽ của tool."""
    fmt = dict(FORMAT_LONG)
    if may == "local":
        kq = lf.generate_image(PHONG_CACH + mo_ta, ra, fmt=fmt)
    else:
        kq = lf.generate_image_router(PHONG_CACH + mo_ta, ra, settings, fmt=fmt)
        if not kq.get("success") and lf.local_cli_ready():
            kq = lf.generate_image(PHONG_CACH + mo_ta, ra, fmt=fmt)
    if kq.get("success"):
        chuan_khung(ra)
    return kq


# ─── Chia vùng theo lời kể ───────────────────────────────────────────────
def chia_vung(anh: str, text: str, settings: Dict) -> List[Dict]:
    """
    AI nhìn tranh, trả 2 tới 3 vùng bao các nhóm hình, xếp theo thứ tự lời kể nhắc
    tới, kèm đoạn lời ứng với vùng. Hỏng thì chia đều theo chiều ngang từ trái qua phải.
    """
    from PIL import Image
    W, H = Image.open(anh).size
    he = ("Bạn chia một bức tranh vẽ tay thành các VÙNG để vẽ dần theo lời kể. Ảnh rộng "
          f"{W} x {H} điểm ảnh. Trả về 2 tới 3 vùng lớn (gộp các hình gần nhau), mỗi vùng bao trọn "
          "một nhóm hình, các vùng không chồng lên nhau nếu được, toạ độ số nguyên điểm ảnh của ảnh "
          "gốc, xếp theo đúng thứ tự lời kể nhắc tới. cau là đoạn NGUYÊN VĂN ngắn trong lời kể ứng "
          'với vùng đó. JSON: {"vung":[{"ten":"...","cau":"...","x":0,"y":0,"w":100,"h":100}]}')
    with open(anh, "rb") as fh:
        kq = ask_json_anh(he, "Lời kể: " + text, [fh.read()], settings, max_tokens=1200)
    ra = []
    for x in ((kq.get("data") or {}).get("vung") or []) if kq.get("success") else []:
        try:
            xx, yy = max(0, int(x["x"])), max(0, int(x["y"]))
            ww, hh_ = min(W - xx, int(x["w"])), min(H - yy, int(x["h"]))
        except (KeyError, TypeError, ValueError):
            continue
        if ww > 40 and hh_ > 40:
            ra.append({"ten": str(x.get("ten") or "")[:60], "cau": str(x.get("cau") or "")[:200],
                       "x": xx, "y": yy, "w": ww, "h": hh_})
    if not ra:
        ra = [{"ten": f"phần {k + 1}", "cau": "", "x": k * W // 3, "y": 0, "w": W // 3 + (W % 3 if k == 2 else 0), "h": H}
              for k in range(3)]
    return ra[:4]


def lap_bang_ve(vung: List[Dict], text: str, word_frames: List[float], voice_frames: int,
                W: int, H: int) -> Dict:
    """
    Bảng vẽ cho bộ vẽ (annotation): vùng đầu vẽ ngay khi cảnh mở, vùng sau bắt đầu
    lúc lời kể nhắc tới nó, mỗi vùng ít nhất 3 giây, vùng nào kéo dài tới lúc vùng
    sau bắt đầu; cuối cảnh đứng yên hơn một giây cho trọn tranh.
    """
    tu = text.split()
    wf = word_frames if len(word_frames) == len(tu) else [i * voice_frames / max(1, len(tu)) for i in range(len(tu))]
    tong = int(voice_frames / FPS * 1000) + 1500
    thap = text.lower()
    bd = []
    for v in vung:
        cau = (v.get("cau") or "").lower().strip()
        vt = thap.find(cau[:20]) if cau else -1
        i = len(thap[:vt].split()) if vt >= 0 else 0
        bd.append(int(wf[min(i, len(wf) - 1)] / FPS * 1000) if wf else 0)
    if bd:
        bd[0] = 0
    for j in range(1, len(bd)):
        bd[j] = max(bd[j], bd[j - 1] + 3000)
    cuoi = tong - 1300
    if bd and bd[-1] > cuoi - 3000:
        bd = [int(j * (cuoi - 300) / len(bd)) for j in range(len(bd))]
    els = []
    for j, v in enumerate(vung):
        s = max(150, bd[j])
        e = max((bd[j + 1] - 100) if j + 1 < len(vung) else cuoi, s + 1000)
        els.append({"id": f"v{j + 1}", "label": v["ten"], "sequence": j + 1, "narrativeRole": v["ten"],
                    "subtitle": v["cau"], "type": "object",
                    "region": {"x": v["x"], "y": v["y"], "width": v["w"], "height": v["h"]},
                    "reveal": {"direction": "left_to_right", "startMs": s, "durationMs": e - s,
                               "maskPaddingPx": 18, "protectedRegions": []},
                    "handPath": {"start": [v["x"], v["y"]], "end": [v["x"] + v["w"], v["y"] + v["h"]],
                                 "easing": "easeInOut"}})
    return {"sceneId": "canh", "canvas": {"width": W, "height": H}, "storyBasis": text,
            "sceneDurationMs": tong, "elements": els}


# ─── Dựng ─────────────────────────────────────────────────────────────────
def _ffmpeg() -> str:
    from remotion_generator import _ffmpeg_bin
    return _ffmpeg_bin()


def ve_mot_canh(anh: str, bang: Dict, ra: str, tay: str, cap_long_edge: int = 1280, ink_path: str = "grid") -> None:
    """Gọi bộ vẽ bảng trắng (vendor) cho một cảnh: 30 khung/giây, bút đi theo nét."""
    pf = ra + ".bang.json"
    with open(pf, "w", encoding="utf-8") as fh:
        json.dump(bang, fh, ensure_ascii=False)
    kq = subprocess.run([sys.executable, os.path.join(VENDOR, "render_stream_whiteboard.py"), anh, pf, ra, tay,
                         "--total-ms", str(bang["sceneDurationMs"]), "--fps", str(FPS),
                         "--cap-long-edge", str(cap_long_edge), "--ink-path", ink_path],
                        capture_output=True, text=True, timeout=3600)
    try:
        os.remove(pf)
    except OSError:
        pass
    if kq.returncode != 0 or not os.path.isfile(ra):
        raise RuntimeError("Bộ vẽ lỗi: " + (kq.stderr or kq.stdout)[-300:])


def _ts(ms: float) -> str:
    ms = int(ms)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def phu_de(text: str, word_frames: List[float], voice_frames: int, lech_ms: int = 300, moi: int = 8) -> str:
    """Phụ đề SRT của một cảnh, tám chữ một dòng, bám mốc từng chữ của giọng đọc."""
    tu = text.split()
    wf = word_frames if len(word_frames) == len(tu) else [i * voice_frames / max(1, len(tu)) for i in range(len(tu))]
    dong = []
    for a in range(0, len(tu), moi):
        b = min(len(tu), a + moi)
        t0 = lech_ms + wf[a] / FPS * 1000
        t1 = lech_ms + (wf[b] / FPS * 1000 if b < len(wf) else voice_frames / FPS * 1000 + 200)
        dong.append(f"{len(dong) + 1}\n{_ts(t0)} --> {_ts(t1)}\n{' '.join(tu[a:b])}\n")
    return "\n".join(dong)


KIEU_PHU_DE = ("FontName=Arial,FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00302018,"
               "BorderStyle=1,Outline=2,Shadow=0,MarginV=22")


def _escape_filter_path(path: str) -> str:
    return path.replace('\\', '\\\\').replace("'", "\\'").replace(':', '\\:').replace(',', '\\,').replace('[', '\\[').replace(']', '\\]')


def ghep_tieng(video: str, am: str, srt: str, ra: str, co_phu_de: bool = True) -> None:
    escaped_srt = _escape_filter_path(srt)
    loc = (f"[0:v]subtitles='{escaped_srt}':force_style='{KIEU_PHU_DE}'[v];" if co_phu_de and srt else "[0:v]null[v];")
    subprocess.run([_ffmpeg(), "-loglevel", "error", "-y", "-i", video, "-i", am, "-filter_complex",
                    loc + "[1:a]adelay=300|300,apad[a]", "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-ar", "48000", "-shortest", ra],
                   check=True, capture_output=True, timeout=1800)


def noi_phim(doan: List[str], ra: str) -> None:
    ds = ra + ".ds.txt"
    with open(ds, "w", encoding="utf-8") as fh:
        fh.writelines(f"file '{d}'\n" for d in doan)
    subprocess.run([_ffmpeg(), "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", ds, "-c", "copy",
                    "-movflags", "+faststart", ra], check=True, capture_output=True, timeout=1800)
    os.remove(ds)


def dung_phim(phien: Dict, cap: Callable[[int, str], None], huy: Callable[[], bool]) -> str:
    """Vẽ mọi cảnh (song song), ghép tiếng và phụ đề, nối, trộn nhạc. Trả đường tải."""
    ma = phien["ma"]
    thu_muc = os.path.join(KHO, ma)
    tay = tao_tay_but(phien.get("ten_but") or "")
    scenes = phien["scenes"]
    from PIL import Image

    def mot(i: int) -> str:
        s = scenes[i]
        anh = os.path.join(KHO, s["anh"])
        W, H = Image.open(anh).size
        bang = lap_bang_ve(s["vung"], s["text"], s.get("wordFrames") or [], int(s.get("voiceFrames") or 90), W, H)
        ve = os.path.join(thu_muc, f"canh_{i + 1:02d}_ve.mp4")
        cle = int(phien.get("cap_long_edge") or 1280)
        ip = str(phien.get("ink_path") or "grid")
        ve_mot_canh(anh, bang, ve, tay, cap_long_edge=cle, ink_path=ip)
        srt = os.path.join(thu_muc, f"canh_{i + 1:02d}.srt")
        with open(srt, "w", encoding="utf-8") as fh:
            fh.write(phu_de(s["text"], s.get("wordFrames") or [], int(s.get("voiceFrames") or 90)))
        from remotion_generator import REMOTION_PUBLIC_DIR
        am = os.path.join(REMOTION_PUBLIC_DIR, s["audioFile"])
        ra = os.path.join(thu_muc, f"canh_{i + 1:02d}.mp4")
        ghep_tieng(ve, am, srt, ra, phien.get("phu_de", True))
        os.remove(ve)
        return ra

    xong = 0
    doan: List[Optional[str]] = [None] * len(scenes)
    ex = ThreadPoolExecutor(max_workers=SONG_SONG)
    tuong_lai = {ex.submit(mot, i): i for i in range(len(scenes))}
    con = set(tuong_lai)
    try:
        # Hỏi huỷ mỗi giây. Chờ lần lượt từng cảnh thì bấm huỷ xong vẫn vẽ hết cả phim.
        while con:
            if huy():
                raise InterruptedError("Đã dừng")
            roi, con = wait(con, timeout=1, return_when=FIRST_COMPLETED)
            for f in roi:
                doan[tuong_lai[f]] = f.result()
                xong += 1
                cap(int(xong * 100 / len(scenes)), f"Đã vẽ {xong}/{len(scenes)} cảnh")
    finally:
        # Cảnh chưa tới lượt thì bỏ; cảnh đang vẽ dở (nhiều nhất SONG_SONG cảnh) tự chạy nốt.
        ex.shutdown(wait=False, cancel_futures=True)
    if huy():
        raise InterruptedError("Đã dừng")
    ten = f"phimvetranh_{ma}.mp4"
    ra = os.path.join(DOWNLOADS, ten)
    noi_phim([d for d in doan if d], ra)
    nhac = phien.get("nhac") or "none"
    if nhac != "none":
        import nhac_nen
        am_luong = float(phien.get("am_luong_nhac") or 0.15)
        db = 22.0 if am_luong <= 0.11 else (14.0 if am_luong >= 0.22 else 18.0)
        tron = nhac_nen.ghep(ra, nhac, duoi_giong_db=db, out_path=ra + ".nhac.mp4")
        if tron.get("success"):
            os.replace(tron["output"], ra)
    # Phụ đề cả phim cho YouTube.
    t, dong = 0, []
    for s, d in zip(scenes, doan):
        if not d:
            continue
        khung = int(s.get("voiceFrames") or 90)
        for khoi in phu_de(s["text"], s.get("wordFrames") or [], khung, lech_ms=t + 300).split("\n\n"):
            if khoi.strip():
                dong.append(f"{len(dong) + 1}\n" + khoi.split("\n", 1)[1])
        # Độ dài đoạn phim của cảnh đúng bằng sceneDurationMs trong lap_bang_ve, khỏi cần ffprobe
        # (máy nhận gói tool chưa chắc có ffprobe).
        t += int(khung / FPS * 1000) + 1500
    # Hai khối phụ đề phải cách nhau một dòng trống, thiếu là YouTube đọc số thứ tự thành lời.
    with open(ra.replace(".mp4", ".srt"), "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(k.rstrip("\n") for k in dong) + "\n")
    return f"/downloads/{ten}"


# ─── Việc nền ─────────────────────────────────────────────────────────────
def _ten_anh(ma: str, i: int) -> str:
    return f"{ma}/canh_{i + 1:02d}_{int(time.time())}.png"


def moc_tung_tu(s: Dict) -> List[float]:
    """
    Mốc khung hình của từng từ trong lời cảnh. Bộ đọc chung chỉ trả mốc khi số từ đọc
    khớp số từ viết; gặp con số ("1914" đọc thành bốn từ) là nó bỏ hết. Ở đây lấy mốc
    thô trong file .marks.json rồi rải lại theo tỉ lệ, lệch vài từ quanh con số nhưng
    vẫn giữ đúng chỗ nghỉ giữa các câu, sát hơn hẳn chia đều.
    """
    so_tu = len((s.get("text") or "").split())
    co = s.get("wordFrames") or []
    if len(co) == so_tu or not so_tu or not s.get("audioFile"):
        return co
    from remotion_generator import REMOTION_PUBLIC_DIR
    try:
        with open(os.path.splitext(os.path.join(REMOTION_PUBLIC_DIR, s["audioFile"]))[0] + ".marks.json",
                  encoding="utf-8") as fh:
            tho = (json.load(fh) or {}).get("marks") or []
        moc = [float(m["t"]) * FPS if isinstance(m, dict) else float(m) for m in tho]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if len(moc) < 2:
        return []
    return [round(moc[min(len(moc) - 1, round(i * (len(moc) - 1) / max(1, so_tu - 1)))], 2) for i in range(so_tu)]


def _co_tranh(s: Dict) -> bool:
    return bool(s.get("anh")) and os.path.isfile(os.path.join(KHO, s["anh"]))


def lam_not(phien: Dict, job: Dict, settings: Dict, tu: int = 31) -> None:
    """
    Làm nốt mọi phần còn thiếu của một phiên đã có kịch bản rồi dựng phim: tả tranh, vẽ
    tranh, đọc lời, chia vùng. Phần nào đã có trên đĩa thì giữ. Lưu phiên sau từng
    tranh, nên hỏng giữa chừng (router trượt tranh thứ 17) thì bấm làm tiếp là chạy từ
    đúng tranh đó chứ không viết lại kịch bản và vẽ lại 16 tranh đầu.
    """
    huy = lambda: bool(job.get("cancel"))  # noqa: E731
    co = lambda pt: tu + (100 - tu) * pt // 100  # noqa: E731
    scenes = phien["scenes"]
    ma = phien["ma"]
    os.makedirs(os.path.join(KHO, ma), exist_ok=True)
    job["scenes"] = scenes
    if any(not s.get("mo_ta") for s in scenes):
        lf.update_job(job, co(0), f"Đang tả tranh cho {len(scenes)} cảnh")
        mo_ta_tranh(phien.get("topic") or phien.get("title") or "", scenes, settings)
        luu_phien(phien)
    for i, s in enumerate(scenes):
        if huy():
            return
        if _co_tranh(s):
            continue
        lf.update_job(job, co(2 + 40 * i // len(scenes)), f"Đang vẽ tranh {i + 1}/{len(scenes)}")
        ten_anh = _ten_anh(ma, i)
        kq = ve_tranh(s["mo_ta"], os.path.join(KHO, ten_anh), settings, phien.get("may") or "router")
        if not kq.get("success"):
            raise RuntimeError(f"Không vẽ được tranh cảnh {i + 1}: {str(kq.get('error') or '')[:150]}. "
                               "Các tranh đã vẽ vẫn còn, bấm Làm tiếp để vẽ từ cảnh này.")
        # Gắn tên tranh sau khi file đã có trên đĩa, để giao diện không tải một tranh chưa vẽ xong.
        s["anh"] = ten_anh
        s.pop("vung", None)
        luu_phien(phien)
    lf.update_job(job, co(42), "Đang đọc lời")
    # Giọng có bộ nhớ đệm theo lời, cảnh đã đọc không đọc lại.
    doc = lf.doc_loi_dan(scenes, phien.get("voice") or {}, "edge", settings, log=lambda m: lf.update_job(job, log=m))
    for s, v in zip(scenes, doc):
        s.update({k: v.get(k) for k in ("audioFile", "voiceFrames", "wordFrames")})
        s["wordFrames"] = moc_tung_tu(s)
    luu_phien(phien)
    # Cảnh đọc hỏng trả audioFile rỗng; để lọt xuống bước ghép là ffmpeg báo một lỗi khó hiểu.
    im_lang = lf.missing_voices(scenes)
    if im_lang:
        raise RuntimeError(lf.missing_voices_message(im_lang))
    for i, s in enumerate(scenes):
        if huy():
            return
        if s.get("vung"):
            continue
        lf.update_job(job, co(48 + 9 * i // len(scenes)), f"Đang chia vùng tranh {i + 1}/{len(scenes)}")
        s["vung"] = chia_vung(os.path.join(KHO, s["anh"]), s["text"], settings)
        luu_phien(phien)
    url = dung_phim(phien, lambda pt, chu: lf.update_job(job, co(57 + pt * 42 // 100), chu), huy)
    phien["ket_qua"] = {"download_url": url, "srt_url": url.replace(".mp4", ".srt")}
    luu_phien(phien)
    job["result"] = {**phien}
    lf.update_job(job, 100, f"Xong phim vẽ tranh {len(scenes)} cảnh")


def lam_phim_worker(topic: str, seconds: int, settings: Dict, voice: Dict, notes: str = "",
                    may: str = "router", ten_but: str = "", nhac: str = "none", phu_de_bat: bool = True,
                    cap_long_edge: int = 1280, ink_path: str = "grid",
                    am_luong_nhac: float = 0.15) -> Callable[[Dict], None]:
    """Một nút từ chủ đề ra phim: kịch bản, tả tranh, vẽ tranh, đọc lời, chia vùng, vẽ, ghép."""

    def worker(job: Dict) -> None:
        ma = hashlib.md5(f"{topic}{time.time()}".encode()).hexdigest()[:10]
        lf.update_job(job, 1, "Đang viết kịch bản")
        # Phim này không có nhân vật chính: bỏ đoạn dẫn dắt vào nhân vật, và bỏ hai lượt AI
        # chốt gương mặt, viết mô tả tranh của bộ máy chung vì xưởng tự tả tranh theo kiểu của mình.
        viet = lf.script_worker(topic=topic, seconds=seconds, settings=settings, language="vi-VN",
                                audience=LUAT_KICH_BAN, notes=(notes.strip() + " " if notes.strip() else "") + LOI_DAN,
                                style_id="illustration", character="", fmt=dict(FORMAT_LONG), loi_chao="",
                                dan_dat=False, chi_loi=True)
        viet(_PhanViec(job, 1, 30))
        if job.get("cancel") or not job.get("result"):
            return
        kb = job["result"]
        job["result"] = None
        scenes = gop_canh(noi_so_tach(kb.get("scenes") or []))
        if not scenes:
            raise RuntimeError("AI không viết được kịch bản.")
        phien = {"ma": ma, "topic": topic, "title": kb.get("title") or topic, "scenes": scenes,
                 "may": may, "ten_but": ten_but, "nhac": nhac, "phu_de": phu_de_bat, "voice": voice,
                 "cap_long_edge": cap_long_edge, "ink_path": ink_path, "am_luong_nhac": am_luong_nhac}
        luu_phien(phien)
        job["result"] = {**phien}
        lf.update_job(job, 31, f"Kịch bản xong: {len(scenes)} cảnh")
        lam_not(phien, job, settings)
    return worker


def ve_lai_worker(i: int, settings: Dict, mo_ta: str = "") -> Callable[[Dict], None]:
    """Vẽ lại tranh một cảnh (tuỳ chọn tả lại), chia vùng lại. Chưa dựng phim."""
    def worker(job: Dict) -> None:
        phien = doc_phien()
        s = phien["scenes"][i]
        if mo_ta.strip():
            s["mo_ta"] = mo_ta.strip()[:900]
        lf.update_job(job, 10, f"Đang vẽ lại tranh cảnh {i + 1}")
        s["anh"] = _ten_anh(phien["ma"], i)
        anh_path = os.path.join(KHO, s["anh"])
        os.makedirs(os.path.dirname(anh_path), exist_ok=True)
        kq = ve_tranh(s["mo_ta"], anh_path, settings, phien.get("may") or "router")
        if not kq.get("success"):
            raise RuntimeError("Không vẽ được: " + str(kq.get("error") or "")[:150])
        lf.update_job(job, 70, "Đang chia vùng")
        s["vung"] = chia_vung(anh_path, s["text"], settings)
        luu_phien(phien)
        job["result"] = {"scene": s, "i": i}
        lf.update_job(job, 100, f"Đã vẽ lại tranh cảnh {i + 1}")
    return worker


def nhan_tranh(i: int, du_lieu: bytes, settings: Dict) -> Dict:
    """Người dùng tải tranh của mình lên cho một cảnh: chuẩn về 16:9 nền kem, chia vùng lại."""
    import io
    from PIL import Image
    phien = doc_phien()
    s = phien["scenes"][i]
    im = Image.open(io.BytesIO(du_lieu)).convert("RGB")
    W, H = 1920, 1080
    nen = Image.new("RGB", (W, H), (245, 235, 215))
    im.thumbnail((W, H))
    nen.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    s["anh"] = _ten_anh(phien["ma"], i)
    anh_path = os.path.join(KHO, s["anh"])
    os.makedirs(os.path.dirname(anh_path), exist_ok=True)
    nen.save(anh_path)
    s["vung"] = chia_vung(anh_path, s["text"], settings)
    luu_phien(phien)
    return s


def dung_lai_worker(settings: Optional[Dict] = None, voice: Optional[Dict] = None) -> Callable[[Dict], None]:
    """
    Dựng lại phim từ phiên đang có (sau khi vẽ lại hay thay tranh). Phiên còn dở vì lượt
    trước hỏng giữa chừng thì làm nốt phần thiếu rồi dựng; phiên đã đủ thì không gọi AI.
    """
    def worker(job: Dict) -> None:
        phien = doc_phien()
        # Đổi giọng ở ô chọn rồi bấm dựng lại: đọc lại lời bằng giọng mới, tranh và vùng giữ nguyên.
        if voice and voice != phien.get("voice"):
            phien["voice"] = voice
            luu_phien(phien)
        lam_not(phien, job, settings or {}, tu=0)
    return worker
