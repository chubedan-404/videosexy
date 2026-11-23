"""
Khung giờ đăng video ngắn gợi ý cho từng thị trường của Global Creator Studio.

Mọi giờ trong bảng là GIỜ ĐỊA PHƯƠNG của thị trường, tính theo múi giờ IANA
khai báo ở `SHORTS_MARKETS` trong `remotion_generator.py`. Nhờ vậy giờ mùa hè
của Mỹ và châu Âu tự được tính đúng, không cần sửa bảng mỗi năm hai lần.

Đây là khung tham khảo theo thói quen lướt điện thoại chung của từng nước
(giờ đi làm, nghỉ trưa, buổi tối sau bữa ăn), KHÔNG phải số liệu đo từ Fanpage
của người dùng. Giao diện phải nói rõ điều đó.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

# Mỗi khung: giờ bắt đầu, giờ kết thúc (không vắt qua nửa đêm), nhãn, và cờ
# `prime` cho khung đông người xem nhất trong ngày.
POSTING_WINDOWS: Dict[str, Dict] = {
    "vi-VN": {
        "windows": [
            {"start": "06:30", "end": "08:00", "label": "Sáng sớm, trước giờ làm", "prime": False},
            {"start": "11:00", "end": "13:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "19:30", "end": "22:30", "label": "Buổi tối sau bữa cơm", "prime": True},
        ],
        "note": "",
    },
    "en-US": {
        "windows": [
            {"start": "06:30", "end": "08:30", "label": "Sáng sớm, trước giờ làm", "prime": False},
            {"start": "12:00", "end": "14:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "19:00", "end": "22:00", "label": "Buổi tối", "prime": True},
        ],
        "note": ("Mỹ trải dài bốn múi giờ. Tool lấy giờ miền Đông vì đông dân nhất; "
                 "19:00 ở New York là 16:00 ở bờ Tây, nên khung tối vẫn phủ được cả nước."),
    },
    "en-GB": {
        "windows": [
            {"start": "07:00", "end": "09:00", "label": "Trên đường đi làm", "prime": False},
            {"start": "12:00", "end": "13:30", "label": "Nghỉ trưa", "prime": False},
            {"start": "19:00", "end": "22:00", "label": "Buổi tối", "prime": True},
        ],
        "note": "",
    },
    "es-ES": {
        "windows": [
            {"start": "08:00", "end": "09:30", "label": "Sáng sớm, trước giờ làm", "prime": False},
            {"start": "14:00", "end": "16:00", "label": "Bữa trưa kiểu Tây Ban Nha", "prime": False},
            {"start": "21:00", "end": "23:30", "label": "Buổi tối muộn", "prime": True},
        ],
        "note": "Người Tây Ban Nha ăn trưa và ăn tối muộn hơn phần lớn châu Âu khoảng hai tiếng.",
    },
    "pt-BR": {
        "windows": [
            {"start": "07:00", "end": "09:00", "label": "Trên đường đi làm", "prime": False},
            {"start": "12:00", "end": "14:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "19:00", "end": "23:00", "label": "Buổi tối", "prime": True},
        ],
        "note": "Tool lấy giờ São Paulo, cũng là giờ của Rio và Brasília, nơi tập trung phần lớn dân số.",
    },
    "fr-FR": {
        "windows": [
            {"start": "07:30", "end": "09:00", "label": "Trên đường đi làm", "prime": False},
            {"start": "12:00", "end": "14:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "19:00", "end": "22:00", "label": "Buổi tối", "prime": True},
        ],
        "note": "",
    },
    "de-DE": {
        "windows": [
            {"start": "06:30", "end": "08:30", "label": "Sáng sớm, trước giờ làm", "prime": False},
            {"start": "12:00", "end": "13:30", "label": "Nghỉ trưa", "prime": False},
            {"start": "18:30", "end": "21:30", "label": "Buổi tối", "prime": True},
        ],
        "note": "Người Đức dậy sớm và nghỉ sớm hơn Pháp, Tây Ban Nha, nên khung tối cũng lùi sớm hơn.",
    },
    "ja-JP": {
        "windows": [
            {"start": "07:00", "end": "08:30", "label": "Trên tàu đi làm", "prime": False},
            {"start": "12:00", "end": "13:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "20:00", "end": "23:00", "label": "Buổi tối muộn", "prime": True},
        ],
        "note": "Giờ tan làm ở Nhật muộn, khán giả đông nhất thường rơi vào sau 21:00.",
    },
    "ko-KR": {
        "windows": [
            {"start": "07:30", "end": "09:00", "label": "Trên tàu đi làm", "prime": False},
            {"start": "12:00", "end": "13:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "20:00", "end": "23:30", "label": "Buổi tối muộn", "prime": True},
        ],
        "note": "",
    },
    "th-TH": {
        "windows": [
            {"start": "07:00", "end": "08:30", "label": "Sáng sớm, trước giờ làm", "prime": False},
            {"start": "12:00", "end": "13:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "18:00", "end": "22:00", "label": "Buổi tối", "prime": True},
        ],
        "note": "",
    },
    "id-ID": {
        "windows": [
            {"start": "06:00", "end": "08:00", "label": "Sáng sớm", "prime": False},
            {"start": "11:30", "end": "13:00", "label": "Nghỉ trưa", "prime": False},
            {"start": "19:00", "end": "22:00", "label": "Buổi tối", "prime": True},
        ],
        "note": ("Tool lấy giờ Jakarta (miền Tây). Bali và Sulawesi nhanh hơn 1 tiếng, "
                 "Papua nhanh hơn 2 tiếng. Tháng Ramadan giờ lướt điện thoại dời hẳn về đêm và sát giờ xả chay."),
    },
    "zh-CN": {
        "windows": [
            {"start": "07:30", "end": "09:00", "label": "Trên đường đi làm", "prime": False},
            {"start": "12:00", "end": "13:30", "label": "Nghỉ trưa", "prime": False},
            {"start": "20:00", "end": "23:00", "label": "Buổi tối", "prime": True},
        ],
        "note": ("Facebook bị chặn ở Trung Quốc đại lục. Video tiếng Trung đăng Facebook chủ yếu tới người Hoa "
                 "ở Đài Loan, Hồng Kông, Singapore và Malaysia, cùng múi giờ UTC+8 nên khung giờ này vẫn dùng được."),
    },
}

DISCLAIMER = ("Khung tham khảo theo thói quen dùng điện thoại chung của từng nước, "
              "chưa dựa trên số liệu Fanpage của bạn.")

# Đang ở giữa khung thì gợi ý đăng sau ít phút, đủ để bấm hẹn và để bộ lên lịch
# (quét mỗi 30 giây) kịp nhận.
IN_WINDOW_LEAD_MINUTES = 10


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _round_up_to_5_minutes(moment: datetime) -> datetime:
    moment = moment.replace(second=0, microsecond=0)
    extra = (-moment.minute) % 5
    return moment + timedelta(minutes=extra)


def _utc_offset_label(moment: datetime) -> str:
    """UTC+9, UTC-4, UTC+5:30."""
    offset = moment.utcoffset() or timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def upcoming_slots(market_id: str, now: Optional[datetime] = None, limit: int = 6) -> List[Dict]:
    """
    Các suất đăng sắp tới theo giờ địa phương của thị trường, xếp theo thời
    gian. Khung đã qua bị bỏ; khung đang diễn ra thì gợi ý đăng sau ít phút.
    """
    from remotion_generator import get_shorts_market
    market = get_shorts_market(market_id)
    tz = ZoneInfo(market["timezone"])
    windows = POSTING_WINDOWS.get(market["id"], POSTING_WINDOWS["vi-VN"])["windows"]
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today: date = now_utc.astimezone(tz).date()

    slots = []
    for day_offset in range(3):
        day = today + timedelta(days=day_offset)
        for window in windows:
            start = datetime.combine(day, _parse_hhmm(window["start"]), tzinfo=tz)
            end = datetime.combine(day, _parse_hhmm(window["end"]), tzinfo=tz)
            lead = now_utc + timedelta(minutes=IN_WINDOW_LEAD_MINUTES)
            if end.astimezone(timezone.utc) <= lead:
                continue
            in_progress = start.astimezone(timezone.utc) <= now_utc
            post_at = _round_up_to_5_minutes(lead.astimezone(tz)) if start.astimezone(timezone.utc) < lead else start
            if post_at >= end:
                continue
            slots.append((post_at, {
                "post_at": post_at.isoformat(),
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "label": window["label"],
                "prime": bool(window.get("prime")),
                "in_progress": in_progress,
            }))
    # Sắp theo mốc thật chứ không theo chuỗi: qua ngày đổi giờ mùa hè thì hai
    # chuỗi mang hai độ lệch UTC khác nhau và thứ tự chữ không còn đúng.
    slots.sort(key=lambda pair: pair[0])
    return [slot for _, slot in slots[:limit]]


def posting_plan(market_id: str, now: Optional[datetime] = None) -> Dict:
    """Toàn bộ dữ liệu giao diện cần để vẽ khối giờ đăng của một thị trường."""
    from remotion_generator import get_shorts_market
    market = get_shorts_market(market_id)
    tz = ZoneInfo(market["timezone"])
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    info = POSTING_WINDOWS.get(market["id"], POSTING_WINDOWS["vi-VN"])
    return {
        "market_id": market["id"],
        "city": market["city"],
        "timezone": market["timezone"],
        "utc_offset": _utc_offset_label(now_utc.astimezone(tz)),
        "windows": info["windows"],
        "note": info["note"],
        "disclaimer": DISCLAIMER,
        "slots": upcoming_slots(market["id"], now=now_utc),
    }


def localize_schedule_time(value: str, timezone_name: str = "") -> datetime:
    """
    Đổi chuỗi giờ hẹn thành mốc có múi giờ rõ ràng.

    Chuỗi có sẵn múi giờ thì giữ nguyên. Chuỗi không có múi giờ (ô chọn ngày
    giờ của trình duyệt gửi lên dạng này) thì hiểu theo `timezone_name` nếu có,
    không thì theo giờ của máy đang chạy tool. Chuỗi sai định dạng thì ném
    ValueError để nơi gọi báo lỗi rõ ràng.
    """
    moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        return moment
    if timezone_name:
        return moment.replace(tzinfo=ZoneInfo(timezone_name))
    return moment.astimezone()
