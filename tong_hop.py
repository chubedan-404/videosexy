"""
tong_hop.py
───────────
Ghép nhiều video đã dựng thành một video tổng hợp dài.

Vì sao đáng làm: video 29 phút giữ chân 10 phần trăm cho ra 3 phút xem, video
160 phút giữ chân 10 phần trăm cho ra 16 phút xem. Cùng một nội dung đã tốn
công làm, mà thời gian xem gấp năm lần. Định dạng này nhắm vào người mở lên để
nghe nền khi làm việc hoặc trước khi ngủ.

Vì sao rẻ: KHÔNG sinh gì mới. Đo ngày 20/09/2026, một video 120 phút nếu dựng
từ đầu cần 400 tranh, vẽ mất khoảng năm giờ. Còn ghép hai video đã có thành
một video 47 phút mất 3,1 giây, vì mọi video ra từ cùng một tool nên cùng khổ,
cùng mã hình và mã tiếng, ghép được bằng cách chép thẳng luồng chứ không mã
hoá lại.

Đúng chỗ này mới là lúc mốc chương có tác dụng: YouTube chỉ nhận mốc khi có từ
ba chương trở lên, mà một video đơn của tool thường chỉ đủ hai ba chương.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")

# Chỉ nhận file do chính tool dựng ra. Tên file tự do là mở đường cho một máy
# khác cùng WiFi trỏ vào file bất kỳ trên ổ đĩa.
TEN_HOP_LE = re.compile(r"^longform_[A-Za-z0-9_]+\.mp4$")

# Dưới mức này thì YouTube không nhận danh sách chương.
CHUONG_TOI_THIEU = 3


def _ffmpeg() -> str:
    local = os.path.join(BASE_DIR, "ffmpeg")
    return local if os.path.isfile(local) else (shutil.which("ffmpeg") or "ffmpeg")


def _ffprobe_dong(path: str) -> Dict:
    """Khổ hình, mã hình, mã tiếng và độ dài của một file."""
    run = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostdin", "-i", path],
        capture_output=True, text=True, timeout=120)
    log = (run.stderr or "")
    ra: Dict = {"seconds": 0.0, "v": "", "a": "", "khổ": ""}
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", log)
    if m:
        ra["seconds"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video: (\w+).*?, (\d+x\d+)", log)
    if m:
        ra["v"], ra["khổ"] = m.group(1), m.group(2)
    m = re.search(r"Audio: (\w+)", log)
    if m:
        ra["a"] = m.group(1)
    return ra


# Tên chương đầu hay là một chữ chung chung, đặt vào mốc chương thì người xem
# đọc xong vẫn không biết phần đó kể về ai.
CHUNG_CHUNG = ("mở đầu", "mở bài", "giới thiệu", "dẫn nhập", "phần một", "mở màn")


# Cảnh chào đứng đầu mọi video và câu chữ giống hệt nhau, nên nó là câu đầu
# tiên trong mọi file phụ đề và không nhận ra được video nào.
LOI_CHAO = ("xin chào", "kính chào", "chào quý vị", "chào mừng")


def _cau_dau_phu_de(stem: str) -> str:
    """
    Câu kể đầu tiên trong file phụ đề, bỏ qua lời chào kênh.

    Từ 20/09 luật mở bài bắt nêu tên nhân vật ngay câu đầu của phần kể, nên
    câu ấy gần như luôn chứa tên. Đo trên kho video của kênh: lấy thẳng câu
    đầu file thì cả bốn video đều ra "Thanh Sử Quán xin chào quý vị khán giả",
    tức không phân biệt được cái nào với cái nào.
    """
    duong = os.path.join(DOWNLOADS_DIR, f"{stem}.srt")
    try:
        with open(duong, encoding="utf-8") as fh:
            dong = [d.strip() for d in fh.read().splitlines()]
    except OSError:
        return ""
    gom = []
    for i, d in enumerate(dong):
        # Khối srt là: số thứ tự, mốc thời gian, rồi mới tới chữ.
        if "-->" in d and i + 1 < len(dong) and dong[i + 1]:
            gom.append(dong[i + 1])
        if len(gom) >= 6:
            break
    for cau in gom:
        if not any(c in cau.lower() for c in LOI_CHAO):
            return cau[:80]
    return gom[0][:80] if gom else ""


def _goi_y_ten(stem: str) -> str:
    """
    Tên gợi ý cho một phần, để người dùng nhận ra video nào là video nào.

    Dự án không tra ra tên được: người dùng hay dùng lại một dự án cho nhiều
    video, nên tám video cùng trỏ về một cái tên.

    Lấy tên chương đầu trước. Nhưng đo 20/09 trên chính kho video của kênh: ba
    trên bốn video có chương đầu tên "Mở đầu", tức không nhận ra được gì. Gặp
    những chữ chung chung ấy thì lùi sang câu đầu của phụ đề, câu đó theo luật
    mới luôn có tên nhân vật.
    """
    duong = os.path.join(DOWNLOADS_DIR, f"{stem}_chuong.txt")
    ten = ""
    try:
        with open(duong, encoding="utf-8") as fh:
            ten = re.sub(r"^\d+:\d+\s*", "", fh.readline().strip())[:80]
    except OSError:
        ten = ""
    if ten and ten.strip().lower() not in CHUNG_CHUNG:
        return ten
    return _cau_dau_phu_de(stem) or ten


def dinh_dang_giay(giay: float) -> str:
    giay = int(round(giay))
    gio, du = divmod(giay, 3600)
    phut, giay = divmod(du, 60)
    return f"{gio}:{phut:02d}:{giay:02d}" if gio else f"{phut}:{giay:02d}"


def danh_sach(toi_da: int = 40) -> List[Dict]:
    """Các video dài đã dựng xong, mới nhất trước."""
    try:
        ten = [n for n in os.listdir(DOWNLOADS_DIR) if TEN_HOP_LE.match(n)]
    except OSError:
        return []
    ten.sort(key=lambda n: os.path.getmtime(os.path.join(DOWNLOADS_DIR, n)), reverse=True)
    ra = []
    for n in ten[:toi_da]:
        duong = os.path.join(DOWNLOADS_DIR, n)
        if os.path.getsize(duong) < 100_000:
            continue
        tin = _ffprobe_dong(duong)
        if tin["seconds"] <= 0:
            continue
        stem = os.path.splitext(n)[0]
        ra.append({
            "file": n,
            "url": f"/downloads/{n}",
            "seconds": round(tin["seconds"], 1),
            "dai": dinh_dang_giay(tin["seconds"]),
            "mb": round(os.path.getsize(duong) / 1048576),
            "khổ": tin["khổ"], "v": tin["v"], "a": tin["a"],
            "goi_y": _goi_y_ten(stem),
        })
    return ra


def _hop_khuon(tin: List[Dict]) -> Optional[str]:
    """Các file có cùng khuôn để chép thẳng luồng không. Không thì nói rõ vì sao."""
    dau = tin[0]
    for t in tin[1:]:
        for khoa, nhan in (("khổ", "khổ hình"), ("v", "mã hình"), ("a", "mã tiếng")):
            if t[khoa] != dau[khoa]:
                return (f"{nhan} khác nhau: {dau['file']} là {dau[khoa]}, "
                        f"{t['file']} là {t[khoa]}. Hai video ra từ hai bản tool khác nhau "
                        "thì phải dựng lại một cái cho cùng khuôn.")
    return None


def moc_chuong(tin: List[Dict], ten_phan: List[str]) -> str:
    """
    Mốc chương dán vào mô tả YouTube, mỗi video thành một chương.

    YouTube đòi mốc đầu là 0:00 và từ ba chương trở lên, mỗi chương ít nhất
    mười giây. Video dài của tool luôn vượt xa mức mười giây nên chỉ cần lo số
    chương.
    """
    if len(tin) < CHUONG_TOI_THIEU:
        return ""
    dong, moc = [], 0.0
    for i, t in enumerate(tin):
        ten = (ten_phan[i] if i < len(ten_phan) else "").strip() or t.get("goi_y") or f"Phần {i + 1}"
        dong.append(f"{dinh_dang_giay(moc)} {ten}")
        moc += t["seconds"]
    return "\n".join(dong)


def ghep(files: List[str], ten_phan: Optional[List[str]] = None,
         output: str = "", tien_trinh=None) -> Dict:
    """
    Nối các video lại thành một, chép thẳng luồng nên không mã hoá lại.

    Không mã hoá lại là điểm mấu chốt: một video hai tiếng rưỡi mã hoá lại mất
    hàng chục phút và mất chất lượng, còn chép luồng mất vài giây và giữ nguyên
    từng khung hình.
    """
    if len(files) < 2:
        return {"success": False, "error": "Cần ít nhất hai video để ghép."}

    duong: List[str] = []
    for n in files:
        n = os.path.basename((n or "").strip())
        if not TEN_HOP_LE.match(n):
            return {"success": False, "error": f"Tên file không hợp lệ: {n}"}
        d = os.path.join(DOWNLOADS_DIR, n)
        if not os.path.isfile(d):
            return {"success": False, "error": f"Không tìm thấy {n}"}
        duong.append(d)

    if tien_trinh:
        tien_trinh("Đang xem các video có cùng khuôn không")
    tin = []
    for d in duong:
        t = _ffprobe_dong(d)
        t["file"] = os.path.basename(d)
        tin.append(t)
    loi = _hop_khuon(tin)
    if loi:
        return {"success": False, "error": loi}

    tong = sum(t["seconds"] for t in tin)
    output = os.path.basename(output or f"tonghop_{int(tong)}s.mp4")
    if not output.endswith(".mp4"):
        output += ".mp4"
    ra = os.path.join(DOWNLOADS_DIR, output)

    with tempfile.TemporaryDirectory(prefix="tonghop_") as thu_muc:
        ds = os.path.join(thu_muc, "list.txt")
        with open(ds, "w", encoding="utf-8") as fh:
            for d in duong:
                # Dấu nháy đơn trong tên file sẽ phá cú pháp của bộ nối.
                fh.write("file '%s'\n" % d.replace("'", "'\\''"))
        if tien_trinh:
            tien_trinh(f"Đang nối {len(duong)} video, tổng {dinh_dang_giay(tong)}")
        run = subprocess.run(
            [_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
             "-i", ds, "-c", "copy", "-movflags", "+faststart", ra],
            capture_output=True, text=True, timeout=3600)
    if run.returncode or not os.path.isfile(ra):
        return {"success": False, "error": f"Nối hỏng: {(run.stderr or '')[-300:]}"}

    chuong = moc_chuong(tin, ten_phan or [])
    ten_chuong = ""
    if chuong:
        ten_chuong = os.path.splitext(output)[0] + "_chuong.txt"
        with open(os.path.join(DOWNLOADS_DIR, ten_chuong), "w", encoding="utf-8") as fh:
            fh.write(chuong)

    return {"success": True, "output": output,
            "download_url": f"/downloads/{output}",
            "seconds": round(tong, 1), "dai": dinh_dang_giay(tong),
            "so_phan": len(duong),
            "chuong": chuong,
            "chuong_url": f"/downloads/{ten_chuong}" if ten_chuong else "",
            "mb": round(os.path.getsize(ra) / 1048576),
            "canh_bao": ("" if chuong else
                         f"Dưới {CHUONG_TOI_THIEU} phần nên YouTube không nhận mốc chương. "
                         "Ghép thêm video nữa thì có.")}
