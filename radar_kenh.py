"""
radar_kenh.py
─────────────
Ra-đa theo dõi kênh cùng ngành.

Nút thắt không còn là làm được bao nhiêu video, mà là biết nên làm cái gì. Ngày
13/09/2026 đã mổ tay một kênh địa chất ba tuần tuổi: mười bốn video, 2,72 triệu
lượt xem, nhưng 2,7 triệu nằm ở đúng một video. Mười ba cái còn lại cộng lại
được hai chục nghìn. Cái đáng học không phải là họ đăng đều, mà là video nào
vọt lên và vì sao.

Việc đó làm tay mất mười phút mỗi kênh. File này làm thay.

Cách đo: với mỗi kênh, lấy lượt xem của những video gần đây rồi so từng video
với trung vị của chính kênh đó. Trung vị chứ không phải trung bình, vì một
video triệu view kéo trung bình lên cao tới mức không video nào còn "vượt" nữa.
So với chính kênh đó chứ không so giữa các kênh, vì kênh trăm nghìn người theo
dõi và kênh nghìn người theo dõi không cùng thang.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "radar", "radar.json")

TRINH_DUYET = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Bao nhiêu lần trung vị thì gọi là chạy vượt mức thường của kênh đó. Lấy 2,5
# vì dưới mức này phần lớn chỉ là dao động ngày thường, còn trên nó thì gần như
# luôn có một lý do rõ ràng để đi tìm.
NGUONG_VUOT = 2.5
# Video mới đăng chưa đủ thời gian tích lượt xem, đem so là oan cho nó.
TOI_THIEU_NGAY = 2
MAX_KENH = 30


def _doc_so(chu: str) -> int:
    """
    "2.5M views" thành 2500000. Trả 0 khi không đọc được.

    YouTube viết tắt theo ngôn ngữ giao diện, nên luôn hỏi trang bằng tiếng Anh
    để chỉ phải hiểu đúng một cách viết.
    """
    got = re.search(r"([\d.,]+)\s*([KMB])?", chu or "")
    if not got:
        return 0
    try:
        so = float(got.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(so * {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(got.group(2) or "", 1))


def _doc_tuoi_ngay(chu: str) -> Optional[int]:
    """
    "3 weeks ago" thành 21. Không đọc được thì trả None chứ không đoán bừa.

    Từ tháng 10/2026 trang kênh viết gọn thành "3w ago", "2mo ago", nên nhận cả hai kiểu.
    """
    got = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year|mo|min|s|h|d|w|y)", chu or "", re.I)
    if not got:
        return None
    so = int(got.group(1))
    return so * {"second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365,
                 "s": 0, "min": 0, "h": 0, "d": 1, "w": 7, "mo": 30, "y": 365}[got.group(2).lower()]


def _tim(o, khoa: str, ra: Optional[List] = None) -> List:
    """Mọi giá trị nằm dưới một khoá, ở bất kỳ tầng nào."""
    ra = [] if ra is None else ra
    if isinstance(o, dict):
        for k, v in o.items():
            if k == khoa:
                ra.append(v)
            else:
                _tim(v, khoa, ra)
    elif isinstance(o, list):
        for v in o:
            _tim(v, khoa, ra)
    return ra


def _tai_trang(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        # Hỏi bằng tiếng Anh để số lượt xem luôn viết theo một kiểu.
        "User-Agent": TRINH_DUYET, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", "ignore")


def doc_kenh(url: str, toi_da: int = 30) -> Dict:
    """
    Video gần đây của một kênh, kèm lượt xem và tuổi.

    Đọc thẳng dữ liệu trang kênh thay vì gọi API: không cần khoá, một lượt tải
    là có cả kênh, và không dính hạn mức.
    """
    url = (url or "").strip()
    if not url:
        return {"success": False, "error": "Chưa có địa chỉ kênh."}
    if not url.startswith("http"):
        url = "https://www.youtube.com/" + url.lstrip("/")
    if not re.match(r"https?://(www\.)?youtube\.com/", url):
        return {"success": False, "error": "Chỉ nhận địa chỉ kênh YouTube."}
    if "/videos" not in url:
        url = url.rstrip("/") + "/videos"

    try:
        html = _tai_trang(url)
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": f"Trang trả về mã {exc.code}. Kiểm tra lại địa chỉ kênh."}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"Không mở được trang kênh: {str(exc)[:120]}"}

    got = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not got:
        return {"success": False, "error": "Trang kênh không trả về dữ liệu đọc được."}
    try:
        data = json.loads(got.group(1))
    except ValueError:
        return {"success": False, "error": "Dữ liệu trang kênh hỏng."}

    ten = ""
    for t in _tim(data, "pageHeaderViewModel")[:1]:
        chu = [c for c in _tim(t, "content") if isinstance(c, str)]
        ten = chu[0] if chu else ""
    if not ten:
        ten = (re.search(r'<title>([^<]+)</title>', html) or [None, ""])[1].replace(" - YouTube", "").strip()

    videos = []
    for lk in _tim(data, "lockupViewModel")[:toi_da]:
        ma = lk.get("contentId")
        if not ma:
            continue
        # Mỗi phần có chữ hiện ra ("90K", "1d ago") và nhãn đọc màn hình ("90 thousand views", "1 day ago").
        # Từ tháng 10/2026 chữ hiện ra không còn từ "views", nên ghép cả nhãn vào để nhận ra phần nào là lượt xem.
        phan = []
        hang = _tim(lk, "metadataParts")
        if hang:
            phan = [f"{p.get('text', {}).get('content', '')} {p.get('accessibilityLabel', '')}".strip()
                    for p in hang[0] if isinstance(p, dict)]
        tieu_de = next((c for c in _tim(lk, "content")
                        if isinstance(c, str) and len(c) > 8 and "views" not in c), "")
        luot = next((_doc_so(p) for p in phan if "view" in p.lower()), 0)
        tuoi = next((_doc_tuoi_ngay(p) for p in phan if "ago" in p.lower()), None)
        videos.append({"id": ma, "title": tieu_de, "views": luot, "tuoi_ngay": tuoi,
                       "url": f"https://www.youtube.com/watch?v={ma}"})
    if not videos:
        return {"success": False, "error": "Kênh này chưa có video công khai, hoặc trang đã đổi cấu trúc."}
    return {"success": True, "channel": ten, "url": url, "videos": videos}


def do_vuot_muc(videos: List[Dict]) -> Dict:
    """
    So từng video với mức thường của chính kênh đó.

    Trả về trung vị lượt xem và danh sách video vượt ngưỡng, xếp theo mức vượt.
    """
    du = [v for v in videos if v.get("views") and (v.get("tuoi_ngay") is None
                                                   or v["tuoi_ngay"] >= TOI_THIEU_NGAY)]
    if len(du) < 4:
        return {"trung_vi": 0, "vuot": [], "du_lieu_mong": True}
    xep = sorted(v["views"] for v in du)
    giua = len(xep) // 2
    trung_vi = xep[giua] if len(xep) % 2 else (xep[giua - 1] + xep[giua]) // 2
    if trung_vi <= 0:
        return {"trung_vi": 0, "vuot": [], "du_lieu_mong": True}
    vuot = []
    for v in du:
        lan = v["views"] / trung_vi
        if lan >= NGUONG_VUOT:
            vuot.append({**v, "gap_lan": round(lan, 1)})
    vuot.sort(key=lambda v: v["gap_lan"], reverse=True)
    return {"trung_vi": trung_vi, "vuot": vuot, "du_lieu_mong": False}


# ─────────────────────────────────────────────────────────────────────────────
# TÌM GÓC KỂ CHO MỘT NHÂN VẬT
# ─────────────────────────────────────────────────────────────────────────────

# Gõ tên một nhân vật lịch sử lên YouTube thì phần lớn kết quả là phim truyền
# hình, không phải video kể chuyện. Đo ngày 15/09/2026 với "Địch Nhân Kiệt":
# ba kết quả đầu là phim với 2,1 triệu, 1,5 triệu và 1 triệu lượt xem, trong
# khi video kể chuyện cao nhất chỉ 198 nghìn. Không lọc thì con số nhu cầu bị
# thổi lên gấp mười, và người dùng chọn nhầm đề tài vì tưởng đang có cầu lớn.
DAU_HIEU_PHIM = (
    "phim", "thuyết minh", "thuyet minh", "tập ", "tap ", "full hd", "trailer",
    "review phim", "tóm tắt phim", "vietsub", "lồng tiếng", "long tieng",
    "phần cuối", "phan cuoi", "siêu phẩm", "sieu pham", "mv ", "official",
    # Phim Trung Quốc thuyết minh hay để tên tiếng Anh, lọt qua mọi dấu hiệu
    # tiếng Việt ở trên. Đo 15/09/2026: hai phim kiểu này lọt vào danh sách
    # góc kể và đứng ngay đầu bảng.
    "dubbed", "movie", "episode", "ep.", "detective dee", "season",
    # Bài hát trùng tên nhân vật. Đo 19/09/2026: tìm "Hàn Tín" thì video đứng
    # đầu bảng là một bài hát 1,18 triệu lượt, chiếm 38% tổng nhu cầu đo được,
    # và nó đẩy Hàn Tín ra khỏi danh sách gợi ý vì trông như đã bị một video
    # nuốt hết. Bài hát không cạnh tranh gì với video kể chuyện, nên phải lọc.
    " ft ", " ft.", "feat.", " feat ", "karaoke", "lyrics", "lyric video",
    "nhạc chế", "cover ", "remix", "acoustic", "official audio", "beat ",
    # Cải lương và tân cổ cũng lấy tên nhân vật lịch sử làm tựa. Đo 19/09/2026:
    # tìm "Kinh Kha" thì bản cải lương "Hận Kinh Kha" đứng đầu bảng với 2,46
    # triệu lượt. Người nghe cải lương và người xem video kể sử là hai tập khác
    # nhau, gộp vào là con số nhu cầu vô nghĩa.
    "cải lương", "cai luong", "tân cổ", "tan co", "vọng cổ", "vong co",
    "trích đoạn", "trich doan", "chèo", "tuồng cổ",
)
# Video kể chuyện thật thường mang những chữ này.
DAU_HIEU_KE = ("cuộc đời", "tiểu sử", "là ai", "vì sao", "tại sao", "lịch sử",
               "giải mã", "sự thật", "câu chuyện", "chân dung", "kỳ án")


def la_phim(tieu_de: str, tieng_viet: bool = True) -> bool:
    """
    Tiêu đề này là phim hay là video kể chuyện.

    Ngoài các dấu hiệu chữ, còn một dấu hiệu nữa: đang tìm bằng tiếng Việt mà
    tiêu đề không có lấy một dấu thanh nào thì gần như chắc là phim thuyết
    minh hoặc kênh nước ngoài, không phải video kể chuyện cho người Việt.
    """
    chu = " " + (tieu_de or "").lower() + " "
    if any(d in chu for d in DAU_HIEU_PHIM):
        return True
    if tieng_viet and not re.search(r"[àáảãạăâđêôơưèéẻẽẹìíỉĩịòóỏõọùúủũụỳýỷỹỵ]", chu):
        return True
    return False


def _tim_kiem(tu_khoa: str, lay: int = 12) -> List[Dict]:
    """Kết quả tìm kiếm YouTube cho một cụm từ."""
    import urllib.parse

    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(tu_khoa)
    try:
        html = _tai_trang(url)
    except Exception:  # noqa: BLE001
        return []
    got = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not got:
        return []
    try:
        data = json.loads(got.group(1))
    except ValueError:
        return []
    ra = []
    for v in _tim(data, "videoRenderer")[:lay]:
        tieu_de = next((t for t in _tim(v, "text") if isinstance(t, str) and len(t) > 10), "")
        luot = next((t for t in _tim(v, "simpleText")
                     if isinstance(t, str) and ("view" in t.lower() or "xem" in t.lower())), "")
        kenh = ""
        for o in _tim(v, "ownerText"):
            kenh = next((t for t in _tim(o, "text") if isinstance(t, str)), "")
        ma = v.get("videoId") if isinstance(v, dict) else ""
        if tieu_de:
            ra.append({"title": tieu_de, "views": _doc_so(luot), "channel": kenh, "id": ma})
    return ra


def tim_goc_ke(ten: str, sau_loc: int = 14) -> Dict:
    """
    Những góc kể đang có cho một nhân vật, xếp theo lượt xem.

    Hỏi YouTube bằng vài cách khác nhau rồi gộp lại, vì mỗi cách hỏi ra một
    nhóm kết quả khác. Phim bị tách riêng chứ không xoá hẳn: biết mảng phim to
    tới đâu cũng là thông tin, chỉ là không được trộn vào con số nhu cầu.
    """
    ten = (ten or "").strip()
    if not ten:
        return {"success": False, "error": "Chưa nhập tên nhân vật."}
    cac_cau = [ten, f"{ten} là ai", f"cuộc đời {ten}", f"{ten} vì sao"]
    gom: Dict[str, Dict] = {}
    for cau in cac_cau:
        for v in _tim_kiem(cau):
            cu = gom.get(v["title"])
            if not cu or v["views"] > cu["views"]:
                gom[v["title"]] = v
        time.sleep(0.4)
    if not gom:
        return {"success": False, "error": "Không tìm được video nào về tên này."}
    ke, phim = [], []
    for v in gom.values():
        (phim if la_phim(v["title"]) else ke).append(v)
    ke.sort(key=lambda v: v["views"], reverse=True)
    phim.sort(key=lambda v: v["views"], reverse=True)
    return {"success": True, "ten": ten,
            "goc": ke[:sau_loc],
            "phim": phim[:5],
            "tong_ke": sum(v["views"] for v in ke),
            "tong_phim": sum(v["views"] for v in phim)}


# ─────────────────────────────────────────────────────────────────────────────
# KHO: DANH SÁCH KÊNH THEO DÕI
# ─────────────────────────────────────────────────────────────────────────────

def _blank() -> Dict:
    return {"channels": [], "quet_luc": 0}


def load() -> Dict:
    try:
        with open(KHO, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("channels"), list):
            return data
    except Exception:  # noqa: BLE001
        pass
    return _blank()


def _write(state: Dict) -> None:
    os.makedirs(os.path.dirname(KHO), exist_ok=True)
    tmp = KHO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, KHO)


def them_kenh(url: str) -> Dict:
    """Thêm một kênh vào danh sách theo dõi. Đọc thử luôn để biết địa chỉ có sống không."""
    got = doc_kenh(url, toi_da=6)
    if not got.get("success"):
        return got
    state = load()
    if any(c["url"] == got["url"] for c in state["channels"]):
        return {"success": False, "error": "Kênh này đã có trong danh sách."}
    if len(state["channels"]) >= MAX_KENH:
        return {"success": False, "error": f"Đang theo dõi tối đa {MAX_KENH} kênh. Bỏ bớt rồi thêm."}
    state["channels"].append({"url": got["url"], "name": got["channel"], "them_luc": time.time()})
    _write(state)
    return {"success": True, "channels": state["channels"]}


def bo_kenh(url: str) -> Dict:
    state = load()
    state["channels"] = [c for c in state["channels"] if c["url"] != url]
    _write(state)
    return {"success": True, "channels": state["channels"]}


def quet(toi_da_moi_kenh: int = 30) -> Dict:
    """
    Quét tất cả kênh đang theo dõi, chỉ ra video nào đang chạy vượt mức.

    Một kênh hỏng không được làm chết cả lượt quét: ghi lý do rồi đi tiếp.
    """
    state = load()
    ket_qua, hong = [], []
    for kenh in state["channels"]:
        got = doc_kenh(kenh["url"], toi_da_moi_kenh)
        if not got.get("success"):
            hong.append({"channel": kenh.get("name") or kenh["url"], "error": got.get("error", "")})
            continue
        do = do_vuot_muc(got["videos"])
        ket_qua.append({"channel": got["channel"], "url": kenh["url"],
                        "so_video": len(got["videos"]), **do})
    # Video vượt mức nhiều nhất lên đầu, vì đó là thứ đáng đọc trước.
    noi_bat = []
    for k in ket_qua:
        for v in k["vuot"]:
            noi_bat.append({**v, "channel": k["channel"]})
    noi_bat.sort(key=lambda v: v["gap_lan"], reverse=True)
    state["quet_luc"] = time.time()
    _write(state)
    return {"success": True, "channels": ket_qua, "noi_bat": noi_bat[:40],
            "hong": hong, "quet_luc": state["quet_luc"]}
