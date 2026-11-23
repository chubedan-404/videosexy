"""
soat_clip.py
────────────
AI soát từng clip máy làm clip trả về, TRƯỚC khi dựng video (thêm ngày 05/11/2026).

Vì sao cần. Máy làm clip (Flow, Muse) hay vẽ sai theo những kiểu người xem nhận ra ngay: hộp biến thành tuýp, tuýp tự hiện ra
giữa clip, gel chảy ra từ giữa thân tuýp, các gói bên trong hộp mỗi clip một kiểu. Trước đây chỉ người dùng soi bằng mắt mới
thấy. Nay quay xong mỗi clip, tool cắt vài khung hình, gửi AI cùng ảnh thật của sản phẩm và đoạn tả bao bì, hỏi có lỗi rõ
không. Clip lỗi thì `review_sp.quay` quay lại (tối đa `review_sp.SO_LAN_QUAY_LAI` lần), kèm lời dặn tránh đúng lỗi đó.

AI không nhìn được thì KHÔNG chặn video: trả `dat=None` (chưa soát) để clip vẫn được dùng, và ghi lý do.
"""
from __future__ import annotations

import io
import os
import subprocess
from typing import Dict, List, Optional

import xuong_chung as pc

SO_KHUNG = 10  # 06/11/2026: 6 khung bỏ lọt một bàn tay thừa trồi lên vài giây

_HE = (
    "Bạn là người soát chất lượng clip quảng cáo sản phẩm do AI tạo, quay bàn tay cầm sản phẩm. Bạn nhận: ảnh THẬT của sản "
    "phẩm (các ảnh đầu), rồi các khung hình cắt đều từ một clip AI (theo thứ tự thời gian). Chỉ báo lỗi RÕ RÀNG mà người xem "
    "bình thường nhận ra. Chỉ trả về JSON."
)

_DE = """Sản phẩm thật (tả bằng tiếng Anh): {ta}

Clip này định quay: {canh}

Có {so_anh} ảnh thật đầu tiên, sau đó là {so_khung} khung hình của clip theo thứ tự thời gian.

Soát đúng những lỗi sau (lỗi nào có thì ghi):
1. Sai sản phẩm: màu, hình dáng, logo hay tên lớn trên bao bì khác hẳn ảnh thật; hoặc là một sản phẩm khác.
2. Vật biến hình hay tự xuất hiện: một vật đổi thành vật khác giữa clip (hộp thành tuýp...), vật tự hiện ra hoặc biến mất mà
   không có tay đưa vào, lấy ra; vật bị nhân đôi; có thêm chai, hộp không có trong ảnh thật.
3. Chất bên trong (gel, bọt) ra sai chỗ: rỉ ra từ thân hay bên hông tuýp, chai thay vì từ miệng ở đầu nắp; hoặc xuất hiện
   trên tay, trên bao bì mà không thấy nặn, bơm ra.
4. Thứ bên trong hộp sai so với phần tả: sai hình dạng, sai cách xếp, số lượng khác hẳn, hay đổi kiểu giữa các khung.
5. Có mặt người, đầu người, hoặc chữ, phụ đề, logo chèn đè lên hình.
6. Bàn tay sai: thêm một bàn tay hay cánh tay thứ ba (nhiều tay hơn một người có), tay tự trồi lên hay biến mất giữa khung,
   số tay đổi giữa các khung mà không thấy tay đưa vào; ngón tay dính nhau, thừa ngón, tay méo.
7. Nhãn tự đổi mặt: mặt in trên bao bì đổi từ mặt này sang mặt khác (mặt trước thành mặt sau, chữ dọc thành chữ ngang) mà
   không thấy tay xoay sản phẩm hẳn một vòng.
8. Tên sản phẩm in TO trên bao bì (chữ lớn nhất, ví dụ tên in dọc thân tuýp) bị sai chữ, thiếu chữ, chữ lạ so với ảnh thật (ví
   dụ "PLEA GRE GEL" thay vì "PLEASURE GEL"). Đọc từng chữ cái của tên lớn ở khung rõ nhất rồi so với ảnh thật. Cũng là lỗi
   khi chữ in trên bao bì khác hẳn kiểu của ảnh thật: ảnh thật chữ nhỏ mà clip vẽ chữ to chạy hết thân, thêm chữ không có
   trên ảnh thật (ví dụ thêm "GEL", "100 ml" to ở mặt trước). KHÔNG tính: chữ nằm ngang thay vì dọc, biểu tượng nhỏ hơi khác,
   miễn tên viết đúng và cỡ chữ gần như ảnh thật.
9. Làm khác câu lệnh một cách rõ ràng: thứ dùng cùng sản phẩm khác loại với câu lệnh tả (câu lệnh nói đùi gà mà clip là cánh gà,
   số lượng khác hẳn như gấp đôi hay chỉ còn một nửa; lệch một hai cái KHÔNG tính); thao tác NGƯỢC chiều câu lệnh dặn (câu lệnh dặn vặn núm sang phải / clockwise mà vạch chỉ trên núm đi
   ngược chiều kim đồng hồ: so vị trí vạch chỉ giữa các khung liền nhau). Chỉ báo khi thấy rõ.

KHÔNG tính là lỗi: dấu ngôi sao hay lấp lánh nhỏ cố định ở góc dưới bên phải (dấu mờ của máy làm clip), chữ NHỎ trên nhãn bị nhoè hay sai chính tả nhẹ (máy AI hay vậy; tên lớn thì vẫn là lỗi, mục 8), ánh sáng, góc máy, mặt bàn, bối cảnh, tay
khác ảnh thật, sản phẩm bị cắt mép khung một chút.

Trả về JSON: {{"dat": true hoặc false, "loi": ["mỗi lỗi một câu tiếng Việt ngắn, nêu khung nào"], "khung_loi": [số thứ tự (đếm từ
1) của MỌI khung clip đang thấy lỗi; lỗi có suốt clip như sai sản phẩm thì ghi đủ mọi khung; đạt thì để trống], "sua": "một câu
TIẾNG ANH dặn máy làm clip tránh đúng các lỗi đó ở lần quay lại, để trống nếu đạt"}}"""


def _gio_khung(dai: float, so: int) -> List[float]:
    """Giây của từng khung `_ffmpeg_khung` cắt (đều nhau, bỏ sát đầu và sát cuối)."""
    return [dai * (i + 0.5) / so for i in range(so)]


def doan_sach(dai: float, giay_loi: List, le: float = 0.3, toi_thieu: float = 1.2) -> List[List[float]]:
    """
    Các đoạn không dính lỗi của một clip dài `dai` giây, biết các khoảng lỗi `giay_loi` [(từ, đến)]. Mỗi khoảng lỗi nới thêm
    `le` giây hai bên; đoạn ngắn hơn `toi_thieu` bị bỏ. Thêm 07/11/2026: trước đây chọn đoạn sạch cho kiểu dựng cắt nhanh phải
    gõ tay từng clip.
    """
    ban = sorted((max(0.0, float(a) - le), min(dai, float(b) + le)) for a, b in giay_loi)
    ra, t = [], 0.3
    for a, b in ban:
        if a - t >= toi_thieu:
            ra.append([round(t, 2), round(a, 2)])
        t = max(t, b)
    if dai - 0.3 - t >= toi_thieu:
        ra.append([round(t, 2), round(dai - 0.3, 2)])
    return ra


def _ffmpeg_khung(duong: str, so: int = SO_KHUNG, rong: int = 448) -> List[bytes]:
    """`so` khung JPEG cắt đều trong clip (bỏ sát đầu và sát cuối)."""
    dai = pc.so_giay(duong) or 5.0
    ra = []
    for i in range(so):
        t = dai * (i + 0.5) / so
        got = subprocess.run([pc.ffmpeg(), "-v", "error", "-ss", f"{t:.2f}", "-i", duong, "-frames:v", "1",
                              "-vf", f"scale={rong}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"],
                             capture_output=True, timeout=60)
        if got.returncode == 0 and got.stdout:
            ra.append(got.stdout)
    return ra


def _anh_jpeg(duong: str, canh_dai: int = 640) -> Optional[bytes]:
    try:
        from PIL import Image
        with Image.open(duong) as im:
            im = im.convert("RGB")
            im.thumbnail((canh_dai, canh_dai))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=85)
            return b.getvalue()
    except Exception:  # noqa: BLE001
        return None


_DEM_TAY = """Có {so} khung hình cắt từ một clip, theo thứ tự thời gian (2 khung mỗi giây). Với TỪNG khung, đếm số bàn tay người
nhìn thấy được, tính cả bàn tay chỉ lộ vài ngón hay một phần ở mép khung, ở góc, phía sau sản phẩm. Đếm kỹ từng khung, đừng đoán
theo khung bên cạnh. Trả về JSON: {{"tay": [số tay khung 1, số tay khung 2, ...]}} đúng {so} số."""


def dem_tay(clip: str) -> Optional[List[int]]:
    """
    Đếm số bàn tay trong từng khung, 2 khung mỗi giây. Thêm 06/11/2026: một bàn tay thứ ba trồi lên chừng nửa giây mà bước soát
    chung (nhìn 10 khung, hỏi nhiều lỗi một lượt) cho qua. Hỏi riêng một việc đếm thì AI nhìn kỹ hơn. None là chưa đếm được.
    """
    import soi_video
    so = max(6, min(24, int(round((pc.so_giay(clip) or 8.0) * 2))))
    khung = _ffmpeg_khung(clip, so, 384)
    if len(khung) < 3:
        return None
    try:
        got = soi_video.hoi_ai_anh("Bạn đếm bàn tay trong ảnh, cẩn thận và chính xác. Chỉ trả về JSON.",
                                   _DEM_TAY.format(so=len(khung)), khung, max_tokens=600)
        tay = [int(x) for x in got.get("tay") or []]
    except Exception:  # noqa: BLE001
        return None
    return tay if len(tay) == len(khung) else None


def soat(clip: str, anh_that: List[str], ta: str, canh: str) -> Dict:
    """
    Soát một clip. `anh_that`: đường dẫn ảnh thật (ảnh đúng cảnh trước, tối đa 2). `ta`: đoạn tả sản phẩm tiếng Anh.
    Trả {"dat": True/False/None, "loi": [...], "sua": "..."}; None là chưa soát được (AI lỗi, thiếu khung).
    """
    import soi_video
    that = [a for a in (_anh_jpeg(x) for x in anh_that[:2]) if a]
    khung = _ffmpeg_khung(clip)
    if not that or len(khung) < 3:
        return {"dat": None, "loi": ["Không cắt được khung hình hoặc thiếu ảnh thật để so."], "sua": ""}
    try:
        got = soi_video.hoi_ai_anh(_HE, _DE.format(ta=ta or "(không có phần tả)", canh=(canh or "")[:900],
                                                   so_anh=len(that), so_khung=len(khung)), that + khung, max_tokens=1500)
    except Exception as exc:  # noqa: BLE001 - AI không nhìn được thì không chặn video
        return {"dat": None, "loi": [f"AI chưa soát được: {str(exc)[:160]}"], "sua": ""}
    dat = got.get("dat")
    loi = [str(x).strip()[:240] for x in (got.get("loi") or []) if str(x).strip()][:5]
    dai = pc.so_giay(clip) or 5.0
    nua = dai / len(khung) / 2
    gio = _gio_khung(dai, len(khung))
    # Giây dính lỗi: quanh mỗi khung AI chỉ ra. AI báo lỗi mà không chỉ khung thì coi như lỗi suốt clip.
    so_loi = [int(x) for x in (got.get("khung_loi") or []) if str(x).strip().lstrip("-").isdigit() and 1 <= int(x) <= len(khung)]
    giay_loi = [(gio[k - 1] - nua, gio[k - 1] + nua) for k in so_loi] if (dat is not False or so_loi) else [(0.0, dai)]
    tay = dem_tay(clip)
    if tay and max(tay) > 2 and isinstance(dat, bool):
        gio_tay = _gio_khung(dai, len(tay))
        nua_tay = dai / len(tay) / 2
        giay_loi += [(gio_tay[k] - nua_tay, gio_tay[k] + nua_tay) for k, n in enumerate(tay) if n > 2]
        giay = [f"{gio_tay[k]:.1f}s" for k, n in enumerate(tay) if n > 2]
        loi = ([] if dat else loi) + [f"Có hơn hai bàn tay (thêm một bàn tay trồi lên) ở {', '.join(giay[:4])}."]
        got["sua"] = ((got.get("sua") or "") if not dat else "") + (" Exactly two hands of one person in the whole shot; no third hand"
                                                                   " or arm ever appears, hands never pop in from the frame edge.")
        dat = False
    if tay and max(tay) > 0 and isinstance(dat, bool) and "PRODUCT ONLY" in (canh or ""):
        # Cảnh chỉ quay sản phẩm (07/11/2026): có bất kỳ bàn tay nào là lỗi.
        gio_tay = _gio_khung(dai, len(tay))
        nua_tay = dai / len(tay) / 2
        giay_loi += [(gio_tay[k] - nua_tay, gio_tay[k] + nua_tay) for k, n in enumerate(tay) if n > 0]
        loi = ([] if dat else loi) + ["Cảnh chỉ quay sản phẩm mà có bàn tay hiện ra."]
        got["sua"] = ((got.get("sua") or "") if not dat else "") + " No hands or people at all; the product stands alone."
        dat = False
    if not isinstance(dat, bool):
        return {"dat": None, "loi": ["AI trả lời không rõ đạt hay không."], "sua": ""}
    if dat:
        loi = []
    elif not loi:
        loi = ["AI báo clip không đạt nhưng không nêu lỗi."]
    return {"dat": dat, "loi": loi, "sua": str(got.get("sua") or "").strip()[:500] if not dat else "",
            "dai": round(dai, 2), "giay_loi": [[round(max(0, a), 2), round(min(dai, b), 2)] for a, b in giay_loi] if not dat else [],
            "doan_sach": doan_sach(dai, giay_loi) if not dat else []}
