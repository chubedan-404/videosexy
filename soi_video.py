"""
Soi video mẫu (PlayAh): đưa một video TikTok (hoặc YouTube, Facebook, file trong máy), tool cắt khung hình, cho AI nhìn
rồi viết ra PHONG CÁCH của video và CÂU LỆNH cho máy làm clip để làm một video PlayAh theo kiểu đó.

Chỉ nhìn hình, KHÔNG bóc lời: lời của video mẫu thường kể chuyện không liên quan tới cách quay. Câu lệnh là AI đoán ngược
từ khung hình, không phải câu lệnh gốc của người làm video (video không lưu câu lệnh bên trong).

Trình tự: tải video -> đo chỗ đổi cảnh (ffmpeg) -> mỗi cảnh lấy một hai khung -> AI nhìn các khung, trả phong cách và
việc xảy ra trong từng cảnh -> tool ghép thành câu lệnh, tự gắn các khoá của PlayAh (đúng sản phẩm theo ảnh mẫu, không
chữ, không dấu mờ). Video nguồn bị xoá ngay sau khi cắt khung, chỉ giữ khung hình.

Mỗi lần soi là một thư mục trong downloads/soi_video/. Nút "Làm video Review theo mẫu này" mở một video mới bên xưởng
Review sản phẩm với các câu lệnh này điền sẵn.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Dict, List, Optional

import requests
from flask import Blueprint, jsonify, request, send_from_directory

import xuong_chung as pc

bp = Blueprint("soi_video", __name__)

XUONG = "soi_video"
KHO = os.path.join(pc.TAI_VE, "soi_video")
_MA = re.compile(r"^sv_\d{8}_\d{6}_[a-f0-9]{4}$")
_DUOI_VIDEO = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")

# Video dài hơn thế này thì chỉ soi đoạn đầu: phong cách của một video ngắn đã lộ hết trong vài chục giây đầu.
GIAY_TOI_DA = 180
# Số khung gửi cho AI. Nhiều hơn thì mỗi lượt hỏi chậm và đắt mà phong cách không rõ thêm.
KHUNG_TOI_DA = 24
# Video quay một lèo (bàn tay mở hộp suốt một hai phút, không cắt cảnh) thì chia thành các đoạn dài cỡ một clip của máy
# làm clip, mỗi đoạn một câu lệnh. Không chia thì cả video chỉ ra đúng một câu lệnh, không dựng lại được.
DOAN_DAI = 10.0
# File video trong máy chạy tool được đưa lên tới cỡ này (video quay bằng điện thoại 2K nặng vài trăm MB).
TEP_TOI_DA = 1024 * 1024 * 1024
# Hai khung liền nhau khác nhau quá mức này thì coi là đổi cảnh (thang 0..1 của ffmpeg).
NGUONG_DOI_CANH = 0.28
CANH_NGAN_NHAT = 0.5
AI_THU = 3

KIEU = {
    "tay": "Chỉ bàn tay cầm sản phẩm",
    "sat": "Giữ người như mẫu",
}
KHOA_SP = ("The product is the exact product shown in the attached reference image: same shape, same colours, same logo "
           "and printed text. Do not redesign it, do not add or change any text on it. The product stays sharp and fully "
           "inside the frame.")
KHOA_TAY = "HANDS ONLY: no face, no head, no torso, no other people, no mirror and no reflection of a person."
# 07/11/2026: cảnh chỉ quay sản phẩm, không người (kiểu Feelex xen giữa các cảnh tay: sản phẩm đứng trên kệ, máy lướt qua nhãn).
KHOA_KHONG_NGUOI = ("PRODUCT ONLY: no hands, no arms, no people, no face, no body parts anywhere in the frame for the whole shot. "
                    "The product stands perfectly still, front label facing the camera; only the camera moves, slowly and smoothly.")
KHOA_CHU = "No on-screen text, no subtitles, no captions, no watermark, no logo overlay, no talking, no voice-over."

_HE = (
    "You reverse-engineer the VISUAL STYLE of a short social video from still frames, so that a text-to-video model can "
    "shoot a new product video in the same style. You only see frames, so describe only what is visible; never guess a "
    "brand, a person's identity or anything said in the audio. Ignore watermarks, usernames, app logos and burned-in "
    "captions when writing prompts (but do report in Vietnamese how on-screen text is used).\n"
    "Frames that share the same shot number belong to ONE continuous shot: compare them to infer the action and the "
    "camera movement inside that shot. A shot marked \"continues the previous take\" is the next few seconds of the "
    "same uncut take: same place, same camera, same hands. For it, describe only the step that happens in THOSE "
    "seconds (compare with the frames just before), so that the shots read as consecutive steps of one sequence.\n"
    "Return ONLY JSON with this shape:\n"
    '{"tom_tat":"2-3 câu tiếng Việt: video này cho xem gì và vì sao nó cuốn người xem",\n'
    ' "phong_cach":{"kieu_hinh":"","boi_canh":"","anh_sang":"","mau_sac":"","goc_may":"","chuyen_dong":"","chu_tren_hinh":"","nhip_dung":""},\n'
    ' "khoa":"English, 35-60 words. A reusable STYLE LOCK pasted into every prompt: kind of footage (real handheld phone, '
    'studio, 3D, animation...), lighting quality, colour grade, lens and depth of field, camera feel. No location, no '
    'product, no specific action, no people identity.",\n'
    ' "canh":[{"so":1,"mo_ta":"một câu tiếng Việt tả đúng cái đang thấy trong cảnh này của video mẫu","hanh_dong":"English, '
    '30-50 words: the location of THIS shot, what is in frame, the action from start to end of the shot, the framing '
    '(close-up, top-down, eye level...) and the camera move."}]}\n'
    "Every value in phong_cach is ONE Vietnamese sentence in plain words. Write exactly one item in canh for each shot "
    "number you were given, in order.\n"
    "tom_tat, phong_cach and mo_ta describe the SAMPLE truthfully, including its real main subject. Only in khoa and "
    "hanh_dong (the English text for the new video): wherever the sample shows its own product or main subject, write "
    "\"the product\" instead, because the new video shows a different product supplied as a reference image."
)
_HE_TAY = (
    "\nHANDS-ONLY RULE: the new video may show ONLY two hands and forearms with the product, never a face or body. If a "
    "shot in the sample shows a person, rewrite hanh_dong as the closest hands-only shot (hands holding, opening, turning, "
    "pointing at, placing or using the product) that keeps the same setting, light, framing distance and camera move."
)
_HE_SAT = (
    "\nKeep people when the sample has them, described generically (apparent age range, hair, clothing, expression, what "
    "they do with the product). Never name anyone."
)


# ── Mẫu trên đĩa ─────────────────────────────────────────────────────────────

def _thu_muc(ma: str) -> str:
    if not _MA.match(ma or ""):
        raise ValueError("Mã video mẫu không hợp lệ.")
    return os.path.join(KHO, ma)


def _tep(ma: str) -> str:
    return os.path.join(_thu_muc(ma), "mau.json")


def mo(ma: str) -> Dict:
    mau = pc.doc_json(_tep(ma))
    if not mau:
        raise FileNotFoundError("Không tìm thấy video mẫu này.")
    return mau


def luu(mau: Dict) -> Dict:
    mau["sua_luc"] = time.time()
    pc.ghi_json(_tep(mau["id"]), mau)
    return mau


def tao(link: str = "", ten: str = "", kieu: str = "tay", cach: str = "thuong") -> Dict:
    ma = time.strftime("sv_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    return luu({"id": ma, "ten": (ten or "").strip()[:120] or "Video mẫu", "link": link, "nen": "", "tac_gia": "",
                "cach": cach if cach in CACH else "thuong",
                "kieu": kieu if kieu in KIEU else "tay", "tao_luc": time.time(), "trang_thai": "cho", "loi": "",
                "so_giay": 0, "rong": 0, "cao": 0, "khung": [], "nhip": {}, "tom_tat": "", "phong_cach": {}, "khoa": "", "canh": []})


def ds() -> List[Dict]:
    ra = []
    for ten in sorted(os.listdir(KHO), reverse=True) if os.path.isdir(KHO) else []:
        mau = pc.doc_json(os.path.join(KHO, ten, "mau.json")) if _MA.match(ten) else None
        if mau:
            ra.append({"id": mau["id"], "ten": mau.get("ten"), "nen": mau.get("nen"), "tac_gia": mau.get("tac_gia"),
                       "trang_thai": mau.get("trang_thai"), "tao_luc": mau.get("tao_luc"), "so_canh": len(mau.get("canh") or []),
                       "loai": loai_mau(mau), "rea": bool(mau.get("rea")),
                       "anh": ((mau.get("khung") or [{}])[0]).get("tep", "")})
    return ra


# ── Cắt khung hình ───────────────────────────────────────────────────────────

def do_video(path: str) -> Dict:
    """Số giây, chiều rộng, chiều cao của video (đã tính cả góc xoay của video quay bằng điện thoại)."""
    res = subprocess.run([pc.ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration", "-of", "json", path],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    tin = json.loads(res.stdout or b"{}")
    luong = (tin.get("streams") or [{}])[0]
    rong, cao = int(luong.get("width") or 0), int(luong.get("height") or 0)
    xoay = str((luong.get("tags") or {}).get("rotate") or next((d.get("rotation") for d in luong.get("side_data_list") or [] if "rotation" in d), 0))
    if xoay.lstrip("-") in ("90", "270"):
        rong, cao = cao, rong
    giay = float((tin.get("format") or {}).get("duration") or 0)
    if not rong or giay <= 0:
        raise RuntimeError("File này không có hình, hoặc không phải video đọc được.")
    return {"so_giay": round(giay, 2), "rong": rong, "cao": cao}


def do_doi_canh(path: str, so_giay: float) -> List[float]:
    """Các mốc giây mà video đổi cảnh, đo trên bản thu nhỏ cho nhanh."""
    res = subprocess.run([pc.ffmpeg(), "-hide_banner", "-nostats", "-i", path, "-t", str(so_giay), "-an",
                          "-vf", f"scale=320:-2,select='gt(scene,{NGUONG_DOI_CANH})',showinfo", "-f", "null", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    return sorted({round(float(m), 2) for m in re.findall(r"pts_time:([\d.]+)", res.stderr.decode("utf-8", "ignore"))})


def chia_canh(moc: List[float], so_giay: float) -> List[List[float]]:
    """Từ các mốc đổi cảnh ra danh sách [đầu, cuối] của từng cảnh. Cảnh ngắn hơn nửa giây (chớp, chuyển cảnh) gộp vào cảnh trước."""
    bien = [0.0]
    for m in moc:
        if CANH_NGAN_NHAT <= m - bien[-1] and so_giay - m >= CANH_NGAN_NHAT:
            bien.append(m)
    bien.append(so_giay)
    return [[round(bien[i], 2), round(bien[i + 1], 2)] for i in range(len(bien) - 1)]


def chia_doan(canh: List[List[float]], doan_dai: float = DOAN_DAI) -> List[List[float]]:
    """Cú máy dài hơn một clip thì chia đều thành các đoạn không quá `doan_dai` giây. Phần tử thứ ba là True nếu đoạn nối tiếp cú máy trước."""
    ra = []
    for dau, cuoi in canh:
        so = max(1, -(-int(round((cuoi - dau) * 100)) // int(doan_dai * 100)))
        # Đoạn cuối mà lẻ dưới 3 giây thì gộp vào, kẻo sinh một clip chỉ có cảnh rút tay ra.
        if so > 1 and (cuoi - dau) - (so - 1) * doan_dai < 3.0:
            so -= 1
        for i in range(so):
            ra.append([round(dau + (cuoi - dau) * i / so, 2), round(dau + (cuoi - dau) * (i + 1) / so, 2), i > 0])
    return ra


def chon_khung(canh: List[List[float]], toi_da: int = KHUNG_TOI_DA) -> List[Dict]:
    """
    Chọn mốc lấy khung. Mỗi cảnh ít nhất một khung ở giữa; cảnh dài thì lấy thêm (chừng 4 giây một khung) để AI thấy được
    chuyển động trong cảnh. Vượt quá `toi_da` thì bớt khung của cảnh dài trước, vẫn thừa thì lấy cách đều các cảnh.
    """
    so = [max(1, min(3, round((c[1] - c[0]) / 4.0))) for c in canh]
    while sum(so) > toi_da and max(so) > 1:
        so[so.index(max(so))] -= 1
    giu = list(range(len(canh)))
    if len(giu) > toi_da:
        giu = sorted({round(i * (len(canh) - 1) / (toi_da - 1)) for i in range(toi_da)})
    ra = []
    for i in giu:
        dau, cuoi = canh[i][0], canh[i][1]
        for k in range(so[i]):
            ra.append({"giay": round(dau + (cuoi - dau) * (k + 0.5) / so[i], 2), "canh": i + 1, "tu": dau, "den": cuoi,
                       "noi": bool(len(canh[i]) > 2 and canh[i][2])})
    return ra


def tinh_nhip(canh: List[List[float]], so_giay: float) -> Dict:
    dai = [c[1] - c[0] for c in canh] or [so_giay]
    return {"so_canh": len(dai), "tb": round(sum(dai) / len(dai), 1), "ngan": round(min(dai), 1), "dai": round(max(dai), 1),
            "dau_3s": sum(1 for c in canh if c[0] < 3.0), "moi_phut": round(len(dai) * 60 / max(so_giay, 1), 1)}


def cat_khung(path: str, thu_muc: str, khung: List[Dict]) -> List[Dict]:
    for cu in [f for f in os.listdir(thu_muc) if re.match(r"^k\d+\.jpg$", f)]:
        os.remove(os.path.join(thu_muc, cu))
    ra = []
    for i, k in enumerate(khung):
        tep = f"k{i + 1:02d}.jpg"
        # Cạnh dài 768: đủ cho AI thấy chi tiết, mà mười mấy tấm vẫn gửi gọn trong một lượt hỏi.
        subprocess.run([pc.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", str(k["giay"]), "-i", path, "-frames:v", "1",
                        "-vf", "scale='if(gt(iw,ih),768,-2)':'if(gt(iw,ih),-2,768)'", "-q:v", "5", os.path.join(thu_muc, tep)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if os.path.isfile(os.path.join(thu_muc, tep)):
            ra.append({**k, "tep": tep})
    if not ra:
        raise RuntimeError("Không cắt được khung hình nào từ video này.")
    return ra


# ── AI nhìn khung hình ───────────────────────────────────────────────────────

def hoi_ai_anh(he: str, de: str, anh: List[bytes], max_tokens: int = 4000) -> Dict:
    """Hỏi AI kèm ảnh JPEG, lấy JSON. 9Router trước (thử vài lượt vì vòng quay model có lúc trượt), rồi Gemini nếu có khoá."""
    import longform_studio as lf
    cd, loi = pc.cai_dat_ai(), []
    b64 = [base64.b64encode(a).decode() for a in anh]
    if cd.get("base_url"):
        for _ in range(AI_THU):
            try:
                res = requests.post(
                    f"{cd['base_url'].rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {cd.get('api_key') or '9router-key'}", "Content-Type": "application/json"},
                    json={"model": cd.get("model") or "combo1", "temperature": 0.3, "max_tokens": max_tokens, "stream": False,
                          "messages": [{"role": "system", "content": he},
                                       {"role": "user", "content": [{"type": "text", "text": de}] + [
                                           {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{x}"}} for x in b64]}]},
                    timeout=300)
                if res.status_code != 200:
                    raise ValueError(f"HTTP {res.status_code}: {res.text[:140]}")
                chu = ((res.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                return lf._extract_json(chu)
            except Exception as exc:  # noqa: BLE001
                loi.append(f"9Router: {str(exc)[:160]}")
                time.sleep(2)
    if cd.get("gemini_api_key"):
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{cd['gemini_model']}:generateContent?key={cd['gemini_api_key']}",
                json={"systemInstruction": {"parts": [{"text": he}]},
                      "contents": [{"role": "user", "parts": [{"text": de}] + [{"inline_data": {"mime_type": "image/jpeg", "data": x}} for x in b64]}],
                      "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"}},
                timeout=300)
            if res.status_code != 200:
                raise ValueError(f"HTTP {res.status_code}: {res.text[:140]}")
            dau = (res.json().get("candidates") or [{}])[0]
            return lf._extract_json("".join(x.get("text", "") for x in (dau.get("content", {}) or {}).get("parts") or []))
        except Exception as exc:  # noqa: BLE001
            loi.append(f"Gemini: {str(exc)[:160]}")
    raise RuntimeError("AI chưa nhìn được khung hình: " + ("; ".join(loi[-2:]) or "chưa cấu hình AI trong phần Cấu hình AI."))


def _gon(chu, dai: int) -> str:
    return re.sub(r"\s+", " ", str(chu or "")).strip()[:dai]


def ghep_lenh(khoa: str, hanh_dong: str, kieu: str) -> str:
    """Câu lệnh gửi máy làm clip: khoá phong cách của mẫu + việc xảy ra trong cảnh + các khoá cố định của PlayAh."""
    return " ".join(x for x in ("Vertical 9:16 video.", khoa, hanh_dong, KHOA_SP, KHOA_TAY if kieu == "tay" else KHOA_KHONG_NGUOI if kieu == "sp" else "", KHOA_CHU) if x)


def chuan(tra: Dict, khung: List[Dict], kieu: str) -> Dict:
    """Gọt câu trả lời của AI về đúng khuôn. Cảnh AI bỏ sót thì bỏ, không tự bịa; không còn cảnh nào thì báo hỏng."""
    if not isinstance(tra, dict):
        raise RuntimeError("AI trả lời sai khuôn.")
    pc_ = tra.get("phong_cach") if isinstance(tra.get("phong_cach"), dict) else {}
    khoa = _gon(tra.get("khoa"), 700)
    so_co = sorted({k["canh"] for k in khung})
    canh = []
    for c in tra.get("canh") if isinstance(tra.get("canh"), list) else []:
        try:
            so = int((c or {}).get("so"))
        except (TypeError, ValueError):
            continue
        hd = _gon((c or {}).get("hanh_dong"), 600)
        if so not in so_co or not hd or any(x["so"] == so for x in canh):
            continue
        cua = [k for k in khung if k["canh"] == so]
        canh.append({"so": so, "tu": cua[0]["tu"], "den": cua[0]["den"], "noi": bool(cua[0].get("noi")), "khung": [k["tep"] for k in cua],
                     "mo_ta": _gon(c.get("mo_ta"), 300), "hanh_dong": hd, "prompt": ghep_lenh(khoa, hd, kieu)})
    if not khoa or not canh:
        raise RuntimeError("AI nhìn khung hình nhưng không viết ra được phong cách. Bấm soi lại.")
    return {"tom_tat": _gon(tra.get("tom_tat"), 600), "khoa": khoa, "canh": sorted(canh, key=lambda x: x["so"]),
            "phong_cach": {k: _gon(pc_.get(k), 300) for k in ("kieu_hinh", "boi_canh", "anh_sang", "mau_sac", "goc_may", "chuyen_dong", "chu_tren_hinh", "nhip_dung")}}


def nhin(mau: Dict) -> Dict:
    """Gửi các khung đã cắt cho AI rồi ghi phong cách và câu lệnh vào mẫu."""
    goc = _thu_muc(mau["id"])
    khung = [k for k in mau.get("khung") or [] if os.path.isfile(os.path.join(goc, k["tep"]))]
    if not khung:
        raise RuntimeError("Mẫu này không còn khung hình. Soi lại từ link.")
    anh = []
    for k in khung:
        with open(os.path.join(goc, k["tep"]), "rb") as f:
            anh.append(f.read())
    nhip = mau.get("nhip") or {}
    de = (f"The sample video is {mau.get('so_giay')} seconds long, {mau.get('rong')}x{mau.get('cao')} pixels, "
          f"{nhip.get('so_canh')} shots in total (average {nhip.get('tb')} s per shot).\n"
          "The attached frames, in order:\n" +
          "\n".join(f"- frame {i + 1}: shot {k['canh']} (from {k['tu']}s to {k['den']}s{', continues the previous take' if k.get('noi') else ''}), "
                    f"taken at {k['giay']}s" for i, k in enumerate(khung)) +
          "\nShot numbers to write: " + ", ".join(str(s) for s in sorted({k['canh'] for k in khung})) + "." + _rea_loi_dan(mau, khung))
    tra = hoi_ai_anh(_HE + (_HE_TAY if mau.get("kieu") == "tay" else _HE_SAT), de, anh, max_tokens=6000)
    mau.update(chuan(tra, khung, mau.get("kieu") or "tay"))
    mau.update(trang_thai="xong", loi="")
    return luu(mau)


# ── Soi kỹ bằng rea-video (10/11/2026) ───────────────────────────────────────
# Người dùng: "import cái repo rea vào tool luôn". rea-video (github.com/JuneYaooo/rea-video, Apache-2.0, bản 08/11/2026
# nằm ở vendor/rea-video, chạy trong rea_venv Python 3.12 riêng) tách cảnh và đo từng cảnh bằng số liệu điểm ảnh: cỡ khung,
# máy đứng yên hay chạy, ánh sáng, màu, chữ trên hình; cả video thì nhịp cắt, có lời hay nhạc. Tool dùng chỗ cắt cảnh của nó
# thay cho ffmpeg, và đưa các số đo cho AI cùng khung hình để viết câu lệnh. Chạy KHÔNG có --vlm (AI của tool đã nhìn hình),
# nên nhãn "quay thật / AI / 3D" của rea chỉ đoán theo điểm ảnh, hay sai: lời dặn AI nói rõ điều đó. rea hỏng thì soi như cũ.
GOC_TOOL = os.path.dirname(os.path.abspath(__file__))
REA = os.path.join(GOC_TOOL, "rea_venv", "bin", "rea-video")
REA_CHO_TOI_DA = 1500
# 13/11/2026 (Toolmevabe): hai chế độ đặt tên lại theo lời người dùng. Rea-Video cài riêng từ repo (vendor/rea-video, xem
# NGUON.txt) và bật --vlm: rea gọi model nhìn hình qua 9Router bằng đúng cài đặt AI của tool (`pc.cai_dat_ai`), tức tài khoản
# Codex/Gemini người dùng đã nối trong 9Router, không cần khoá riêng. Thử 13/11 trên 15 giây "HF CORE": rea chỉ đo điểm ảnh
# thì đoán "AI 57%", có model nhìn hình thì ra đúng "quay thật 100%", kèm lời tả từng cảnh và câu lệnh quay gợi ý.
CACH = {"thuong": "AI Scan", "rea": "Rea-Video"}
REA_SO_CANH_AI = 20   # tối đa ngần ấy lượt hỏi model nhìn hình mỗi video (rea chọn cảnh khó đoán nhất trước)


def co_rea() -> bool:
    return os.access(REA, os.X_OK)


def _rea_ket(x, *khoa):
    for k in khoa:
        x = x.get(k) if isinstance(x, dict) else None
    return x


def rea_tom(bd: Dict) -> Dict:
    """Gọt breakdown.json của rea-video (vài trăm KB) còn phần tool dùng: các dòng tả cả video và số đo từng cảnh."""
    tom = bd.get("summary") or {}
    am = bd.get("audio") or {}
    canh = []
    for sh in bd.get("shots") or []:
        asp = sh.get("aspects") or {}
        ks = _rea_ket(asp, "framing", "result") or {}
        may = _rea_ket(asp, "camera", "result") or {}
        den = _rea_ket(asp, "lighting", "result") or {}
        mau_ = _rea_ket(asp, "grade", "result") or {}
        chu = sorted({str(l.get("kind")) for l in sh.get("layers") or [] if isinstance(l, dict) and l.get("kind")})
        ai = sh.get("vlm") if isinstance(sh.get("vlm"), dict) else {}
        nhan = ai.get("replication") if isinstance(ai.get("replication"), dict) else {}
        canh.append({"tu": round(float(sh.get("start") or 0), 2), "den": round(float(sh.get("end") or 0), 2),
                     "co": ks.get("shot_size") or "", "goc": ks.get("camera_angle") or "", "vi_tri": ks.get("position") or "",
                     "may": " ".join(x for x in (may.get("support"), may.get("move")) if x),
                     "anh_sang": "; ".join(str(den.get(k)) for k in ("key_direction", "quality", "color_temp") if den.get(k)),
                     "mau": ", ".join(str(mau_.get(k)) for k in ("look", "saturation", "contrast") if mau_.get(k)),
                     "chu": chu, "loai": _rea_ket(sh, "base", "label") or "", "mat": float(sh.get("faces") or 0) > 0,
                     # Phần model nhìn hình (khi chạy --vlm): lời tả cảnh (rea thường viết tiếng Trung) và câu lệnh quay gợi ý.
                     "ai_ta": str(ai.get("description") or "")[:300], "ai_lenh": str(nhan.get("video_prompt_en") or nhan.get("video_prompt") or "")[:500]})
    ca = bd.get("whole_video_vlm") if isinstance(bd.get("whole_video_vlm"), dict) else {}
    ai_tt = _rea_ket(bd, "analysis", "vlm") or {}
    return {"ket_luan": str(tom.get("verdict") or "")[:300], "dong": [str(x)[:400] for x in (tom.get("aspects") or [])][:8],
            "quay": [str(x)[:300] for x in _rea_ket(bd, "shooting_plan", "steps") or []][:6],
            "tieng": {"loi": bool(am.get("speech_likely")), "nhac": bool(am.get("music_likely")), "bpm": am.get("tempo_bpm"),
                      "cat_theo_nhip": bool(am.get("beat_synced_edit"))},
            "cat_moi_phut": round(len(canh) * 60 / max(float(tom.get("duration") or 1), 1), 1), "canh": canh,
            "ai": {"co": bool(ai_tt.get("used")), "model": str(ai_tt.get("model") or "")[:80], "tom_tat": str(ca.get("summary") or "")[:400],
                   "hook": str(ca.get("hook") or "")[:300], "diem": [str(x)[:200] for x in (ca.get("highlights") or [])][:4],
                   "doan": [{"tu": d.get("start"), "den": d.get("end"), "vai": d.get("role"), "viec": str(d.get("beat") or "")[:200]}
                            for d in (ca.get("segments") or []) if isinstance(d, dict)][:12]}}


def rea_soi(nguon: str, goc: str, viec: Dict) -> Dict:
    """Chạy rea-video trên file video, trả bản gọn. Mọi file của nó (tách tiếng, ảnh từng cảnh, chừng 15 MB một video) bị
    xoá vì ổ đĩa gần đầy. Ảnh storyboard của nó cũng không giữ: nó in nhãn tiếng Trung "AI 视频 58%" lên cả video quay thật."""
    ra = os.path.join(goc, "rea_tam")
    shutil.rmtree(ra, ignore_errors=True)
    try:
        lenh = [REA, "decompose", nguon, "-o", ra, "--lang", "en"]
        moi_truong = dict(os.environ)
        cd = pc.cai_dat_ai()
        if cd.get("base_url"):
            lenh += ["--vlm", "--max-vlm-shots", str(REA_SO_CANH_AI)]
            moi_truong.update(REA_VLM_BASE_URL=cd["base_url"].rstrip("/"), REA_VLM_API_KEY=cd.get("api_key") or "9router-key",
                              REA_VLM_MODEL=cd.get("model") or "combo1", REA_VLM_MEDIA="frames", REA_VLM_TIMEOUT="240")
        p = subprocess.Popen(lenh, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=GOC_TOOL, env=moi_truong)
        bat_dau = time.time()
        while p.poll() is None:
            if viec.get("huy") or time.time() - bat_dau > REA_CHO_TOI_DA:
                p.kill()
                raise RuntimeError("Đã dừng." if viec.get("huy") else f"rea-video chạy quá {REA_CHO_TOI_DA // 60} phút.")
            time.sleep(1)
        duoi = (p.stdout.read() or b"").decode("utf-8", "ignore")[-400:]
        tep = os.path.join(ra, "breakdown.json")
        if p.returncode != 0 or not os.path.isfile(tep):
            raise RuntimeError(f"rea-video hỏng (mã {p.returncode}): {duoi.strip()[-200:]}")
        with open(tep, encoding="utf-8") as f:
            tom = rea_tom(json.load(f))
        if not tom["canh"]:
            raise RuntimeError("rea-video không tách được cảnh nào.")
        return tom
    finally:
        shutil.rmtree(ra, ignore_errors=True)


def _rea_cho_canh(rea: Dict, tu: float, den: float) -> List[Dict]:
    """Các cảnh của rea-video nằm chồng lên đoạn [tu, den] của tool (đoạn của tool có thể gộp hay chia cảnh của rea)."""
    return [c for c in rea.get("canh") or [] if min(den, c["den"]) - max(tu, c["tu"]) > 0.15]


_TEN_LOP = {"huazi": "styled text", "subtitle": "subtitles", "title": "title card", "sticker": "stickers", "pip": "picture-in-picture"}


def _rea_loi_dan(mau: Dict, khung: List[Dict]) -> str:
    rea = mau.get("rea") or {}
    if not rea.get("canh"):
        return ""
    dong = ["", "MEASUREMENTS from a shot-analysis tool (pixel statistics, no human check). Use them for framing, camera, "
            "light and pacing words when they agree with the frames; when they disagree, trust the frames. Its guess of how "
            "a shot was produced (live action / AI / 3D) is often wrong: ignore it.",
            "Whole video: " + " | ".join(rea.get("dong") or [])]
    t = rea.get("tieng") or {}
    dong.append(f"Audio: {'voice-over/talking' if t.get('loi') else 'no clear speech'}, {'music' if t.get('nhac') else 'no clear music'}"
                + (f" at about {round(t['bpm'])} bpm" if t.get("bpm") else "") + ("; cuts land on the beat" if t.get("cat_theo_nhip") else "")
                + f"; {rea.get('cat_moi_phut')} cuts per minute.")
    ai = rea.get("ai") or {}
    if ai.get("co"):
        dong.append("A vision model already watched the video (its notes may be in Chinese; read them, write your answer as asked):")
        if ai.get("tom_tat"):
            dong.append("Summary: " + ai["tom_tat"] + (" Hook: " + ai["hook"] if ai.get("hook") else ""))
        for d in ai.get("doan") or []:
            dong.append(f"Beat {d.get('tu')}-{d.get('den')}s [{d.get('vai')}]: {d.get('viec')}")
    for so in sorted({k["canh"] for k in khung}):
        k = next(x for x in khung if x["canh"] == so)
        ta = [c for c in _rea_cho_canh(rea, k["tu"], k["den"]) if c.get("ai_ta") or c.get("ai_lenh")]
        if ta:
            dong.append(f"Shot {so} as the vision model saw it: " + " / ".join(
                (c.get("ai_lenh") or c.get("ai_ta")) for c in ta[:2]))
        do = [c for c in _rea_cho_canh(rea, k["tu"], k["den"]) if c["co"] or c["may"] or c["anh_sang"]]
        if not do:
            continue
        mo_ta = "; ".join(f"{c['co'] or '?'} {c['goc']} {c['vi_tri']}, camera {c['may'] or '?'}, light {c['anh_sang'] or '?'}, grade {c['mau'] or '?'}"
                          + (f", overlays {'/'.join(_TEN_LOP.get(x, x) for x in c['chu'])}" if c["chu"] else "") + (", a face is visible" if c["mat"] else "")
                          for c in do[:3])
        dong.append(f"Shot {so} ({k['tu']}-{k['den']}s, {len(do)} source shot(s)): {re.sub(r' +', ' ', mo_ta)}")
    return "\n".join(dong)


def soi(mau: Dict, viec: Dict, tep_co_san: str = "", giu_nguon: bool = False) -> Dict:
    """
    Trọn một lượt: tải (hoặc dùng file đã đưa lên), cắt khung, cho AI nhìn. Video nguồn bị xoá sau khi cắt khung, TRỪ KHI
    `giu_nguon`: đó là file của người dùng nằm sẵn trong máy, tool chỉ đọc chứ không được xoá.
    """
    goc = _thu_muc(mau["id"])
    nguon = tep_co_san
    try:
        if not nguon:
            import tai_link
            pc.buoc(viec, 5, f"Đang tải video từ {tai_link.ten_nen(mau['link'])}")
            got = tai_link.tai_video(mau["link"], goc)
            nguon = got["file"]
            mau.update(nen=got["nen"], tac_gia=got.get("tac_gia") or "")
            if got.get("ten"):
                mau["ten"] = got["ten"]
        if viec.get("huy"):
            return mau
        do = do_video(nguon)
        soi_toi = min(do["so_giay"], GIAY_TOI_DA)
        moc = None
        mau.pop("rea", None)
        if mau.get("cach") == "rea" and co_rea():
            pc.buoc(viec, 20, "rea-video đang soi kỹ từng cảnh (cỡ khung, máy, ánh sáng, nhịp cắt), chừng một phút")
            try:
                mau["rea"] = rea_soi(nguon, goc, viec)
                mau["rea_loi"] = ""
                moc = [c["tu"] for c in mau["rea"]["canh"] if 0 < c["tu"] < soi_toi]
            except Exception as exc:  # noqa: BLE001
                if viec.get("huy"):
                    return mau
                mau["rea_loi"] = str(exc)[:300]
        if moc is None:
            pc.buoc(viec, 30, "Đang đo chỗ đổi cảnh")
            moc = do_doi_canh(nguon, soi_toi)
        canh = chia_canh(moc, soi_toi)
        doan = chia_doan(canh)
        pc.buoc(viec, 50, f"Đang cắt khung hình của {len(doan)} đoạn")
        mau.update(do, khung=cat_khung(nguon, goc, chon_khung(doan)), nhip=tinh_nhip(canh, soi_toi),
                   cat_bot=do["so_giay"] > GIAY_TOI_DA, trang_thai="co_khung")
        luu(mau)
    finally:
        if nguon and not giu_nguon and os.path.isfile(nguon):
            os.remove(nguon)
    if viec.get("huy"):
        return mau
    pc.buoc(viec, 65, f"AI đang nhìn {len(mau['khung'])} khung hình để viết phong cách và câu lệnh")
    return nhin(mau)


# ── Đưa sang xưởng Review sản phẩm ───────────────────────────────────────────

# Chỉ các ĐỘNG TÁC chắc chắn của gel, kem, bọt. Không dùng danh từ (gel, cream...) hay "spread": mẫu hộp áo mưa cũng có
# "the packets spread across the table" (đã gặp 04/11/2026 với HERFEEL SEN, HF CORE, CAT TONGUE).
_CHU_GEL = re.compile(r"\b(squeez\w*|pump(s|ed|ing)?|dispens\w*|rubs?|rubbing|lather\w*|smear\w*|massag\w*)\b", re.I)


def loai_mau(mau: Dict) -> str:
    """Mẫu quay loại gì: có cảnh nặn, bơm, bôi, xoa gel, kem, bọt thì là "gel", không thì "hop". Đọc câu lệnh các bước."""
    return "gel" if any(_CHU_GEL.search(c.get("hanh_dong") or "") for c in mau.get("canh") or []) else "hop"


_HE_THICH_UNG = (
    "You adapt the shot list of a competitor's product video so it can be re-shot with a DIFFERENT, single product. You only "
    "return JSON."
)
_DE_THICH_UNG = """Our product (exactly ONE unit; this is everything that physically exists): {ta}

Shot list taken from a competitor's video (one action per step):
{buoc}

For EACH step decide:
- keep it as is if it works with our single product;
- rewrite it if it mentions things our product does not have (several bottles, a product line-up, different flavours, sizes or
  colours, other brands, a different container shape, props that are part of their product): keep the same camera, framing,
  motion and mood, but show only our one product, its real packaging and its real contents;
- drop it ("giu": false) only if it cannot work at all without those missing things.
Never invent features, flavours, sizes or text. Do not name any brand.

Return JSON: {{"buoc": [{{"so": 1, "giu": true, "hanh_dong": "the step in English, one or two sentences", "ly_do": "Vietnamese, short, only if changed or dropped"}}]}}"""


def thich_ung(mau: Dict, da: Dict) -> List[Dict]:
    """
    Các bước của mẫu đã sửa cho hợp với sản phẩm của video (thêm ngày 05/11/2026). Mẫu học từ video đối thủ hay có bước PlayAh
    không có (nhiều chai, 5 vị, nhiều cỡ): máy làm clip cố quay theo thì bịa thêm chai, thêm vị. AI đọc các bước cùng đoạn tả
    sản phẩm, giữ / viết lại / bỏ từng bước. Kết quả nhớ trong file mẫu theo sản phẩm (và theo nội dung các bước, soi lại là
    tính lại). AI hỏng hay sản phẩm chưa có phần tả thì trả các bước gốc.
    """
    import hashlib
    import kho_anh
    canh = mau.get("canh") or []
    ten = (da.get("san_pham") or {}).get("kho") or ""
    ta = (kho_anh.thong_tin(ten).get("nhan_dien") or "") if ten else ""
    goc = [{"so": c.get("so"), "giu": True, "hanh_dong": c.get("hanh_dong") or "", "ly_do": ""} for c in canh]
    if not ta or not canh:
        return goc
    dau = hashlib.sha1((ta + "|" + "|".join(c.get("hanh_dong") or "" for c in canh)).encode("utf-8")).hexdigest()[:12]
    nho = (mau.get("thich_ung") or {}).get(kho_anh.gon(ten)) or {}
    if nho.get("dau") == dau:
        return nho["buoc"]
    try:
        got = pc.hoi_ai(_HE_THICH_UNG, _DE_THICH_UNG.format(
            ta=ta, buoc="\n".join(f"{i + 1}. {c.get('hanh_dong')}" for i, c in enumerate(canh))), max_tokens=3000)
        ra = []
        for i, c in enumerate(canh):
            x = next((b for b in got.get("buoc") or [] if str(b.get("so")) == str(i + 1)), None)
            if not x or not str(x.get("hanh_dong") or "").strip():
                ra.append(goc[i])
                continue
            ra.append({"so": c.get("so"), "giu": x.get("giu") is not False, "hanh_dong": _gon(x.get("hanh_dong"), 600),
                       "ly_do": _gon(x.get("ly_do"), 200)})
        if not any(b["giu"] for b in ra):
            return goc
    except Exception:  # noqa: BLE001 - AI hỏng thì dùng các bước gốc, không chặn video
        return goc
    try:
        moi = mo(mau["id"])
        moi.setdefault("thich_ung", {})[kho_anh.gon(ten)] = {"dau": dau, "buoc": ra, "luc": time.time()}
        luu(moi)
    except Exception:  # noqa: BLE001
        pass
    return ra


def _giay_clip(da: Dict, muon: float) -> int:
    """
    Số giây mỗi clip máy làm clip của video này làm được, gần `muon` nhất. Omni 1.2 và Veo không cho chọn số giây (ra 8 tới
    10 giây) nên lấy đúng độ dài đó: chọn 5 giây thì số clip gấp đôi mà clip vẫn dài 10 giây, tốn credit vô ích (07/11/2026).
    """
    import review_sp as rv
    c = rv.tim_cach(rv.cach_quay(da))
    if c["may"] == "flow" and not c["model"].startswith("Omni 1.1"):
        return int(c["giay"])
    return min(rv.GIAY_FLOW if c["may"] == "flow" else rv.GIAY_CLIP, key=lambda g: abs(g - muon))


def phong_cach(mau: Dict) -> str:
    """
    CHỈ phần phong cách của một mẫu (13/11/2026, Lên ý tưởng mẻ): ánh sáng, góc máy, màu, chuyển động, nhịp cắt. Cố ý bỏ bối cảnh
    (`boi_canh`) và các bước của mẫu: AI lên ý tưởng không được chép trình tự cảnh hay nơi quay của mẫu.
    """
    pc_ = mau.get("phong_cach") if isinstance(mau.get("phong_cach"), dict) else {}
    dong = [f"- {nhan}: {pc_[k]}" for k, nhan in (("kieu_hinh", "Kiểu hình"), ("anh_sang", "Ánh sáng"), ("mau_sac", "Màu"),
                                                   ("goc_may", "Góc máy"), ("chuyen_dong", "Chuyển động máy"), ("nhip_dung", "Nhịp dựng"))
            if str(pc_.get(k) or "").strip()]
    nhip = mau.get("nhip") or {}
    if nhip.get("tb"):
        dong.append(f"- Nhịp đo được: trung bình {nhip['tb']} giây một cảnh, {nhip.get('moi_phut', '?')} lần cắt mỗi phút")
    for d in (mau.get("rea") or {}).get("dong") or []:
        if str(d).startswith(("Framing:", "Camera:", "Lighting:", "Grade:", "Editing:")):
            dong.append(f"- (đo bằng rea-video) {str(d)[:300]}")
    return "\n".join(dong)


def ap_mau(da: Dict, mau: Dict, so_giay: int = 0) -> Dict:
    """
    Điền các bước của một mẫu vào một video Review: mỗi bước là một clip có câu lệnh sẵn. `so_giay` > 0 thì video dài đúng
    chừng đó (lấy các bước đầu của mẫu cho vừa); bằng 0 thì dài theo số bước của mẫu, tối đa 90 giây.

    Sản phẩm thuộc loại có trình tự riêng (gel: nặn ra, bôi lên tay) thì chỉ mượn PHONG CÁCH của mẫu (góc máy, ánh sáng, mặt
    bàn), còn các bước là trình tự của loại đó: bắt gel đi theo các bước mở hộp đổ gói của mẫu thì ra video sai sản phẩm.
    """
    import review_sp as rv
    canh = mau.get("canh") or []
    if not canh:
        raise ValueError("Mẫu này chưa có câu lệnh nào. Soi xong đã rồi mới làm video.")
    # Mẫu KHÁC loại với sản phẩm (gel theo mẫu mở hộp đổ gói, hay hộp theo mẫu nặn gel) thì chỉ mượn phong cách; mẫu CÙNG
    # loại thì theo đúng các bước của mẫu (mẫu gel có cảnh xả nước, xoa tay... mà trình tự cố định không có).
    loai = da.get("loai_sp") or "hop"
    if loai in rv.TRINH_TU and loai_mau(mau) != loai:
        giay = _giay_clip(da, 10)
        tong = max(15, min(90, int(so_giay) if so_giay else 50))
        cac = rv.trinh_tu(da)[:max(1, tong // giay)]
        da.update(giay_clip=giay, so_giay=tong, mau={"id": mau["id"], "ten": mau.get("ten")},
                  canh=[{"ma": f"c{i + 1:02d}", "hanh_dong": hd, "prompt": ghep_lenh(mau.get("khoa") or "", rv._HD[hd][1], "tay")[:3000],
                         "tu_sua": True, "trang_thai": "cho"} for i, hd in enumerate(cac)])
        rv.lap_canh(da)
        return da
    # Mỗi bước của mẫu dài gần mức nào thì clip dài mức đó: mẫu cắt nhanh ra clip 5 giây, mẫu quay một lèo ra clip 10 giây.
    tb = sum(c["den"] - c["tu"] for c in canh) / len(canh)
    giay = _giay_clip(da, tb)
    tong = max(15, min(90, int(so_giay) if so_giay else len(canh) * giay))
    # Bước nào của mẫu không hợp sản phẩm (nhiều chai, nhiều vị...) thì viết lại hoặc bỏ (thich_ung).
    buoc = thich_ung(mau, da)
    kieu = mau.get("kieu") or "tay"
    lenh = [c["prompt"] if b["hanh_dong"] == (c.get("hanh_dong") or "") else ghep_lenh(mau.get("khoa") or "", b["hanh_dong"], kieu)
            for c, b in zip(canh, buoc) if b["giu"]] or [c["prompt"] for c in canh]
    da["mau_sua"] = [{"so": b["so"], "giu": b["giu"], "ly_do": b["ly_do"]} for b in buoc if b.get("ly_do") or not b["giu"]]
    lenh = lenh[:max(1, tong // giay)]
    da.update(giay_clip=giay, so_giay=tong, mau={"id": mau["id"], "ten": mau.get("ten")},
              canh=[{"ma": f"c{i + 1:02d}", "hanh_dong": rv.HANH_DONG[i % len(rv.HANH_DONG)][0], "prompt": p[:3000],
                     "tu_sua": True, "trang_thai": "cho"} for i, p in enumerate(lenh)])
    rv.lap_canh(da)
    return da


def tao_review(mau: Dict, san_pham: str = "", tags=None, concept: str = "", ai_soan: bool = False) -> Dict:
    """Mở một video mới bên Review sản phẩm, mỗi bước của mẫu thành một clip với câu lệnh điền sẵn. Có tên sản phẩm thì lấy ảnh từ Kho ảnh."""
    import review_sp as rv
    if ai_soan:
        # 10/11/2026: AI (astra 6) soạn trọn bộ cảnh từ khung hình + số đo rea của mẫu, code gắn câu khoá (codex_canh.py).
        if not (san_pham or "").strip():
            raise ValueError("Chọn sản phẩm trước: AI soạn cảnh cần ảnh và đoạn tả của sản phẩm.")
        import codex_canh
        return codex_canh.tao_video(mau, san_pham)
    if not mau.get("canh"):
        raise ValueError("Mẫu này chưa có câu lệnh nào. Soi xong đã rồi mới làm video.")
    da = rv.tao("Theo mẫu: " + (mau.get("ten") or "")[:60])
    if (san_pham or "").strip():
        import kho_anh
        da["loai_sp"] = kho_anh.loai_cua(san_pham)
    da = rv.luu(ap_mau(da, mau))
    if (san_pham or "").strip():
        da = kho_anh.dua_vao_review(da["id"], san_pham, tags)
    if concept:
        da = rv.mo(da["id"])
        da["mau"] = {**(da.get("mau") or {}), "khoa": mau.get("khoa") or ""}
        da = rv.luu(rv.ap_concept(da, concept))
    return da


# ── Đường vào máy chủ ────────────────────────────────────────────────────────

def _loi(exc, ma=400):
    return jsonify({"ok": False, "error": str(exc)[:400]}), (404 if isinstance(exc, FileNotFoundError) else ma)


def _ban() -> Optional[str]:
    dang = pc.moi_viec_dang_chay()
    return f"Máy đang chạy một việc khác ({dang[0].get('step') or dang[0].get('kind')}). Chờ xong rồi soi." if dang else None


def _chay(mau: Dict, lam) -> str:
    def viec_nen(viec):
        try:
            lam(viec)
        except Exception as exc:  # noqa: BLE001
            moi = pc.doc_json(_tep(mau["id"])) or mau
            moi.update(trang_thai="loi" if not moi.get("khung") else "co_khung", loi=str(exc)[:400])
            luu(moi)
            raise
    return pc.bat_dau(XUONG, "soi", viec_nen, mau["id"])


@bp.route("/api/soi_video/phien", methods=["GET"])
def api_phien():
    import kho_anh
    return jsonify({"ok": True, "mau": ds(), "job_id": pc.viec_dang_chay(XUONG), "kieu": KIEU, "giay_toi_da": GIAY_TOI_DA,
                    "cach": CACH, "co_rea": co_rea(),
                    "san_pham": [{"ten": s["ten"], "so_anh": s["so_anh"], "loai": kho_anh.loai_cua(s["ten"])} for s in kho_anh.ds_san_pham()]})


@bp.route("/api/soi_video/soi", methods=["POST"])
def api_soi():
    """Nhận link (JSON hoặc form) hoặc một file video, mở việc nền soi."""
    try:
        if _ban():
            raise ValueError(_ban())
        # Video quay bằng điện thoại nặng hơn mức đưa lên chung của tool (200 MB), riêng đường này nới ra.
        request.max_content_length = TEP_TOI_DA
        form = request.form if (request.files or request.form) else (request.get_json(silent=True) or {})
        kieu = form.get("kieu") if form.get("kieu") in KIEU else "tay"
        # Mặc định soi kỹ bằng rea-video khi máy đã cài nó; trang gửi cach="thuong" để soi nhanh như cũ.
        cach = form.get("cach") if form.get("cach") in CACH else ("rea" if co_rea() else "thuong")
        tep = request.files.get("file")
        trong_may = os.path.expanduser(str(form.get("url") or "").strip().strip('"\''))
        if not (tep and tep.filename) and trong_may.startswith("/"):
            # Dán đường dẫn một file nằm sẵn trong máy chạy tool: đọc thẳng, không chép, không xoá. Máy khác trong WiFi không
            # được dùng đường này, kẻo ai cũng dò được file trên máy chủ.
            if request.remote_addr not in ("127.0.0.1", "::1"):
                raise ValueError("Chỉ máy đang chạy tool mới dán được đường dẫn file. Ở máy này hãy bấm chọn file video.")
            if os.path.splitext(trong_may)[1].lower() not in _DUOI_VIDEO or not os.path.isfile(trong_may):
                raise ValueError("Không thấy file video ở đường dẫn này (nhận MP4, MOV, WEBM, MKV).")
            mau = tao("", os.path.splitext(os.path.basename(trong_may))[0], kieu, cach)
            mau["nen"] = "File trong máy"
            ma = _chay(luu(mau), lambda viec: soi(mau, viec, trong_may, giu_nguon=True))
        elif tep and tep.filename:
            duoi = os.path.splitext(tep.filename)[1].lower()
            if duoi not in _DUOI_VIDEO:
                raise ValueError("Chỉ nhận file video: MP4, MOV, WEBM, MKV.")
            mau = tao("", os.path.splitext(os.path.basename(tep.filename))[0], kieu, cach)
            mau["nen"] = "File trong máy"
            dich = os.path.join(_thu_muc(mau["id"]), "nguon" + duoi)
            tep.save(dich)
            ma = _chay(luu(mau), lambda viec: soi(mau, viec, dich))
        else:
            import tai_link
            url = tai_link.kiem_link(form.get("url") or "")
            mau = tao(url, "", kieu, cach)
            ma = _chay(mau, lambda viec: soi(mau, viec))
        return jsonify({"ok": True, "id": mau["id"], "job_id": ma})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


@bp.route("/api/soi_video/mau/<ma>", methods=["GET", "DELETE"])
def api_mau(ma):
    try:
        if request.method == "DELETE":
            if pc.viec_dang_chay(XUONG):
                raise ValueError("Đang soi một video. Chờ xong rồi xoá.")
            mo(ma)
            shutil.rmtree(_thu_muc(ma), ignore_errors=True)
            return jsonify({"ok": True})
        return jsonify({"ok": True, "mau": mo(ma)})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


@bp.route("/api/soi_video/mau/<ma>/viet_lai", methods=["POST"])
def api_viet_lai(ma):
    """Cho AI nhìn lại các khung đã cắt (không tải lại video), có thể đổi kiểu câu lệnh."""
    try:
        if _ban():
            raise ValueError(_ban())
        mau = mo(ma)
        kieu = (request.get_json(silent=True) or {}).get("kieu")
        if kieu in KIEU:
            mau["kieu"] = kieu
        if not mau.get("khung"):
            raise ValueError("Mẫu này chưa cắt được khung hình. Soi lại từ link.")
        luu(mau)

        def lam(viec):
            pc.buoc(viec, 40, f"AI đang nhìn lại {len(mau['khung'])} khung hình")
            nhin(mau)
        return jsonify({"ok": True, "job_id": _chay(mau, lam)})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


@bp.route("/api/soi_video/mau/<ma>/tao_review", methods=["POST"])
def api_tao_review(ma):
    try:
        moi = request.get_json(silent=True) or {}
        da = tao_review(mo(ma), moi.get("san_pham") or "", moi.get("tags"), moi.get("concept") or "", bool(moi.get("ai_soan")))
        return jsonify({"ok": True, "pid": da["id"], "ten": da["ten"], "so_clip": len(da["canh"]), "co_anh": bool(da["san_pham"]["anh"])})
    except Exception as exc:  # noqa: BLE001
        return _loi(exc)


@bp.route("/api/soi_video/tep/<ma>/<ten>", methods=["GET"])
def api_tep(ma, ten):
    try:
        return send_from_directory(_thu_muc(ma), os.path.basename(ten))
    except ValueError as exc:
        return _loi(exc)


@bp.route("/api/soi_video/viec/<ma>", methods=["GET"])
def api_viec(ma):
    viec = pc.xem_viec(ma)
    return jsonify({"ok": True, **viec}) if viec else _loi(FileNotFoundError("Không tìm thấy việc này."))


@bp.route("/api/soi_video/viec/<ma>/huy", methods=["POST"])
def api_huy(ma):
    return jsonify({"ok": pc.huy_viec(ma)})
