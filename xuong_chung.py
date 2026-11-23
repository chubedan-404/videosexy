"""Đồ dùng chung của hai xưởng PlayAh (Review sản phẩm, Bé Bao kể chuyện): việc chạy nền, hỏi AI, đọc giọng, ffmpeg."""
import json
import re
import os
import shutil
import subprocess
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TAI_VE = os.path.join(BASE_DIR, "downloads")
PUBLIC = os.path.join(BASE_DIR, "remotion", "public")

# Giọng nhân bản đọc tiếng Việt khoảng 4,2 tiếng mỗi giây. Dùng để ước số chữ cho vừa số giây.
TIENG_MOI_GIAY = 4.2
# Lời dài quá số giây thì ép nhanh lên, nhưng không quá mức này: nhanh hơn nữa nghe méo.
EP_NHANH_TOI_DA = 1.2

NHAT_KY = os.path.join(TAI_VE, "nhat_ky_xuong.jsonl")
# Tốc độ đọc thật của từng giọng nhân bản (tiếng mỗi giây), đo sau mỗi lần đọc. Giọng chậm đọc 2,9 tiếng/giây trong khi
# TIENG_MOI_GIAY giả định 4,2: lời viết "vừa 60 giây" đọc ra 85 giây (gặp 07/11/2026 ở Bé Bao).
TOC_DO_GIONG = os.path.join(TAI_VE, "toc_do_giong.json")

_VIEC: Dict[str, Dict] = {}
_KHOA = threading.Lock()
# Việc nền nào đang chạy trên luồng này (đặt trong bat_dau). Dùng để nhớ video nào việc nền đang giữ trong bộ nhớ.
_LUONG = threading.local()
# Khoá nội bộ của việc nền, không trả cho trang (giữ nguyên dạng trả về của xem_viec / moi_viec_dang_chay).
_KHOA_AN = ("huy", "giu")
DANG_LAM_LOI = "Video đang làm, chờ xong rồi sửa."


def ffmpeg() -> str:
    local = os.path.join(BASE_DIR, "ffmpeg")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe() -> str:
    local = os.path.join(BASE_DIR, "ffprobe")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which("ffprobe") or "ffprobe"


def chay(args: List[str], timeout: int = 900) -> None:
    res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg lỗi: " + res.stderr.decode("utf-8", "ignore").strip()[-300:])


def so_giay(path: str) -> float:
    try:
        res = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        return float(json.loads(res.stdout or b"{}").get("format", {}).get("duration") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def doc_json(path: str, mac_dinh=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return mac_dinh


def ghi_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Mỗi lượt ghi một file tạm riêng (13/11/2026). Trước đây mọi lượt dùng chung `<file>.tam`: hai luồng ghi cùng file
    # (việc nền và nút Lưu) có thể đè lên file tạm của nhau, đổi tên một file ghi dở vào chỗ thật, rồi doc_json đọc hỏng
    # trả về rỗng. Định dạng file và cách đổi tên vào chỗ cũ giữ nguyên.
    tam = f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tam"
    try:
        with open(tam, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tam, path)
    except BaseException:
        try:
            os.remove(tam)
        except OSError:
            pass
        raise


# ── Nhật ký vận hành (bảng Tổng quan đọc file này) ──────────────────────────

def _ai() -> str:
    """Máy nào bấm: địa chỉ trong mạng nội bộ, "máy chủ" nếu là chính máy chạy tool."""
    try:
        from flask import has_request_context, request
        if has_request_context():
            return "máy chủ" if request.remote_addr in ("127.0.0.1", "::1") else str(request.remote_addr)
    except Exception:  # noqa: BLE001
        pass
    return ""


def ghi_nhat_ky(xuong: str, viec: str, du_an: str = "", ket_qua: str = "done", giay: float = 0.0, loi: str = "", ai: str = "", ten: str = "") -> None:
    dong = {"luc": time.time(), "ai": ai or _ai(), "xuong": xuong, "viec": viec, "du_an": du_an, "ten": ten,
            "ket_qua": ket_qua, "giay": round(giay, 1), "loi": loi[:200]}
    try:
        os.makedirs(TAI_VE, exist_ok=True)
        with _KHOA, open(NHAT_KY, "a", encoding="utf-8") as f:
            f.write(json.dumps(dong, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - ghi nhật ký hỏng không được làm hỏng việc chính
        pass


def doc_nhat_ky(toi_da: int = 3000) -> List[Dict]:
    try:
        with open(NHAT_KY, encoding="utf-8") as f:
            dong = f.readlines()[-toi_da:]
    except OSError:
        return []
    ra = []
    for d in dong:
        try:
            ra.append(json.loads(d))
        except ValueError:
            continue
    return ra


# ── Việc chạy nền ────────────────────────────────────────────────────────────

def bat_dau(xuong: str, loai: str, lam: Callable[[Dict], None], du_an: str = "") -> str:
    """Chạy `lam(viec)` ở luồng nền. `lam` tự gọi `buoc` để báo tiến độ và xem `viec["huy"]` giữa các bước."""
    ma = uuid.uuid4().hex[:12]
    viec = {"id": ma, "xuong": xuong, "kind": loai, "project": du_an, "status": "running", "progress": 0,
            "step": "Đang bắt đầu", "error": "", "result": None, "huy": False, "luc": time.time(), "ai": _ai()}
    with _KHOA:
        for cu in [k for k, v in _VIEC.items() if v["status"] != "running" and time.time() - v["luc"] > 3600]:
            _VIEC.pop(cu, None)
        _VIEC[ma] = viec

    def run():
        _LUONG.viec = viec
        try:
            lam(viec)
            if viec["huy"]:
                viec["status"], viec["step"] = "cancelled", "Đã dừng"
            elif viec["status"] == "running":
                viec["status"], viec["progress"] = "done", 100
        except Exception as exc:  # noqa: BLE001
            viec["status"], viec["error"] = "error", str(exc)[:400]
        ghi_nhat_ky(xuong, loai, du_an, viec["status"], time.time() - viec["luc"], viec["error"], viec.get("ai") or "")
    threading.Thread(target=run, daemon=True, name=f"{xuong}-{loai}-{ma}").start()
    return ma


def buoc(viec: Dict, phan_tram: Optional[int], chu: str = "") -> None:
    if phan_tram is not None:
        viec["progress"] = max(0, min(100, int(phan_tram)))
    if chu:
        viec["step"] = chu


def xem_viec(ma: str) -> Optional[Dict]:
    viec = _VIEC.get(ma)
    return {k: v for k, v in viec.items() if k not in _KHOA_AN} if viec else None


def huy_viec(ma: str) -> bool:
    viec = _VIEC.get(ma)
    if viec and viec["status"] == "running":
        viec["huy"] = True
        return True
    return False


def moi_viec_dang_chay() -> List[Dict]:
    return [{k: v for k, v in viec.items() if k not in _KHOA_AN} for viec in list(_VIEC.values()) if viec["status"] == "running"]


def giu_du_an(ma_du_an: str) -> None:
    """
    Gọi từ trong việc nền: đánh dấu việc này đang giữ video `ma_du_an` (đã đọc vào bộ nhớ và sẽ ghi lại). Gọi từ luồng
    khác (yêu cầu của trang) thì không làm gì. Dấu hết khi việc nền xong, vì chỉ việc đang chạy mới bị xét.
    """
    viec = getattr(_LUONG, "viec", None)
    if viec is not None and ma_du_an and ma_du_an not in viec.setdefault("giu", []):
        viec["giu"].append(ma_du_an)


def du_an_dang_lam(ma_du_an: str) -> bool:
    """Có việc nền nào đang chạy trên đúng video này không (việc của chính video đó, hoặc mẻ / lịch đang giữ nó)."""
    if not ma_du_an:
        return False
    return any(v["status"] == "running" and (v.get("project") == ma_du_an or ma_du_an in (v.get("giu") or []))
               for v in list(_VIEC.values()))


def viec_dang_chay(xuong: str) -> str:
    """Mã việc đang chạy của một xưởng, rỗng nếu rảnh. File khởi động lại an toàn hỏi chỗ này."""
    for ma, viec in list(_VIEC.items()):
        if viec["xuong"] == xuong and viec["status"] == "running":
            return ma
    return ""


# ── AI viết chữ ──────────────────────────────────────────────────────────────

def cai_dat_ai() -> Dict:
    from gemini_translator import load_gemini_config
    cfg = load_gemini_config() or {}
    la_openai = cfg.get("ai_provider") == "openai"
    return {"base_url": cfg.get("openai_base_url") or "http://localhost:20128/v1",
            "model": cfg.get("openai_model") or "combo1",
            "api_key": cfg.get("openai_api_key") or "9router-key",
            "gemini_api_key": "" if la_openai else (cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")),
            "gemini_model": cfg.get("model") or "gemini-2.5-flash"}


def hoi_ai(he: str, de: str, max_tokens: int = 3000) -> Dict:
    """Hỏi AI lấy JSON. Lỗi thì ném RuntimeError kèm lý do đọc được."""
    import longform_studio as lf
    got = lf.ask_json(he, de, cai_dat_ai(), max_tokens=max_tokens)
    if not got.get("success"):
        ly_do = got.get("error") or "; ".join(got.get("attempts") or []) or "không rõ"
        raise RuntimeError("AI chưa viết được: " + str(ly_do)[:260])
    return got["data"]


def so_tieng(chu: str) -> int:
    return len([t for t in (chu or "").split() if any(c.isalnum() for c in t)])


# ── Giọng đọc (chỉ giọng nhân bản) ───────────────────────────────────────────

def ds_giong() -> Dict:
    import voice_clone
    return {"installed": voice_clone.engine_installed(),
            "voices": [{"id": g.get("id"), "name": g.get("name")} for g in voice_clone.list_voices()]}


def toc_do(ma_giong: str) -> float:
    """Số tiếng mỗi giây giọng này đọc được (đã đo), chưa đo thì TIENG_MOI_GIAY."""
    try:
        v = float((doc_json(TOC_DO_GIONG) or {}).get(ma_giong or "") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if 1.5 <= v <= 7 else TIENG_MOI_GIAY


def _ghi_toc_do(ma_giong: str, tieng: int, giay: float) -> None:
    if not ma_giong or tieng < 30 or giay < 4:
        return  # đoạn quá ngắn đo không chuẩn
    moi = tieng / giay
    bang = doc_json(TOC_DO_GIONG) or {}
    cu = float(bang.get(ma_giong) or 0)
    bang[ma_giong] = round(0.6 * cu + 0.4 * moi if cu else moi, 3)
    ghi_json(TOC_DO_GIONG, bang)


# Giọng Edge (miễn phí, cần mạng), mã "edge:<tên giọng>". 14/11/2026: Kho giọng chỉ có giọng nam (đo cao độ ~110 Hz) và file
# mẫu của "Mơ" gần như im lặng, người dùng muốn giọng nữ cho video Shopee. Chỉ nhận giọng tiếng Việt có trong GIONG_EDGE.
GIONG_EDGE = [{"id": "edge:vi-VN-HoaiMyNeural", "name": "Giọng nữ HoaiMy (Edge)"},
              {"id": "edge:vi-VN-NamMinhNeural", "name": "Giọng nam NamMinh (Edge)"},
              {"id": "mac:Linh", "name": "Giọng nữ Linh (máy Mac, không cần mạng)"}]


def _doc_mac(ten: str, chu: str, dich: str) -> None:
    """Giọng tiếng Việt có sẵn của macOS (`say`), chạy không cần mạng. Dự phòng khi máy chủ Edge chặn (14/11/2026)."""
    if f"mac:{ten}" not in {g["id"] for g in GIONG_EDGE}:
        raise RuntimeError("Giọng máy Mac này không có trong danh sách.")
    tam = dich + ".aiff"
    r = subprocess.run(["say", "-v", ten, "-o", tam, chu], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if r.returncode != 0 or not os.path.isfile(tam):
        raise RuntimeError("Giọng máy Mac không đọc được: " + r.stderr.decode("utf-8", "ignore")[:200])
    chay([ffmpeg(), "-y", "-i", tam, "-ar", "44100", "-ac", "1", dich])
    os.remove(tam)


def _doc_edge(ten: str, chu: str, dich: str) -> None:
    import asyncio
    import edge_tts
    if f"edge:{ten}" not in {g["id"] for g in GIONG_EDGE}:
        raise RuntimeError("Giọng Edge này không có trong danh sách.")
    # edge-tts 7.2.8 bị Microsoft trả 403 (14/11/2026), cần >= 7.3.1. Bản mới hay báo NoAudioReceived NGẪU NHIÊN (đo cùng ngày:
    # cùng câu "xin chào các bạn" lúc được lúc không, cả bài 400 ký tự hỏng 3 lần liền). Nên đọc TỪNG CÂU, câu nào hỏng thì chờ
    # tăng dần rồi đọc lại (tối đa 6 lần), xong ghép các câu (ffmpeg concat). Hỏng hẳn một câu thì báo lỗi, không bỏ câu.
    cau = [c.strip() for c in re.split(r"(?<=[.!?…])\s+", chu or "") if c.strip()] or [chu]
    thu_muc = dich + ".cau"
    os.makedirs(thu_muc, exist_ok=True)
    try:
        tep = []
        for i, c in enumerate(cau):
            ra = os.path.join(thu_muc, f"{i:03d}.mp3")
            for lan in range(3):
                try:
                    asyncio.run(edge_tts.Communicate(c, ten).save(ra))
                    if os.path.getsize(ra) > 0:
                        break
                except Exception as e:  # noqa: BLE001
                    if lan == 2:
                        raise RuntimeError(f"Giọng Edge không đọc được câu {i + 1} (cần mạng, máy chủ Microsoft chập chờn): {e}") from e
                time.sleep(2 + 3 * lan)
            tep.append(ra)
        ds = os.path.join(thu_muc, "ds.txt")
        with open(ds, "w", encoding="utf-8") as f:
            f.write("\n".join(f"file '{t}'" for t in tep))
        chay([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", ds, "-ar", "44100", "-ac", "1", dich])
    finally:
        shutil.rmtree(thu_muc, ignore_errors=True)


def doc_giong(ma_giong: str, chu: str, dich: str, toi_da: float = 0.0) -> Dict:
    """
    Đọc `chu` bằng giọng nhân bản ra file wav `dich`. `toi_da` > 0 thì lời dài hơn sẽ được ép nhanh cho vừa,
    tới mức EP_NHANH_TOI_DA; vẫn dài hơn nữa thì giữ nguyên và báo lại để bên gọi rút lời.
    """
    tho = dich + ".tho.wav"
    du_phong = ""
    if (ma_giong or "").startswith("mac:"):
        _doc_mac(ma_giong[4:], chu, tho)
    elif (ma_giong or "").startswith("edge:"):
        try:
            _doc_edge(ma_giong[5:], chu, tho)
        except RuntimeError as e:
            # Đọc lại CẢ BÀI bằng giọng Linh của máy Mac (không ghép hai giọng trong một video), ghi lại để báo người dùng.
            if "HoaiMy" not in ma_giong:
                raise
            _doc_mac("Linh", chu, tho)
            du_phong = f"Máy chủ Edge chặn ({str(e)[:120]}), đã đọc bằng giọng Linh của máy Mac."
    else:
        import voice_clone
        if not voice_clone.engine_installed():
            raise RuntimeError("Máy này chưa cài bộ nhân bản giọng. Vào Kho giọng để cài, rồi nhân bản một giọng.")
        if not voice_clone.get_voice(ma_giong):
            raise RuntimeError("Chưa chọn giọng đọc, hoặc giọng đã bị xoá khỏi Kho giọng.")
        voice_clone.speak_to_file(ma_giong, chu, tho)
    dai = so_giay(tho)
    _ghi_toc_do("mac:Linh" if du_phong else ma_giong, so_tieng(chu), dai)
    ep = 1.0
    if toi_da and dai > toi_da:
        ep = min(EP_NHANH_TOI_DA, dai / toi_da + 0.005)
    loc = f"atempo={ep:.4f}," if ep > 1.001 else ""
    chay([ffmpeg(), "-y", "-i", tho, "-af", loc + "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "2", dich])
    os.remove(tho)
    dai = so_giay(dich)
    return {"seconds": round(dai, 2), "speed": round(ep, 3), "over": bool(toi_da and dai > toi_da + 0.3),
            **({"du_phong": du_phong} if du_phong else {})}


def chia_phu_de(chu: str, tong_giay: float, toi_da_tieng: int = 7) -> List[Dict]:
    """Chia lời thành cụm ngắn, giờ hiện chia theo số chữ. Không dò tiếng thật nên lệch được vài phần mười giây."""
    import re
    cum: List[str] = []
    for cau in re.split(r"(?<=[.!?…])\s+|\n+", (chu or "").strip()):
        tu = cau.split()
        while tu:
            lay = toi_da_tieng if len(tu) > toi_da_tieng + 2 else len(tu)
            cum.append(" ".join(tu[:lay]))
            tu = tu[lay:]
    tong = sum(len(c) for c in cum) or 1
    ra, t = [], 0.0
    for c in cum:
        dai = tong_giay * len(c) / tong
        ra.append({"text": c, "start": round(t, 3), "end": round(t + dai, 3)})
        t += dai
    return ra
