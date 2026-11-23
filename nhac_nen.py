"""
nhac_nen.py
───────────
Trộn nhạc nền vào video kể chuyện đã dựng xong.

Bản đầu ngày 19/09 bị gỡ vì nghe ồn. Dựng lại ngày 20/09 sau khi đo thêm bốn
kênh kể chuyện thuộc bốn thể loại: cả bốn đều có nhạc, ở mức 15 tới 20 dB dưới
giọng. Nghĩa là khái niệm đúng, chỉ có bản nhạc là sai.

Sai ở đâu: bản đầu dùng ba bản nhạc tự tổng hợp bằng ffmpeg từ sóng sin cộng
hoạ âm. Phổ tần đo ra đúng chuẩn, nhưng tai nghe thành tiếng ù trầm chứ không
ra nhạc. Lần này KHÔNG tự tổng hợp nữa: người dùng tự bỏ file nhạc thật vào
thư mục, tool chỉ lo phần trộn.

Nguồn nhạc: chỉ Thư viện âm thanh trong YouTube Studio là được chính YouTube
cam kết không bị Content ID nhận diện. Các kho ghi "miễn phí bản quyền" khác
vẫn bị nhận diện như thường.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Thư mục riêng, không dùng chung static/bgm với tab dịch video: bốn file ở đó
# là nhạc cho video ngắn, dồn hết năng lượng vào đúng dải giọng người.
NHAC_DIR = os.path.join(BASE_DIR, "nhac_ke_chuyen")

DUOI_NHAN = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")

# Thấp hơn giọng đọc bao nhiêu decibel.
#
# Đo 20/09/2026, tách giọng khỏi nhạc bằng Demucs trên hai phút giữa video:
#   Top Thú vị, khoa học, 2,17 triệu lượt   15,2 dB
#   MÊ KỲ ÁN, hình sự, 732 nghìn            16,3 dB
#   Nguyễn Ngọc Ngạn, truyện ma, 2,4 triệu  16,7 dB
#   Tâm Sự Cùng Sâm, chuyện đời, 80 nghìn   20,2 dB
#   Sử Lược, lịch sử, 696 nghìn             26,0 dB
#
# Lấy 18 làm mặc định: giữa mặt bằng chung của nhóm kể chuyện. Kênh lịch sử để
# nhỏ hơn hẳn, nên ai làm sử thì kéo lên phía 26.
MAC_DINH_DB = 18.0
MIN_DB, MAX_DB = 10.0, 40.0


def _ffmpeg() -> str:
    local = os.path.join(BASE_DIR, "ffmpeg")
    return local if os.path.isfile(local) else (shutil.which("ffmpeg") or "ffmpeg")


def danh_sach() -> List[Dict]:
    """Các bản nhạc người dùng đã bỏ vào thư mục, kèm độ dài."""
    try:
        ten = sorted(n for n in os.listdir(NHAC_DIR)
                     if os.path.splitext(n)[1].lower() in DUOI_NHAN)
    except OSError:
        return []
    ra = []
    for n in ten:
        duong = os.path.join(NHAC_DIR, n)
        if os.path.getsize(duong) < 10_000:
            continue
        ra.append({"id": n, "label": os.path.splitext(n)[0],
                   "giay": round(_do_dai(duong)),
                   "url": f"/api/nhac/{n}"})
    return ra


def _do_dai(duong: str) -> float:
    run = subprocess.run([_ffmpeg(), "-hide_banner", "-nostdin", "-i", duong],
                         capture_output=True, text=True, timeout=120)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", run.stderr or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def duong_dan_nhac(gia_tri: str) -> str:
    """
    Đổi lựa chọn thành đường dẫn thật.

    Chỉ nhận tên file nằm ngay trong thư mục nhạc. Để tên tự do là mở đường cho
    một máy khác cùng WiFi trỏ vào file bất kỳ trên ổ đĩa.
    """
    ten = os.path.basename((gia_tri or "").strip())
    if not ten or ten in (".", "..") or ten == "none":
        return ""
    if os.path.splitext(ten)[1].lower() not in DUOI_NHAN:
        return ""
    duong = os.path.join(NHAC_DIR, ten)
    return duong if os.path.isfile(duong) else ""


_I_LINE = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)


def do_do_to(duong: str) -> Optional[float]:
    """Độ to tổng của một file, tính bằng LUFS. Không đo được thì trả None."""
    run = subprocess.run([_ffmpeg(), "-hide_banner", "-nostdin", "-i", duong,
                          "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True, timeout=1800)
    got = _I_LINE.findall((run.stderr or "") + (run.stdout or ""))
    if not got:
        return None
    try:
        muc = float(got[-1])
    except ValueError:
        return None
    # Video câm hay đo hỏng thì ffmpeg trả một số rất thấp, đừng tin nó.
    return muc if muc > -70 else None


def ghep(video_path: str, chon: str, duoi_giong_db: float = MAC_DINH_DB,
         out_path: str = "", tien_trinh=None) -> Dict:
    """
    Trộn nhạc vào một video đã dựng xong.

    Đo độ to của giọng TRƯỚC rồi mới tính mức nhạc, thay vì cắm cứng một con số
    phần trăm: giọng Gemini và giọng Edge to khác nhau vài decibel, nên cùng
    một phần trăm cho ra hai kết quả khác hẳn.

    Hình không mã hoá lại, chỉ chạm vào luồng tiếng, nên video bốn mươi phút
    chỉ mất vài chục giây.
    """
    nhac = duong_dan_nhac(chon)
    if not nhac:
        return {"success": False, "error": "Chưa chọn được bản nhạc nào."}
    if not os.path.isfile(video_path):
        return {"success": False, "error": "Không tìm thấy video để ghép nhạc."}

    duoi = max(MIN_DB, min(MAX_DB, float(duoi_giong_db or MAC_DINH_DB)))
    if tien_trinh:
        tien_trinh("Đang đo độ to của giọng đọc")
    giong = do_do_to(video_path)
    if giong is None:
        return {"success": False,
                "error": "Video này không có tiếng nên không ghép nhạc được."}
    nhac_hien = do_do_to(nhac)
    if nhac_hien is None:
        return {"success": False, "error": "File nhạc không đọc được."}

    bu = (giong - duoi) - nhac_hien
    out_path = out_path or (os.path.splitext(video_path)[0] + "_nhac.mp4")

    if tien_trinh:
        tien_trinh(f"Đang trộn nhạc, thấp hơn giọng {duoi:.0f} decibel")
    loc = (
        # Lặp nhạc cho đủ độ dài video rồi cắt theo tiếng của video.
        f"[1:a]volume={bu:.2f}dB,afade=t=in:st=0:d=4[nhac];"
        # normalize=0: giữ nguyên mức vừa tính, không để amix tự hạ cả hai.
        "[0:a][nhac]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[ra]"
    )
    run = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-i", video_path,
         "-stream_loop", "-1", "-i", nhac,
         "-filter_complex", loc, "-map", "0:v", "-map", "[ra]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
         "-movflags", "+faststart", out_path],
        capture_output=True, text=True, timeout=3600)
    if run.returncode or not os.path.isfile(out_path):
        return {"success": False,
                "error": f"Trộn nhạc hỏng: {(run.stderr or '')[-300:]}"}

    return {"success": True, "output": out_path,
            "giong_lufs": round(giong, 1), "duoi_giong_db": duoi,
            "nhac": os.path.basename(nhac)}
