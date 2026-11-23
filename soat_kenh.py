"""
soat_kenh.py
────────────
Soát mọi video trên kênh, tìm những lỗi khai báo mà mắt thường không bắt được.

Vì sao cần. Ngày 20/09/2026, hai video của kênh Thanh Sử Quán đứng im ở đúng 0
lượt xem trong khi video liền trước được 50. Nhìn bằng mắt thì cả hai hoàn toàn
bình thường: công khai, xử lý xong, ảnh bìa đẹp, tiêu đề tiếng Việt. Lỗi nằm ở
một ô mà không ai nghĩ tới mở ra xem, ô Video language trong mục Language and
captions certification: một cái khai là tiếng Thái, một cái khai là tiếng Ả Rập.

YouTube dùng ô đó để quyết định đem video đi mời ai. Kể chuyện bằng tiếng Việt
mà khai tiếng Thái thì nó mời người Thái, những người bấm vào là thoát ngay, và
nó không mời người Việt. Đó là lý do con số tròn 0 chứ không phải 3 hay 5.

Đây đúng là loại lỗi phải để máy soát: nó không hiện ra ở đâu cả, không có cảnh
báo nào, và chỉ lộ ra sau nhiều ngày qua việc video không có ai xem.

Mọi ngưỡng trong file này đều lấy từ số đo thật của kênh, ghi ngay cạnh luật.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Đo trên sáu video đã đăng: ba tiêu đề dài 81, 83 và 93 ký tự. YouTube chỉ
# hiện khoảng 70 ký tự trong danh sách gợi ý, nên cái 93 ký tự bị cắt thành
# "...LƯƠNG SƠN LẠI CHẾT VÌ CH", mất trọn vế móc câu.
TIEU_DE_TOI_DA = 70

# Năm video làm đúng đều có 5 tới 6 thẻ rời. Dưới ba thẻ là bỏ trống một ô
# miễn phí.
THE_TOI_THIEU = 3

# Mô tả ngắn hơn mức này thì YouTube gần như không có gì để đọc hiểu video.
MO_TA_TOI_THIEU = 200

NANG = "nang"   # chặn video tới được người xem
NHE = "nhe"     # phí một cơ hội, không chặn

# Hashtag YouTube hay coi là spam. #viral từng nằm trong tiêu đề một video.
#
# KHÔNG có "shorts" trong danh sách này. Ngày 21/09/2026 nó từng nằm đây, và
# bảng soát báo "#shorts là loại YouTube hay coi là spam" cho đúng hai clip vừa
# đăng. Sai: #shorts là thẻ chuẩn để YouTube nhận ra video dọc, ai làm Shorts
# cũng dùng. Báo nhầm kiểu đó vài lần là người dùng bỏ luôn không đọc bảng nữa.
HASHTAG_XAU = {"viral", "fyp", "foryou", "trending", "viralvideo", "xuhuong"}


def _loi(muc: str, nang: str, chuyen_gi: str, cach_sua: str,
         sua_duoc: str = "") -> Dict:
    """`sua_duoc` là mã để tool tự sửa hộ; để trống nghĩa là phải tự vào Studio."""
    return {"muc": muc, "nang": nang, "chuyen_gi": chuyen_gi,
            "cach_sua": cach_sua, "sua_duoc": sua_duoc}


def ngon_ngu_kenh(videos: List[Dict], mac_dinh: str = "vi") -> str:
    """
    Ngôn ngữ mà kênh này ĐÁNG LẼ phải khai, lấy theo số đông.

    Không lấy cứng "vi" vì tool còn làm được kênh tiếng khác. Lấy theo số đông
    thì kênh nào cũng tự đúng, và một video khai lệch khỏi phần còn lại chính
    là dấu hiệu cần bắt.
    """
    dem: Dict[str, int] = {}
    for v in videos:
        ma = (v.get("ngon_ngu") or "").strip().lower()[:2]
        if ma:
            dem[ma] = dem.get(ma, 0) + 1
    if not dem:
        return mac_dinh
    return max(dem.items(), key=lambda x: x[1])[0]


def soat_mot(v: Dict, ngon_ngu_dung: str) -> List[Dict]:
    """Mọi lỗi tìm được trên một video."""
    ra: List[Dict] = []
    tieu_de = v.get("title") or ""
    ma_nn = (v.get("ngon_ngu") or "").strip().lower()

    # ── Nhãn ngôn ngữ ────────────────────────────────────────────────────────
    if not ma_nn:
        ra.append(_loi(
            "Chưa khai ngôn ngữ tiếng nói", NANG,
            "Ô Video language đang để trống.",
            "Studio, thẻ Chi tiết, bấm HIỆN THÊM, mục Language and captions "
            f"certification, chọn ngôn ngữ đúng.", "ngon_ngu"))
    elif ma_nn[:2] != ngon_ngu_dung:
        ra.append(_loi(
            "Khai sai ngôn ngữ tiếng nói", NANG,
            f"Đang khai là '{ma_nn}', trong khi cả kênh là '{ngon_ngu_dung}'. "
            "YouTube sẽ đem video đi mời người nói thứ tiếng đó, và không mời "
            "người nói tiếng của kênh.",
            "Studio, thẻ Chi tiết, bấm HIỆN THÊM, mục Language and captions "
            "certification, đổi ô Video language.", "ngon_ngu"))

    # ── Tiêu đề ──────────────────────────────────────────────────────────────
    if len(tieu_de) > TIEU_DE_TOI_DA:
        ra.append(_loi(
            "Tiêu đề quá dài", NHE,
            f"Dài {len(tieu_de)} ký tự. YouTube chỉ hiện khoảng "
            f"{TIEU_DE_TOI_DA} ký tự nên phần sau bị cắt: "
            f"…{tieu_de[TIEU_DE_TOI_DA:][:28]}",
            f"Rút xuống dưới {TIEU_DE_TOI_DA} ký tự, giữ vế móc câu ở trước."))
    xau = [t.lstrip("#").lower() for t in re.findall(r"#\w+", tieu_de)]
    co_spam = [t for t in xau if t in HASHTAG_XAU]
    if co_spam:
        ra.append(_loi(
            "Hashtag spam trong tiêu đề", NANG,
            "Tiêu đề có " + ", ".join("#" + t for t in co_spam)
            + ". Đây là loại YouTube hay coi là spam và có thể hạ hiển thị.",
            "Bỏ mấy thẻ đó đi. Muốn gắn hashtag thì để ở cuối phần mô tả."))
    # Hashtag thường thì không phải lỗi riêng. Nó chỉ đáng nói khi ăn mất chỗ
    # làm tiêu đề vượt quá mức hiện được, mà luật độ dài ở trên đã bắt rồi.
    con_lai = [t for t in xau if t not in HASHTAG_XAU]
    if con_lai and len(tieu_de) > TIEU_DE_TOI_DA:
        so_ky_tu = sum(len(t) + 2 for t in con_lai)
        ra.append(_loi(
            "Hashtag đang chiếm chỗ của tiêu đề", NHE,
            f"{len(con_lai)} hashtag trong tiêu đề chiếm khoảng {so_ky_tu} ký tự, "
            f"đúng phần đang bị cắt mất.",
            "Chuyển hashtag xuống cuối mô tả, tiêu đề sẽ vừa chỗ hiện."))

    # ── Ô thẻ ────────────────────────────────────────────────────────────────
    the = [t for t in (v.get("tags") or []) if str(t).strip()]
    dinh = [t for t in the if "#" in t]
    if dinh:
        ra.append(_loi(
            "Ô thẻ đang chứa hashtag", NANG,
            f"Có {len(dinh)} thẻ chứa dấu thăng, ví dụ \"{dinh[0][:60]}\". "
            "Dán cả cụm hashtag vào ô thẻ thì YouTube đọc ra một thẻ dài duy "
            "nhất, và không ai tìm bằng cụm đó cả.",
            "Xoá đi, gõ lại thành từng thẻ rời, viết thường, không dấu thăng, "
            "ngăn bằng dấu phẩy. Hashtag thì để ở cuối mô tả."))
    elif len(the) < THE_TOI_THIEU:
        ra.append(_loi(
            "Quá ít thẻ", NHE,
            f"Mới có {len(the)} thẻ." if the else "Chưa có thẻ nào.",
            f"Thêm cho đủ ít nhất {THE_TOI_THIEU} thẻ: tên nhân vật, thời kỳ, "
            "chủ đề, và công thức tiêu đề của kênh."))

    # Cố ý KHÔNG soát ảnh bìa tự làm. API không có trường nào nói video đã tải
    # ảnh bìa riêng chưa, mà đoán qua việc có maxresdefault hay không thì sai:
    # video quay ở độ phân giải cao nào cũng có sẵn tấm đó. Thà thiếu một luật
    # còn hơn có một luật báo đỏ nhầm, vì báo nhầm vài lần là người dùng bỏ
    # luôn không đọc bảng soát nữa.

    # ── Mấy ô âm thầm chặn đề xuất ───────────────────────────────────────────
    if v.get("tre_em"):
        ra.append(_loi(
            "Đang đánh dấu là nội dung cho trẻ em", NANG,
            "Video bị tắt bình luận, tắt thông báo và gần như không được đề xuất.",
            "Studio, thẻ Chi tiết, mục Đối tượng, chọn Không, video này không "
            "dành cho trẻ em."))
    if v.get("chan_vung"):
        ra.append(_loi(
            "Đang bị chặn theo vùng", NANG,
            f"Cài đặt chặn vùng: {v['chan_vung']}",
            "Studio, thẻ Chi tiết, bấm HIỆN THÊM, bỏ phần giới hạn khu vực."))
    rieng = (v.get("rieng_tu") or "").lower()
    if rieng and rieng != "public":
        ra.append(_loi(
            "Chưa công khai", NHE,
            f"Đang ở chế độ '{rieng}', nên không ai tìm thấy.",
            "Đổi sang Công khai nếu đây không phải cố ý."))

    # ── Mô tả ────────────────────────────────────────────────────────────────
    mo_ta = v.get("mo_ta") or ""
    if len(mo_ta) < MO_TA_TOI_THIEU:
        ra.append(_loi(
            "Mô tả quá ngắn", NHE,
            f"Mới {len(mo_ta)} ký tự. YouTube đọc mô tả để hiểu video nói về gì.",
            f"Viết ít nhất {MO_TA_TOI_THIEU} ký tự, tóm nội dung và các mốc chính."))
    return ra


def soat(videos: List[Dict], ngon_ngu: str = "") -> Dict:
    """
    Soát cả kênh.

    Xếp video có lỗi nặng lên trước: lỗi nặng là lỗi chặn video tới được người
    xem, còn lỗi nhẹ chỉ là phí một cơ hội.
    """
    dung = (ngon_ngu or "").strip().lower()[:2] or ngon_ngu_kenh(videos)
    ket = []
    for v in videos:
        loi = soat_mot(v, dung)
        ket.append({
            "id": v.get("id", ""), "title": v.get("title", ""),
            "url": v.get("url", ""), "thumb": v.get("thumb", ""),
            "views": v.get("views", 0),
            "loi": loi,
            "so_nang": sum(1 for x in loi if x["nang"] == NANG),
            "so_nhe": sum(1 for x in loi if x["nang"] == NHE),
        })
    ket.sort(key=lambda x: (-x["so_nang"], -x["so_nhe"]))
    nang = sum(x["so_nang"] for x in ket)
    nhe = sum(x["so_nhe"] for x in ket)
    return {
        "ngon_ngu_kenh": dung,
        "videos": ket,
        "so_nang": nang,
        "so_nhe": nhe,
        "sach": nang == 0 and nhe == 0,
        "tom_tat": _tom_tat(len(ket), nang, nhe),
    }


def _tom_tat(so_video: int, nang: int, nhe: int) -> str:
    if not so_video:
        return "Chưa có video nào để soát."
    if not nang and not nhe:
        return f"Soát {so_video} video, không thấy lỗi khai báo nào."
    phan = []
    if nang:
        phan.append(f"{nang} lỗi nặng, loại chặn video tới được người xem")
    if nhe:
        phan.append(f"{nhe} lỗi nhẹ, loại phí cơ hội")
    return f"Soát {so_video} video, thấy " + " và ".join(phan) + "."
