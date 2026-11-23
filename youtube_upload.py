"""
Đăng video lên YouTube bằng tài khoản của chính người chạy tool.

Viết cho một kênh, là Thanh Sử Quán. Một Gmail quản được nhiều kênh, nên sau khi
cấp quyền tool hỏi luôn xem mình vừa nối vào kênh nào rồi giữ tên đó lại: chọn
nhầm kênh lúc cấp quyền là video lên nhầm chỗ mà không có gì báo.

Hạn mức mỗi ngày là 10.000 điểm. Tải một video tốn 1.600, đặt ảnh bìa tốn 50,
hỏi tên kênh tốn 1. Tức là khoảng sáu video một ngày, thừa cho hàng đợi đêm.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KHO = os.path.join(BASE_DIR, "downloads", "youtube")
CHANNELS_DIR = os.path.join(KHO, "channels")
FILE_ACTIVE_CHANNEL = os.path.join(KHO, "active_channel.json")
FILE_TOKEN = os.path.join(KHO, "token.json")
FILE_KENH = os.path.join(KHO, "kenh.json")


def active_channel_id() -> str:
    """Trả về ID kênh YouTube đang được kích hoạt."""
    data = _doc_json(FILE_ACTIVE_CHANNEL)
    if data.get("active_channel_id"):
        return data["active_channel_id"]
    kenh = _doc_json(FILE_KENH)
    return kenh.get("id", "")


def duong_token(cid: Optional[str] = None) -> str:
    cid = cid or active_channel_id()
    if cid:
        p = os.path.join(CHANNELS_DIR, cid, "token.json")
        if os.path.isfile(p):
            return p
    return FILE_TOKEN


def duong_kenh(cid: Optional[str] = None) -> str:
    cid = cid or active_channel_id()
    if cid:
        p = os.path.join(CHANNELS_DIR, cid, "kenh.json")
        if os.path.isfile(p):
            return p
    return FILE_KENH


def set_active_channel(cid: str) -> bool:
    """Chuyển đổi kênh đang kích hoạt sang channel_id mới."""
    if not cid:
        return False
    cdir = os.path.join(CHANNELS_DIR, cid)
    t_file = os.path.join(cdir, "token.json")
    k_file = os.path.join(cdir, "kenh.json")
    if not os.path.isfile(t_file):
        return False
    _ghi_json(FILE_ACTIVE_CHANNEL, {"active_channel_id": cid})
    import shutil
    try:
        shutil.copy2(t_file, FILE_TOKEN)
        if os.path.isfile(k_file):
            shutil.copy2(k_file, FILE_KENH)
        s_file = os.path.join(cdir, "so_lieu.json")
        f_so_lieu = os.path.join(KHO, "so_lieu.json")
        if os.path.isfile(s_file):
            shutil.copy2(s_file, f_so_lieu)
        elif os.path.isfile(f_so_lieu):
            try:
                os.remove(f_so_lieu)
            except OSError:
                pass
    except Exception as e:
        print(f"[⚠️ set_active_channel sync error]: {e}")
    return True


def danh_sach_kenh() -> List[Dict]:
    """Trả về danh sách tất cả các kênh YouTube đã kết nối."""
    os.makedirs(CHANNELS_DIR, exist_ok=True)
    cur_id = active_channel_id()
    ket_qua = []
    seen_ids = set()

    if os.path.isdir(CHANNELS_DIR):
        for name in sorted(os.listdir(CHANNELS_DIR)):
            cdir = os.path.join(CHANNELS_DIR, name)
            if os.path.isdir(cdir):
                k_file = os.path.join(cdir, "kenh.json")
                t_file = os.path.join(cdir, "token.json")
                if os.path.isfile(t_file):
                    kdata = _doc_json(k_file)
                    cid = kdata.get("id") or name
                    seen_ids.add(cid)
                    ket_qua.append({
                        "id": cid,
                        "name": kdata.get("name") or "Kênh YouTube",
                        "handle": kdata.get("handle") or "",
                        "avatar": kdata.get("avatar") or "",
                        "subscribers": int(kdata.get("subscribers") or 0),
                        "videos": int(kdata.get("videos") or 0),
                        "is_active": cid == cur_id,
                    })

    if os.path.isfile(FILE_TOKEN) and os.path.isfile(FILE_KENH):
        legacy = _doc_json(FILE_KENH)
        cid = legacy.get("id")
        if cid and cid not in seen_ids:
            target_dir = os.path.join(CHANNELS_DIR, cid)
            os.makedirs(target_dir, exist_ok=True)
            import shutil
            shutil.copy2(FILE_TOKEN, os.path.join(target_dir, "token.json"))
            shutil.copy2(FILE_KENH, os.path.join(target_dir, "kenh.json"))
            seen_ids.add(cid)
            ket_qua.insert(0, {
                "id": cid,
                "name": legacy.get("name") or "Kênh YouTube",
                "handle": legacy.get("handle") or "",
                "avatar": legacy.get("avatar") or "",
                "subscribers": int(legacy.get("subscribers") or 0),
                "videos": int(legacy.get("videos") or 0),
                "is_active": (cid == cur_id) or not cur_id,
            })

    if not any(k.get("is_active") for k in ket_qua) and ket_qua:
        ket_qua[0]["is_active"] = True
        set_active_channel(ket_qua[0]["id"])

    return ket_qua


def xoa_kenh(cid: str) -> bool:
    """Ngắt kết nối một kênh cụ thể."""
    if not cid:
        return False
    cdir = os.path.join(CHANNELS_DIR, cid)
    if os.path.isdir(cdir):
        import shutil
        shutil.rmtree(cdir, ignore_errors=True)
    con_lai = danh_sach_kenh()
    if con_lai:
        set_active_channel(con_lai[0]["id"])
    else:
        for f in (FILE_TOKEN, FILE_KENH, FILE_ACTIVE_CHANNEL):
            try:
                os.remove(f)
            except OSError:
                pass
    return True

# Bao lâu thì hỏi lại Google về kênh.
#
# Trước 21/09/2026 KHÔNG có hạn: nhớ một lần rồi dùng mãi. Kết quả là trang Kênh
# của mình hiện "0 người đăng ký, 1 video" suốt một tuần, vì đó đúng là số của
# ngày nối kênh. Tên kênh và ảnh đại diện thì gần như không đổi, nhưng số sub và
# số video đổi mỗi ngày, mà đó lại chính là thứ người ta mở trang này để xem.
KENH_SONG_GIAY = 15 * 60

# youtube.upload đủ để tải video lên. force-ssl thêm quyền đọc tên kênh, đặt ảnh
# bìa và tải phụ đề, ba thứ đều cần cho một lần đăng trọn vẹn.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    # Chỉ đọc. Cho thời gian xem, thời lượng xem trung bình và chỗ người xem bỏ
    # đi, ba thứ mà lượt xem trần không nói được.
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# Mặc định riêng tư để người dùng xem lại trước khi công khai. Chủ kênh xác nhận
# 04/10/2026: video hẹn giờ của Thanh Sử Quán TỰ công khai đúng giờ. Đừng viết
# hay nói rằng video đăng qua tool bị ép riêng tư: điều đó không đúng với kênh này.
RIENG_TU = "private"
CHE_DO = [
    {"id": "private", "label": "Riêng tư, tự bấm công khai sau"},
    {"id": "unlisted", "label": "Không công khai, ai có link thì xem được"},
    {"id": "public", "label": "Công khai ngay"},
]

# Giờ lấy từ kênh mẫu: 12 trên 14 video gần nhất của họ đăng trong khung chín
# giờ sáng giờ Việt Nam, và video ăn nhất đăng đúng 9h30 sáng thứ tư.
GIO_DANG = 9
PHUT_DANG = 30
# Nhịp thì KHÔNG lấy của họ. Họ đăng hai ngày một video, còn chủ kênh này đăng
# mỗi ngày một video. Để 2 thì tool hẹn giờ giãn gấp đôi nhịp thật, đặt cả mẻ
# là lịch trôi mất mấy ngày so với ý người dùng.
CACH_NGAY = 1

# Danh mục 22 là People & Blogs, 27 là Education. Kênh kể chuyện lịch sử nằm ở
# Education thì YouTube gợi ý đúng tệp người xem hơn.
DANH_MUC = "27"

_KHOA = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# GIẤY TỜ
# ─────────────────────────────────────────────────────────────────────────────

FILE_GIAY_MAC_DINH = os.path.join(KHO, "giay_phep_mac_dinh.json")


def duong_giay_phep(ten: str = "") -> str:
    """
    Đường tới file client secret tải từ Google Cloud.

    Nhận cả tên gốc dài ngoằng của Google lẫn tên đã đổi cho gọn, để người dùng
    không phải nhớ đặt tên gì.
    """
    if ten:
        return next((g["duong"] for g in danh_sach_giay_phep() if g["file"] == os.path.basename(ten)), "")
    ro = os.path.join(BASE_DIR, "google_oauth.json")
    if os.path.isfile(ro):
        return ro
    found = sorted(glob.glob(os.path.join(BASE_DIR, "client_secret_*.json")))
    if len(found) <= 1:
        if found and not os.path.isfile(FILE_GIAY_MAC_DINH):
            _ghi_json(FILE_GIAY_MAC_DINH, {"file": os.path.basename(found[0])})
        return found[0] if found else ""
    # Có từ hai file trở lên: file mặc định là file đã ghim (file có sẵn từ trước), KHÔNG phải file xếp đầu theo tên.
    # Thả thêm file của một kênh mới vào thư mục không được làm các kênh cũ đổi sang dự án Google khác.
    ghim = os.path.join(BASE_DIR, os.path.basename(str(_doc_json(FILE_GIAY_MAC_DINH).get("file") or "")))
    if os.path.isfile(ghim) and ghim in found:
        return ghim
    da_dung = {_doc_json(t).get("client_id") for t in glob.glob(os.path.join(CHANNELS_DIR, "*", "token.json"))}
    return next((f for f in found if _ma_khach(f) in da_dung), found[0])


def _ma_khach(duong: str) -> str:
    d = _doc_json(duong)
    return str((d.get("installed") or d.get("web") or {}).get("client_id") or "")


def danh_sach_giay_phep() -> List[Dict]:
    """
    Mọi file giấy phép Google có trong thư mục tool. Mỗi kênh có thể đi qua một dự án Google riêng (chủ kênh dặn
    04/10/2026: dự án của Thanh Sử Quán chỉ dành cho Thanh Sử Quán, kênh mới phải có dự án riêng). Kênh nào dùng file
    nào thì suy ra từ mã khách ghi sẵn trong quyền đã lưu của kênh đó, không cần sổ riêng.
    """
    mac_dinh = duong_giay_phep()
    ten_kenh: Dict[str, List[str]] = {}
    for t in glob.glob(os.path.join(CHANNELS_DIR, "*", "token.json")):
        ma = _doc_json(t).get("client_id") or ""
        ten_kenh.setdefault(ma, []).append(_doc_json(os.path.join(os.path.dirname(t), "kenh.json")).get("name") or "Kênh YouTube")
    ra = []
    for f in sorted(set(glob.glob(os.path.join(BASE_DIR, "client_secret_*.json")) + glob.glob(os.path.join(BASE_DIR, "google_oauth*.json")))):
        d = _doc_json(f)
        goc = d.get("installed") or d.get("web") or {}
        if not goc.get("client_id"):
            continue
        ra.append({"file": os.path.basename(f), "duong": f, "du_an": str(goc.get("project_id") or ""),
                   "mac_dinh": f == mac_dinh, "kenh": ten_kenh.get(goc["client_id"], [])})
    return ra


def da_co_giay_phep() -> bool:
    return bool(duong_giay_phep())


def _doc_json(path: str) -> Dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _ghi_json(path: str, data: Dict) -> None:
    """Ghi ra file tạm rồi đổi tên, tắt máy giữa chừng không làm hỏng bản cũ."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tam = path + ".tmp"
    with open(tam, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tam, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# CẤP QUYỀN
# ─────────────────────────────────────────────────────────────────────────────

def _nap_quyen(cid: Optional[str] = None):
    """Đọc mã đã lưu, tự làm mới nếu hết hạn. Chưa cấp quyền thì trả về None."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    try:
        import datetime
        import google.auth._helpers as _auth_helpers
        _auth_helpers.REFRESH_THRESHOLD = datetime.timedelta(seconds=10)
    except Exception:
        pass

    token_path = duong_token(cid)
    if not os.path.isfile(token_path):
        return None
    try:
        quyen = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:  # noqa: BLE001
        return None
    if quyen and quyen.expired and quyen.refresh_token:
        try:
            quyen.refresh(Request())
            _ghi_json(token_path, json.loads(quyen.to_json()))
            if token_path != FILE_TOKEN and (not cid or cid == active_channel_id()):
                _ghi_json(FILE_TOKEN, json.loads(quyen.to_json()))
        except Exception as exc:  # noqa: BLE001
            print(f"[⚠️ YouTube] Không làm mới được quyền: {exc}")
            return None
    return quyen if quyen and quyen.valid else None


def da_cap_quyen() -> bool:
    return _nap_quyen() is not None


# Trình duyệt mở màn hình cấp quyền. Mặc định máy Mac mở Safari, mà tài khoản
# Google người dùng đang đăng nhập thường nằm ở Chrome, nên mở Safari là phải gõ
# lại mật khẩu và làm lại xác thực hai bước.
TRINH_DUYET = [
    {"id": "chrome", "label": "Chrome"},
    {"id": "safari", "label": "Safari"},
    {"id": "firefox", "label": "Firefox"},
    {"id": "", "label": "Trình duyệt mặc định của máy"},
]
TRINH_DUYET_MAC_DINH = "chrome"


def _mo_bang(ten: str) -> str:
    """
    Bắt màn hình cấp quyền mở bằng đúng trình duyệt được chọn.

    Đặt trình duyệt đó lên đầu danh sách ưu tiên của Python, vì hàm cấp quyền
    của Google gọi thẳng `webbrowser.open` chứ không cho truyền trình duyệt vào.
    Máy không có trình duyệt đó thì im lặng dùng mặc định, chứ không chặn cả
    lượt cấp quyền chỉ vì thiếu một cái trình duyệt.
    """
    import webbrowser

    ten = (ten or "").strip().lower()
    if not ten:
        return ""
    try:
        bo = webbrowser.get(ten)
    except webbrowser.Error:
        print(f"[⚠️ YouTube] Máy không có {ten}, mở bằng trình duyệt mặc định.")
        return ""
    try:
        webbrowser.register(ten, None, bo, preferred=True)
    except TypeError:  # Python cũ không nhận preferred
        return ""
    return ten


def cap_quyen(trinh_duyet: str = TRINH_DUYET_MAC_DINH, add_new: bool = False, giay_phep: str = "") -> Dict:
    """
    Mở trình duyệt để bạn chọn kênh và đồng ý, rồi giữ mã lại.
    Hỗ trợ cả nối lần đầu lẫn nối thêm kênh thứ 2, thứ 3...
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    giay = duong_giay_phep(giay_phep)
    if not giay:
        return {"success": False,
                "error": "Chưa thấy file giấy phép của Google. Tải file client secret "
                         "từ Google Cloud về thư mục tool, đặt tên google_oauth.json."}
    dung = _mo_bang(trinh_duyet)
    try:
        flow = InstalledAppFlow.from_client_secrets_file(giay, SCOPES)
        quyen = flow.run_local_server(
            port=0, prompt="select_account consent",
            authorization_prompt_message=(
                f"Đang mở {dung or 'trình duyệt'} để bạn chọn kênh..."),
            success_message="Xong. Quay lại tool được rồi.")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": _doc_loi(exc)}

    # Đọc thông tin kênh vừa được cấp quyền
    try:
        from googleapiclient.discovery import build
        dv = build("youtube", "v3", credentials=quyen, cache_discovery=False)
        res = dv.channels().list(part="snippet,statistics", mine=True).execute()
        items = res.get("items") or []
        if not items:
            return {"success": False, "error": "Tài khoản Google này chưa có kênh YouTube nào."}
        m = items[0]
        cid = m.get("id") or f"channel_{int(time.time())}"
        snip = m.get("snippet") or {}
        tk = m.get("statistics") or {}
        kenh_info = {
            "id": cid,
            "name": snip.get("title", ""),
            "handle": snip.get("customUrl", ""),
            "avatar": ((snip.get("thumbnails") or {}).get("default") or {}).get("url", ""),
            "subscribers": int(tk.get("subscriberCount") or 0),
            "videos": int(tk.get("videoCount") or 0),
            "doc_luc": time.time(),
        }
        # Lưu vào thư mục channels/<cid>/
        cdir = os.path.join(CHANNELS_DIR, cid)
        os.makedirs(cdir, exist_ok=True)
        _ghi_json(os.path.join(cdir, "token.json"), json.loads(quyen.to_json()))
        _ghi_json(os.path.join(cdir, "kenh.json"), kenh_info)
        set_active_channel(cid)
        return {"success": True, **kenh_info, "channels": danh_sach_kenh()}
    except Exception as exc:  # noqa: BLE001
        _ghi_json(FILE_TOKEN, json.loads(quyen.to_json()))
        return doc_kenh_dang_noi(lam_moi=True)


_OAUTH_SESSION: Dict[str, Any] = {}


def bat_dau_cap_quyen(giay_phep: str = "", giu_kenh_dang_chon: bool = False, port: int = 0) -> Dict:
    """
    Khởi động luồng OAuth trả về link cho trình duyệt tự mở hoặc copy.

    `giay_phep` là tên file giấy phép riêng của kênh sắp nối (bỏ trống thì dùng file mặc định như cũ).
    `giu_kenh_dang_chon` thì nối xong KHÔNG chuyển "kênh đang chọn" sang kênh mới, để các màn đang làm việc với kênh cũ
    không bị đổi kênh sau lưng người dùng.
    """
    global _OAUTH_SESSION
    if _OAUTH_SESSION.get("server"):
        try:
            _OAUTH_SESSION["server"].server_close()
        except Exception:
            pass
    giay = duong_giay_phep(giay_phep)
    if not giay:
        return {"success": False, "error": "Chưa thấy file giấy phép của Google." if not giay_phep
                else f"Không thấy file giấy phép {os.path.basename(giay_phep)} trong thư mục tool."}
    from google_auth_oauthlib.flow import InstalledAppFlow, _RedirectWSGIApp, _WSGIRequestHandler
    import wsgiref.simple_server
    import threading

    wsgi_app = _RedirectWSGIApp("Đã cấp quyền thành công. Bạn có thể đóng tab này và quay lại Toolmevabe.")
    local_server = wsgiref.simple_server.make_server("localhost", port, wsgi_app, handler_class=_WSGIRequestHandler)
    flow = InstalledAppFlow.from_client_secrets_file(giay, SCOPES)
    flow.redirect_uri = f"http://localhost:{local_server.server_port}/"
    auth_url, _ = flow.authorization_url(prompt="select_account consent", access_type="offline")

    _OAUTH_SESSION = {
        "server": local_server,
        "flow": flow,
        "wsgi_app": wsgi_app,
        "auth_url": auth_url,
        "done": False,
        "result": None,
        "error": None,
        "started_at": time.time(),
    }

    def _worker():
        try:
            local_server.timeout = 2
            while not wsgi_app.last_request_uri and not _OAUTH_SESSION.get("cancelled"):
                local_server.handle_request()
                if time.time() - _OAUTH_SESSION.get("started_at", 0) > 1800:
                    break
            if not wsgi_app.last_request_uri:
                _OAUTH_SESSION["error"] = "Hết thời gian chờ cấp quyền."
                _OAUTH_SESSION["done"] = True
                return
            auth_response = wsgi_app.last_request_uri.replace("http", "https")
            flow.fetch_token(authorization_response=auth_response)
            quyen = flow.credentials
            from googleapiclient.discovery import build
            dv = build("youtube", "v3", credentials=quyen, cache_discovery=False)
            res = dv.channels().list(part="snippet,statistics", mine=True).execute()
            items = res.get("items") or []
            if not items:
                _OAUTH_SESSION["error"] = "Tài khoản Google này chưa có kênh YouTube nào."
                _OAUTH_SESSION["done"] = True
                return
            m = items[0]
            cid = m.get("id") or f"channel_{int(time.time())}"
            snip = m.get("snippet") or {}
            tk = m.get("statistics") or {}
            kenh_info = {
                "id": cid,
                "name": snip.get("title", ""),
                "handle": snip.get("customUrl", ""),
                "avatar": ((snip.get("thumbnails") or {}).get("default") or {}).get("url", ""),
                "subscribers": int(tk.get("subscriberCount") or 0),
                "videos": int(tk.get("videoCount") or 0),
                "doc_luc": time.time(),
            }
            cdir = os.path.join(CHANNELS_DIR, cid)
            os.makedirs(cdir, exist_ok=True)
            _ghi_json(os.path.join(cdir, "token.json"), json.loads(quyen.to_json()))
            _ghi_json(os.path.join(cdir, "kenh.json"), kenh_info)
            if not (giu_kenh_dang_chon and active_channel_id()):
                set_active_channel(cid)
            _OAUTH_SESSION["result"] = {"success": True, **kenh_info, "channels": danh_sach_kenh()}
            _OAUTH_SESSION["done"] = True
        except Exception as exc:
            _OAUTH_SESSION["error"] = _doc_loi(exc)
            _OAUTH_SESSION["done"] = True
        finally:
            try:
                local_server.server_close()
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    _OAUTH_SESSION["thread"] = t
    return {"success": True, "auth_url": auth_url}


def kiem_tra_cap_quyen() -> Dict:
    """Kiểm tra tiến trình cấp quyền qua link/tab."""
    global _OAUTH_SESSION
    if not _OAUTH_SESSION.get("server") and not _OAUTH_SESSION.get("done"):
        return {"active": False, "done": False}
    if _OAUTH_SESSION.get("done"):
        if _OAUTH_SESSION.get("error"):
            err = _OAUTH_SESSION["error"]
            _OAUTH_SESSION = {}
            return {"active": False, "done": True, "success": False, "error": err}
        res = _OAUTH_SESSION.get("result") or {"success": True}
        _OAUTH_SESSION = {}
        return {"active": False, "done": True, "success": True, **res}
    return {"active": True, "done": False, "auth_url": _OAUTH_SESSION.get("auth_url")}


def huy_cap_quyen() -> Dict:
    """Huỷ tiến trình cấp quyền đang chờ."""
    global _OAUTH_SESSION
    if _OAUTH_SESSION.get("server"):
        try:
            _OAUTH_SESSION["server"].server_close()
        except Exception:
            pass
    _OAUTH_SESSION = {}
    return {"success": True}


def bo_quyen(cid: Optional[str] = None) -> Dict:
    """Xoá mã đã lưu, để nối lại từ đầu khi nối nhầm kênh."""
    cid = cid or active_channel_id()
    if cid:
        xoa_kenh(cid)
    for f in (FILE_TOKEN, FILE_KENH):
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        os.remove(FILE_ACTIVE_CHANNEL)
    except OSError:
        pass
    return {"success": True}


def _dich_vu(cid: Optional[str] = None):
    from googleapiclient.discovery import build
    quyen = _nap_quyen(cid)
    if not quyen:
        return None
    return build("youtube", "v3", credentials=quyen, cache_discovery=False)


# ─────────────────────────────────────────────────────────────────────────────
# KÊNH ĐANG NỐI
# ─────────────────────────────────────────────────────────────────────────────

def doc_kenh_dang_noi(lam_moi: bool = False) -> Dict:
    """
    Hỏi Google xem mã đang giữ thuộc kênh nào.

    Một Gmail quản được nhiều kênh, và bước chọn kênh nằm bên trong màn hình cấp
    quyền của Google. Không hỏi lại thì không cách nào biết mình vừa nối vào cái
    nào, tới lúc video lên nhầm kênh mới biết.
    """
    cid = active_channel_id()
    k_path = duong_kenh(cid)
    cu = _doc_json(k_path)
    if not cu.get("id"):
        cu = _doc_json(FILE_KENH)
    if not lam_moi and cu.get("id") and \
            time.time() - (cu.get("doc_luc") or 0) < KENH_SONG_GIAY:
        return {"success": True, **cu}
    dv = _dich_vu()
    if dv is None:
        # Hỏi lại không được thì trả bản cũ còn hơn không trả gì: tên kênh và
        # ảnh đại diện vẫn đúng, chỉ số sub là cũ.
        return {"success": True, **cu} if cu.get("id") else \
            {"success": False, "error": "Chưa cấp quyền cho tool."}
    try:
        res = dv.channels().list(part="snippet,statistics", mine=True).execute()
    except Exception as exc:  # noqa: BLE001
        if cu.get("id"):
            return {"success": True, **cu}
        return {"success": False, "error": _doc_loi(exc)}
    muc = (res.get("items") or [])
    if not muc and cu.get("id"):
        return {"success": True, **cu}
    if not muc:
        return {"success": False,
                "error": "Tài khoản này chưa có kênh YouTube nào. Tạo kênh rồi cấp quyền lại."}
    m = muc[0]
    snip = m.get("snippet") or {}
    tk = m.get("statistics") or {}
    kenh = {
        "id": m.get("id", ""),
        "name": snip.get("title", ""),
        "handle": snip.get("customUrl", ""),
        "avatar": ((snip.get("thumbnails") or {}).get("default") or {}).get("url", ""),
        "subscribers": int(tk.get("subscriberCount") or 0),
        "videos": int(tk.get("videoCount") or 0),
        "doc_luc": time.time(),
    }
    _ghi_json(k_path, kenh)
    if k_path != FILE_KENH:
        _ghi_json(FILE_KENH, kenh)
    return {"success": True, **kenh}


# ─────────────────────────────────────────────────────────────────────────────
# GIỜ ĐĂNG
# ─────────────────────────────────────────────────────────────────────────────

def gio_dang_ke_tiep(sau_lan_nao: float = 0.0) -> str:
    """
    Mốc giờ đăng tiếp theo, trả về đúng dạng YouTube nhận.

    Đo 14 video gần nhất của kênh mẫu ngày 15/9: họ đăng hai ngày một video, 12
    trên 14 video rơi vào khung chín giờ sáng giờ Việt Nam, và video ăn nhất của
    họ đăng 9h30 sáng. Nên mặc định đi theo đúng nhịp đó.

    `sau_lan_nao` là mốc giờ của video đăng ngay trước, để cả mẻ giãn đều hai
    ngày một cái chứ không dồn hết vào một buổi sáng.
    """
    from datetime import datetime, timedelta, timezone

    vn = timezone(timedelta(hours=7))
    bay_gio = datetime.now(vn)
    if sau_lan_nao:
        moc = datetime.fromtimestamp(sau_lan_nao, vn) + timedelta(days=CACH_NGAY)
    else:
        moc = bay_gio
    moc = moc.replace(hour=GIO_DANG, minute=PHUT_DANG, second=0, microsecond=0)
    # Giờ đã trôi qua thì đẩy sang ngày mai: YouTube từ chối lịch đặt trong quá khứ.
    while moc <= bay_gio + timedelta(minutes=5):
        moc += timedelta(days=1)
    return moc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# ĐĂNG
# ─────────────────────────────────────────────────────────────────────────────

def _doc_loi(exc: Exception) -> str:
    """Đổi lỗi của Google sang câu người dùng đọc hiểu và biết phải làm gì."""
    chu = str(exc)
    if "access_denied" in chu or "has not completed" in chu:
        return ("Google chặn vì Gmail này chưa nằm trong danh sách người dùng thử. "
                "Vào Google Auth Platform, mục Audience, thêm đúng Gmail sở hữu kênh "
                "vào Test users rồi cấp quyền lại.")
    if "quotaExceeded" in chu or "uploadLimitExceeded" in chu:
        return ("Hết hạn mức YouTube hôm nay. Hạn mức đặt lại lúc 2 giờ chiều giờ Việt Nam. "
                "Mỗi ngày đăng được khoảng sáu video.")
    if "has not been used in project" in chu or "is disabled" in chu:
        return ("Dự án Google chưa bật YouTube Analytics API. Vào Google Cloud Console, "
                "mục Enabled APIs, bật YouTube Analytics API rồi chờ một phút cho nó ngấm.")
    if "youtubeSignupRequired" in chu:
        return "Tài khoản Google này chưa có kênh YouTube. Tạo kênh rồi cấp quyền lại."
    if "forbidden" in chu.lower() or "insufficientPermissions" in chu:
        return ("Quyền đang giữ không đủ. Bấm Đổi kênh để cấp quyền lại, "
                "nhớ tick cả hai ô Google hỏi.")
    if "invalid_grant" in chu:
        return "Quyền đã bị thu hồi hoặc hết hạn. Bấm Đổi kênh để cấp lại."
    return chu[:300]


TRAN_ANH_BIA = 2 * 1024 * 1024  # YouTube từ chối ảnh bìa nặng hơn 2 MB


def anh_bia_vua_co(duong: str) -> str:
    """
    Ảnh bìa nhẹ dưới 2 MB để YouTube nhận.

    Ảnh bìa vẽ ra là PNG 1672x941, thường 2,2 MB, YouTube trả "Media larger
    than: 2097152" (11/11/2026, video mộ Tào Tháo lên kênh mà không có ảnh bìa).
    Nặng quá thì lưu thêm một bản JPEG cạnh file gốc, hạ dần chất lượng rồi tới
    cỡ ảnh cho tới khi vừa; file gốc giữ nguyên.
    """
    if not duong or os.path.getsize(duong) <= TRAN_ANH_BIA:
        return duong
    from PIL import Image
    anh = Image.open(duong).convert("RGB")
    ra = os.path.splitext(duong)[0] + "_yt.jpg"
    for co in (1280, 1280, 1280, 1024):
        if anh.width > co:
            anh = anh.resize((co, round(anh.height * co / anh.width)), Image.LANCZOS)
        for q in (92, 85, 75, 65):
            anh.save(ra, "JPEG", quality=q, optimize=True)
            if os.path.getsize(ra) <= TRAN_ANH_BIA:
                return ra
    return ra


def _tep(duong: str, ten: str) -> Optional[str]:
    """Đường tới file, kèm câu báo nếu không có."""
    if duong and os.path.isfile(duong) and os.path.getsize(duong) > 0:
        return None
    return f"Không thấy {ten} trên máy: {os.path.basename(duong or '')}"


def dang_video(video_path: str, title: str, description: str = "",
               tags: Optional[List[str]] = None,
               che_do: str = RIENG_TU, dang_luc: str = "",
               thumb_path: str = "", srt_path: str = "",
               ngon_ngu: str = "vi",
               bao: Optional[Callable[[int, str], None]] = None,
               channel_id: Optional[str] = None, danh_muc: str = "") -> Dict:
    """
    Tải một video lên kênh đang nối, kèm ảnh bìa và phụ đề.

    `danh_muc` để trống thì dùng danh mục chung (Giáo dục, hợp kênh lịch sử). Xưởng làm phim khác thể loại tự
    truyền danh mục của mình, không đổi hằng số chung.

    Đặt lịch thì chế độ phải là riêng tư, đó là luật của YouTube chứ không phải
    lựa chọn: video hẹn giờ nằm riêng tư cho tới đúng giờ rồi tự công khai.

    Ảnh bìa và phụ đề hỏng thì không đánh hỏng cả lần đăng, vì video đã nằm trên
    YouTube rồi, chỉ ghi lại để người dùng tự bù sau.
    """
    from googleapiclient.http import MediaFileUpload

    def buoc(pt: int, chu: str) -> None:
        if bao:
            bao(pt, chu)

    loi = _tep(video_path, "file video")
    if loi:
        return {"success": False, "error": loi}
    if not title.strip():
        return {"success": False, "error": "Chưa có tiêu đề cho video."}

    cid = channel_id or active_channel_id()
    dv = _dich_vu(cid)
    if dv is None:
        return {"success": False, "error": "Chưa cấp quyền cho tool. Bấm Nối kênh YouTube."}

    trang_thai: Dict = {"selfDeclaredMadeForKids": False}
    if dang_luc:
        # Hẹn giờ thì bắt buộc riêng tư cho tới đúng giờ, YouTube không nhận kiểu khác.
        trang_thai["privacyStatus"] = RIENG_TU
        trang_thai["publishAt"] = dang_luc
    else:
        trang_thai["privacyStatus"] = che_do if che_do in {c["id"] for c in CHE_DO} else RIENG_TU

    than = {
        "snippet": {
            "title": title.strip()[:100],
            "description": (description or "").strip()[:5000],
            "tags": [t.strip() for t in (tags or []) if t.strip()][:15],
            "categoryId": str(danh_muc or DANH_MUC),
            "defaultLanguage": ngon_ngu,
            "defaultAudioLanguage": ngon_ngu,
        },
        "status": trang_thai,
    }

    buoc(3, "Đang mở kết nối tới YouTube")
    try:
        import socket
        socket.setdefaulttimeout(180)
        media = MediaFileUpload(video_path, chunksize=10 * 1024 * 1024, resumable=True,
                                mimetype="video/mp4")
        yeu_cau = dv.videos().insert(part="snippet,status", body=than, media_body=media)
        phan_hoi = None
        lan_thu = 0
        while phan_hoi is None:
            try:
                tinh_hinh, phan_hoi = yeu_cau.next_chunk()
                lan_thu = 0
                if tinh_hinh:
                    # Tải chiếm gần hết thời gian, nên ánh xạ nó vào khoảng 5 tới 85.
                    buoc(5 + int(80 * tinh_hinh.progress()), "Đang tải video lên YouTube")
            except Exception as e_chunk:
                lan_thu += 1
                if lan_thu > 6:
                    raise
                time.sleep(3 * lan_thu)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": _doc_loi(exc)}

    video_id = (phan_hoi or {}).get("id", "")
    if not video_id:
        return {"success": False, "error": "YouTube nhận video nhưng không trả về mã video."}
    ket_qua = {"success": True, "video_id": video_id,
               "url": f"https://www.youtube.com/watch?v={video_id}",
               "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
               "privacy": trang_thai["privacyStatus"],
               "publish_at": dang_luc, "ghi_chu": []}

    if thumb_path and not _tep(thumb_path, "ảnh bìa"):
        buoc(88, "Đang đặt ảnh bìa")
        try:
            dv.thumbnails().set(videoId=video_id,
                                media_body=MediaFileUpload(anh_bia_vua_co(thumb_path))).execute()
            ket_qua["thumbnail"] = True
        except Exception as exc:  # noqa: BLE001
            ket_qua["ghi_chu"].append(f"Chưa đặt được ảnh bìa: {_doc_loi(exc)}")

    if srt_path and not _tep(srt_path, "phụ đề"):
        buoc(94, "Đang tải phụ đề")
        try:
            dv.captions().insert(
                part="snippet",
                body={"snippet": {"videoId": video_id, "language": ngon_ngu,
                                  "name": "Phụ đề", "isDraft": False}},
                media_body=MediaFileUpload(srt_path, mimetype="application/octet-stream"),
            ).execute()
            ket_qua["captions"] = True
        except Exception as exc:  # noqa: BLE001
            ket_qua["ghi_chu"].append(f"Chưa tải được phụ đề: {_doc_loi(exc)}")

    buoc(100, "Đã đăng lên YouTube")
    return ket_qua


def dang_worker(video_path: str, **kw) -> Callable[[Dict], None]:
    """Gói lần đăng thành một việc nền, để thanh tiến trình chạy như mọi bước khác."""
    import longform_studio as lf

    def worker(job: Dict) -> None:
        def bao(pt: int, chu: str) -> None:
            lf.update_job(job, pt, chu)

        lf.update_job(job, 1, "Đang chuẩn bị đăng")
        res = dang_video(video_path, bao=bao, **kw)
        if not res.get("success"):
            raise RuntimeError(res.get("error") or "Không đăng được lên YouTube.")
        job["result"] = res

    return worker


def trinh_duyet_co_tren_may() -> List[Dict]:
    """Chỉ liệt kê trình duyệt thật sự mở được, chọn vào là chắc chắn chạy."""
    import webbrowser

    ra = []
    for t in TRINH_DUYET:
        if not t["id"]:
            ra.append(t)
            continue
        try:
            webbrowser.get(t["id"])
            ra.append(t)
        except webbrowser.Error:
            continue
    return ra


def tinh_hinh(lam_moi: bool = False) -> Dict:
    """
    Một câu tóm tắt cho giao diện: có giấy phép chưa, nối kênh nào.

    `lam_moi` bỏ qua bộ nhớ đệm mười lăm phút. Cần nó vì nút Đo lại trong trang
    Kênh của mình trước đây chỉ làm mới số của từng video, còn số người đăng ký
    ở header thì đứng im tới mười lăm phút, nên bấm mãi không thấy đổi.
    """
    with _KHOA:
        channels = danh_sach_kenh()
        cur_id = active_channel_id()
        co_giay = da_co_giay_phep()
        if not co_giay:
            return {"success": True, "ready": False, "co_giay_phep": False,
                    "channels": channels, "active_channel_id": cur_id,
                    "message": "Chưa có file giấy phép Google trong thư mục tool."}
        if not da_cap_quyen():
            return {"success": True, "ready": False, "co_giay_phep": True,
                    "channels": channels, "active_channel_id": cur_id,
                    "message": "Đã có giấy phép, chưa nối kênh.",
                    "trinh_duyet": trinh_duyet_co_tren_may()}
        kenh = doc_kenh_dang_noi(lam_moi=lam_moi)
        if not kenh.get("success"):
            return {"success": True, "ready": False, "co_giay_phep": True,
                    "channels": channels, "active_channel_id": cur_id,
                    "message": kenh.get("error", "")}
        return {"success": True, "ready": True, "co_giay_phep": True,
                "channel": {k: kenh.get(k) for k in
                            ("id", "name", "handle", "avatar", "subscribers", "videos")},
                "channels": channels,
                "active_channel_id": cur_id,
                "che_do": CHE_DO, "gio_goi_y": gio_dang_ke_tiep(),
                "trinh_duyet": trinh_duyet_co_tren_may()}
