"""
Sơ đồ hành trình: vẽ đường đi của một chiến dịch, một chuyến lưu lạc, một cuộc
dời đô, bằng các chấm nối với nhau và TÊN ĐỊA DANH viết rõ.

Vì sao không dùng model vẽ ảnh cho việc này. Luật viết prompt của tool cấm
tuyệt đối chữ, chữ cái, con số và biển hiệu trong tranh, vì model vẽ ra chữ
loằng ngoằng không đọc nổi và tiếng Việt thì càng hỏng. Nhưng một hành trình
mà không đọc được tên nơi đi nơi đến thì chỉ là mấy vệt màu. Nên phần này để
Remotion vẽ: chữ do trình duyệt dựng, sắc nét, đúng dấu.

Vì sao KHÔNG làm bản đồ địa lý thật. Muốn đúng thì phải có toạ độ thật, đường
bờ biển thật, biên giới đúng theo từng thời kỳ. Vẽ sai một đường biên trong
video lịch sử là mời người ta vào phần bình luận cãi nhau về chuyện không liên
quan tới câu chuyện đang kể. Sơ đồ rút gọn thì không hứa hẹn điều gì về địa
lý: nó chỉ nói đi từ đâu tới đâu, theo thứ tự nào.

Bố trí: các chấm chạy từ trái sang phải theo thứ tự kể, so le trên dưới quanh
trục giữa để đường nối có nhịp chứ không phải một gạch ngang chết.
"""
from typing import Dict, List, Optional

# Hai điểm mới thành một hành trình. Quá sáu thì chữ chen nhau không đọc được,
# mà lời kể hai mươi giây cũng không kịp đọc tên bảy nơi.
SO_DIEM_TOI_THIEU = 2
SO_DIEM_TOI_DA = 6

# Toạ độ 0..1 của khung hình. Chừa lề để tên địa danh dài không bị cắt mép.
LE_NGANG = 0.14
# Trục giữa đẩy xuống dưới 0.5 một chút: phía trên chừa chỗ cho tên hành trình,
# phía dưới là chỗ phụ đề chạy.
TRUC_DOC = 0.54
BIEN_DO_DOC = 0.17


def bo_tri(diem: List[str]) -> List[Dict]:
    """
    Đặt từng điểm vào toạ độ 0..1 của khung hình.

    Trả về danh sách {"ten", "x", "y", "thu_tu"}. Toạ độ là tỉ lệ chứ không phải
    pixel, để dùng chung cho cả khổ ngang lẫn khổ dọc.
    """
    ten = [str(d).strip() for d in (diem or []) if str(d).strip()][:SO_DIEM_TOI_DA]
    if len(ten) < SO_DIEM_TOI_THIEU:
        return []
    if len(ten) == 1:
        return [{"ten": ten[0], "x": 0.5, "y": TRUC_DOC, "thu_tu": 0}]
    buoc = (1.0 - 2 * LE_NGANG) / (len(ten) - 1)
    ra = []
    for i, t in enumerate(ten):
        # So le trên dưới. Điểm đầu và điểm cuối kéo về gần trục giữa để hai
        # đầu hành trình nhìn cân, phần lệch dồn vào các chặng giữa.
        huong = 1 if i % 2 else -1
        rieu = 0.45 if i in (0, len(ten) - 1) else 1.0
        ra.append({"ten": t, "thu_tu": i,
                   "x": round(LE_NGANG + buoc * i, 4),
                   "y": round(TRUC_DOC + huong * BIEN_DO_DOC * rieu, 4)})
    return ra


SO_DO_SYSTEM = """You read one narration paragraph from a history video and decide whether it
describes a JOURNEY or a CAMPAIGN with named places visited in order.

Return ONLY JSON: {"route": ["place one", "place two"], "title": "..."}.
- "route": the named places IN THE ORDER THE NARRATION VISITS THEM, 2 to 6 of them.
  Return an EMPTY list when the paragraph does not move between named places.
- "title": three to six words naming the journey, in the same language as the narration.
- Only include a place the narration actually names. Never invent, never add a place you
  know from history but the paragraph did not mention.
- A paragraph that names two places without travelling between them is NOT a journey.
  "He was born in A and died in B" is not a journey. "He marched from A to B, then on to C" is.
- Keep each place name short, as the narration writes it, with no extra description."""


def doc_hanh_trinh(text: str, settings: Optional[Dict] = None,
                   hoi=None) -> Dict:
    """
    Hỏi AI xem một đoạn lời dẫn có phải một hành trình không, lấy ra các chặng.

    `hoi` để test thay chỗ gọi AI. Không có hành trình thì trả về điểm rỗng,
    đó là kết quả thường gặp nhất chứ không phải lỗi.
    """
    if hoi is None:
        # Nạp muộn: longform_studio nạp so_do, nạp ngược ở đầu file thì vòng.
        from longform_studio import ask_json as hoi
    doan = " ".join((text or "").split())
    if not doan:
        return {"success": True, "diem": [], "tieu_de": ""}
    res = hoi(SO_DO_SYSTEM, f"NARRATION:\n{doan[:900]}", settings or {}, max_tokens=400)
    if not res.get("success"):
        return {"success": False, "error": res.get("error") or "Không hỏi được AI"}
    data = res.get("data") or {}
    diem = bo_tri([str(x) for x in (data.get("route") or [])])
    return {"success": True, "diem": diem,
            "tieu_de": str(data.get("title") or "").strip() if diem else ""}


def gan_so_do(scenes: List[Dict], settings: Optional[Dict] = None,
              toi_da: int = 2, hoi=None) -> Dict:
    """
    Duyệt các cảnh, gắn sơ đồ vào những cảnh thật sự kể một hành trình.

    `toi_da` chặn số sơ đồ trong một video. Sơ đồ là thứ đổi vị khung hình, mà
    đổi vị năm lần trong một video thì chính nó thành cái đơn điệu mới. Hai cái
    cho một video bốn mươi phút là vừa.
    """
    dat = 0
    loi = ""
    for canh in scenes:
        if dat >= toi_da:
            break
        # Cảnh chào và cảnh dẫn dắt không kể hành trình, khỏi tốn lượt hỏi AI.
        if canh.get("chao") or canh.get("dan_dat"):
            continue
        res = doc_hanh_trinh(canh.get("text") or "", settings, hoi=hoi)
        if not res.get("success"):
            loi = res.get("error") or loi
            continue
        if res["diem"]:
            canh["route"] = {"diem": res["diem"], "tieu_de": res["tieu_de"]}
            dat += 1
    return {"success": True, "so_do": dat, "error": loi}
