"""
social_scanner.py — Engine Đọc, Lọc & Thống Kê Trùng Comment Đa Kênh (Facebook, TikTok, YouTube, Excel).
Hỗ trợ:
- Quét comment từ Facebook Graph API (Post, Reel, Video, Permalink) với phân trang tự động và reply.
- Đọc danh sách bài viết / Reel gần đây của Page để chọn nhanh 1 chạm.
- Đọc và phân tích dữ liệu dán trực tiếp (TikTok / YouTube / CSV / Excel).
- Trích xuất tự động Số Điện Thoại Việt Nam, Email, Link.
- Thống kê trùng người bình luận (User Deduplication - phục vụ Minigame, Giveaway, lọc 1 comment/người).
- Thống kê trùng nội dung (Content Deduplication - phát hiện Seeding, Bot, Spammer).
- Thống kê từ khóa thịnh hành.
- Xuất báo cáo chuyên nghiệp ra file Excel (.xlsx).
"""

import os
import re
import json
import unicodedata
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from collections import Counter
import requests
import openpyxl
from bs4 import BeautifulSoup
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from facebook_publisher import GRAPH_API_BASE, load_facebook_config

# Regex nhận diện số điện thoại di động Việt Nam (các đầu số 03, 05, 07, 08, 09)
# Hỗ trợ cả định dạng: 0912345678, 0912.345.678, 0912 345 678, +84912345678
PHONE_REGEX = re.compile(
    r'(?:(?:\+84|84|0)[3|5|7|8|9])(?:[.\s-]?[0-9]){8}\b'
)

# Regex nhận diện email
EMAIL_REGEX = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'
)

# Regex nhận diện URL / liên kết web
URL_REGEX = re.compile(
    r'(?:https?:\/\/|www\.)[^\s]+'
)


def normalize_phone_number(raw_phone: str) -> str:
    """Chuẩn hóa số điện thoại về dạng 10 chữ số 0xxxxxxxxx."""
    digits = re.sub(r'\D', '', raw_phone)
    if digits.startswith('84') and len(digits) == 11:
        digits = '0' + digits[2:]
    return digits


def strip_vietnamese_accents(text: str) -> str:
    """
    Chuyển đổi văn bản tiếng Việt có dấu sang không dấu:
    Ví dụ: 'Hôm nay trời đẹp!' -> 'hom nay troi dep'
    Hỗ trợ xử lý chữ đ/Đ và các dấu tổ hợp Unicode (NFD/NFKD).
    """
    if not text:
        return ""
    text = text.replace('đ', 'd').replace('Đ', 'd')
    nfkd = unicodedata.normalize('NFKD', text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_comment_content(msg: str) -> str:
    """
    Chuẩn hoá nội dung comment để so khớp trùng lặp:
    1. Bỏ dấu tiếng Việt ('hôm nay trời đẹp' -> 'hom nay troi dep')
    2. Chuyển về chữ thường
    3. Loại bỏ ký tự đặc biệt, dấu câu, emoji, icon
    4. Gom khoảng trắng thừa
    """
    if not msg:
        return ""
    unaccented = strip_vietnamese_accents(msg.lower())
    clean = re.sub(r'[^\w\s]', ' ', unaccented)
    return re.sub(r'\s+', ' ', clean).strip()


def extract_facebook_object_id(url_or_id: str) -> str:
    """
    Trích xuất Post ID, Video ID hoặc Reel ID từ nhiều định dạng liên kết Facebook khác nhau.
    Ví dụ:
    - https://www.facebook.com/1382486814943877/posts/122191617794848796
    - https://www.facebook.com/reel/102030405060
    - https://www.facebook.com/watch/?v=102030405060
    - https://www.facebook.com/permalink.php?story_fbid=122191617794848796&id=1382486814943877
    - 1382486814943877_122191617794848796 (raw ID)
    """
    raw = str(url_or_id).strip()
    if not raw:
        return ""

    # Nếu là ID thuần (chỉ chứa số hoặc có dấu gạch dưới như pageid_postid)
    if re.match(r'^\d+(_\d+)?$', raw):
        return raw

    # Xóa tham số tracking rác nếu có (?mibextid=... &rdid=...)
    parsed = urllib.parse.urlparse(raw)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)

    # 1. Định dạng query params: permalink.php?story_fbid=X&id=Y
    if "story_fbid" in query:
        fbid = query["story_fbid"][0]
        page_id = query.get("id", [""])[0]
        return f"{page_id}_{fbid}" if page_id else fbid

    # 2. Định dạng query params: watch/?v=X
    if "v" in query:
        return query["v"][0]

    # 3. Định dạng đường dẫn path
    # /reel/123456789
    reel_match = re.search(r'/reel/(\d+)', path)
    if reel_match:
        return reel_match.group(1)

    # /videos/123456789
    video_match = re.search(r'/videos/(\d+)', path)
    if video_match:
        return video_match.group(1)

    # /posts/123456789 hoặc /posts/pfbid...
    post_match = re.search(r'/posts/([^/?]+)', path)
    if post_match:
        return post_match.group(1)

    # /p/123456789
    p_match = re.search(r'/p/(\d+)', path)
    if p_match:
        return p_match.group(1)

    # Trích cụm số dài cuối cùng trong URL nếu các regex trên không khớp
    digits_match = re.findall(r'\d{8,}', raw)
    if digits_match:
        return digits_match[-1]

    return raw


def fetch_page_recent_posts(page_id: str, access_token: Optional[str] = None, limit: int = 25) -> List[Dict]:
    """
    Lấy danh sách các bài viết / Reel / Video gần nhất của Fanpage
    để người dùng có thể chọn nhanh 1 chạm thay vì phải tự copy link.
    """
    cfg = load_facebook_config()
    token = access_token
    if not token:
        page = next((p for p in cfg.get("pages", []) if str(p.get("id")) == str(page_id)), None)
        if page:
            token = page.get("access_token", "")
        elif cfg.get("pages"):
            token = cfg["pages"][0].get("access_token", "")

    if not token:
        raise ValueError("Không tìm thấy Page Access Token để truy vấn danh sách bài viết!")

    results = []

    # 1. Truy vấn Feed của Page
    try:
        url = f"{GRAPH_API_BASE}/{page_id}/feed"
        params = {
            "fields": "id,message,created_time,permalink_url,full_picture,shares,comments.summary(true),likes.summary(true)",
            "limit": min(limit, 50),
            "access_token": token
        }
        res = requests.get(url, params=params, timeout=15).json()
        for item in res.get("data", []):
            msg = item.get("message", "")
            title = msg.split("\n")[0][:80] if msg else "Bài viết không có tiêu đề"
            comments_count = item.get("comments", {}).get("summary", {}).get("total_count", 0)
            likes_count = item.get("likes", {}).get("summary", {}).get("total_count", 0)
            results.append({
                "id": item.get("id"),
                "type": "post",
                "title": title,
                "message": msg,
                "created_time": item.get("created_time"),
                "picture": item.get("full_picture", ""),
                "permalink_url": item.get("permalink_url", ""),
                "comments_count": comments_count,
                "likes_count": likes_count
            })
    except Exception as e:
        print(f"[⚠️ Fetch feed error for page {page_id}]: {e}")

    # 2. Truy vấn Video Reels của Page để không bị thiếu video
    try:
        url_v = f"{GRAPH_API_BASE}/{page_id}/videos"
        params_v = {
            "fields": "id,title,description,created_time,permalink_url,thumbnails{uri,is_preferred},comments.summary(true),likes.summary(true)",
            "limit": min(limit, 30),
            "access_token": token
        }
        res_v = requests.get(url_v, params=params_v, timeout=15).json()
        existing_ids = {r["id"] for r in results}
        for item in res_v.get("data", []):
            v_id = item.get("id")
            if v_id in existing_ids:
                continue
            desc = item.get("description", "") or item.get("title", "")
            title = item.get("title") or (desc.split("\n")[0][:80] if desc else "Video Reel")
            thumb = ""
            for t in item.get("thumbnails", {}).get("data", []):
                if t.get("is_preferred"):
                    thumb = t.get("uri")
                    break
            if not thumb and item.get("thumbnails", {}).get("data"):
                thumb = item["thumbnails"]["data"][0].get("uri")

            comments_count = item.get("comments", {}).get("summary", {}).get("total_count", 0)
            likes_count = item.get("likes", {}).get("summary", {}).get("total_count", 0)
            results.append({
                "id": v_id,
                "type": "video",
                "title": f"🎬 {title}",
                "message": desc,
                "created_time": item.get("created_time"),
                "picture": thumb,
                "permalink_url": item.get("permalink_url", f"https://www.facebook.com/{v_id}"),
                "comments_count": comments_count,
                "likes_count": likes_count
            })
    except Exception as e:
        print(f"[⚠️ Fetch videos error for page {page_id}]: {e}")

    # Sắp xếp bài viết mới nhất lên đầu
    results.sort(key=lambda x: x.get("created_time") or "", reverse=True)
    return results[:limit]


def fetch_facebook_comments(
    page_id: str,
    post_id_or_url: str,
    access_token: Optional[str] = None,
    max_comments: int = 1000
) -> Dict[str, Any]:
    """
    Quét toàn bộ comment của bài viết/video Facebook qua Graph API.
    Hỗ trợ cả top-level comment và reply comments với phân trang tự động.
    """
    cfg = load_facebook_config()
    token = access_token
    if not token:
        page = next((p for p in cfg.get("pages", []) if str(p.get("id")) == str(page_id)), None)
        if page:
            token = page.get("access_token", "")
        elif cfg.get("pages"):
            token = cfg["pages"][0].get("access_token", "")

    if not token:
        raise ValueError("Chưa cung cấp Access Token Facebook Fanpage!")

    object_id = extract_facebook_object_id(post_id_or_url)
    if not object_id:
        raise ValueError("Không thể nhận diện Post ID từ đường dẫn đã nhập!")

    query_id = object_id

    all_comments: List[Dict] = []
    seen_ids = set()

    url = f"{GRAPH_API_BASE}/{query_id}/comments"
    params = {
        "fields": "id,from{id,name,picture},message,created_time,like_count,comment_count,parent,attachment",
        "filter": "stream",  # Lấy toàn bộ cả comment chính và reply
        "limit": 100,
        "summary": "true",
        "access_token": token
    }

    total_summary_count = 0
    next_url = url

    while next_url and len(all_comments) < max_comments:
        try:
            if next_url == url:
                resp = requests.get(next_url, params=params, timeout=20)
            else:
                resp = requests.get(next_url, timeout=20)

            if resp.status_code != 200:
                err_data = resp.json().get("error", {})
                err_msg = err_data.get("message", "Lỗi Graph API không xác định")
                # Thử fallback ghép {page_id}_{object_id} nếu chưa thử
                if "_" not in query_id and page_id:
                    alt_id = f"{page_id}_{object_id}"
                    alt_url = f"{GRAPH_API_BASE}/{alt_id}/comments"
                    resp2 = requests.get(alt_url, params=params, timeout=20)
                    if resp2.status_code == 200:
                        resp = resp2
                    else:
                        raise ValueError(f"Facebook Graph API Error: {err_msg} (ID: {query_id})")
                else:
                    raise ValueError(f"Facebook Graph API Error: {err_msg} (ID: {query_id})")

            data = resp.json()
            if "summary" in data:
                total_summary_count = data["summary"].get("total_count", 0)

            items = data.get("data", [])
            if not items:
                break

            for c in items:
                c_id = c.get("id")
                if not c_id or c_id in seen_ids:
                    continue
                seen_ids.add(c_id)

                user = c.get("from") or {}
                user_avatar = ""
                if isinstance(user.get("picture"), dict):
                    user_avatar = user.get("picture", {}).get("data", {}).get("url", "")

                all_comments.append({
                    "id": c_id,
                    "user_id": user.get("id", ""),
                    "user_name": user.get("name") or "Người dùng Facebook",
                    "user_avatar": user_avatar,
                    "message": (c.get("message") or "").strip(),
                    "created_time": c.get("created_time", ""),
                    "like_count": c.get("like_count", 0),
                    "reply_count": c.get("comment_count", 0),
                    "parent_id": c.get("parent", {}).get("id") if c.get("parent") else None,
                    "has_attachment": bool(c.get("attachment")),
                    "post_id": query_id,
                    "source": "facebook"
                })

                if len(all_comments) >= max_comments:
                    break

            next_url = data.get("paging", {}).get("next")
        except Exception as ex:
            print(f"[⚠️ Fetch comments error]: {ex}")
            if all_comments:
                break
            else:
                raise ex

    return {
        "object_id": query_id,
        "total_fetched": len(all_comments),
        "total_on_post": total_summary_count or len(all_comments),
        "comments": all_comments
    }


def parse_facebook_html_comments(html_str: str) -> List[Dict]:
    """
    Trích xuất danh sách comment trực tiếp từ mã HTML copy từ Facebook Web (Inspect Element F12 hoặc View Source).
    Bóc tách chính xác:
    - Tên người dùng thật (thay vì 'Người dùng Facebook' từ Graph API)
    - Link profile cá nhân Facebook (user_link)
    - Facebook User ID / Username (user_id)
    - Ảnh đại diện thật (user_avatar)
    - Nội dung comment (message)
    """
    if not BeautifulSoup:
        return []

    soup = BeautifulSoup(html_str, 'html.parser')
    parsed = []

    # 1. Tìm các block comment qua role="article"
    articles = soup.find_all(attrs={'role': 'article'})
    if not articles:
        # Fallback: tìm theo aria-label chứa "Bình luận của" hoặc "Comment by"
        articles = soup.find_all(lambda el: el.has_attr('aria-label') and any(kw in el['aria-label'] for kw in ['Bình luận của', 'Comment by', 'commented']))

    for idx, art in enumerate(articles):
        # A. Tìm link profile & Tên người dùng
        link_el = art.find('a', attrs={'role': 'link'})
        if not link_el:
            link_el = art.find('a', href=re.compile(r'facebook\.com|profile\.php|\/user\/'))
        
        user_name = "Khách"
        user_link = ""
        user_id = ""

        if link_el:
            user_name = link_el.get_text(strip=True)
            raw_href = link_el.get('href', '')
            if raw_href:
                if raw_href.startswith('/'):
                    user_link = f"https://www.facebook.com{raw_href}"
                else:
                    user_link = raw_href
                # Trích xuất user_id từ link
                if 'id=' in user_link:
                    m = re.search(r'id=(\d+)', user_link)
                    if m: user_id = m.group(1)
                else:
                    m = re.search(r'facebook\.com/([^/?#]+)', user_link)
                    if m: user_id = m.group(1)

        # Fallback: nếu không có link_el nhưng có aria-label="Bình luận của Nguyễn Văn A"
        if (not user_name or user_name == "Khách") and art.has_attr('aria-label'):
            label = art['aria-label']
            m = re.search(r'(?:Bình luận của|Comment by)\s+([^,]+)', label)
            if m:
                user_name = m.group(1).strip()

        if not user_id:
            user_id = re.sub(r'[^a-zA-Z0-9]', '', user_name).lower() or f"user_{idx+1}"

        # B. Avatar
        img_el = art.find('img') or art.find('image')
        user_avatar = ""
        if img_el:
            user_avatar = img_el.get('src') or img_el.get('xlink:href', '')

        # C. Nội dung bình luận
        dir_els = art.find_all(attrs={'dir': 'auto'})
        message_parts = []
        for d in dir_els:
            txt = d.get_text(strip=True)
            if txt and txt != user_name and txt not in ['Thích', 'Phản hồi', 'Chia sẻ', 'Like', 'Reply', 'Share', 'Xem thêm']:
                message_parts.append(txt)
        
        message = " ".join(message_parts).strip()
        if not message:
            full_txt = art.get_text(separator=' ', strip=True)
            if user_name in full_txt:
                full_txt = full_txt.replace(user_name, '', 1).strip()
            full_txt = re.sub(r'\b(Thích|Phản hồi|Like|Reply|Share|\d+\s*(?:giờ|ngày|phút|tuần|h|d|m|w))\b', '', full_txt).strip()
            message = full_txt

        if message:
            parsed.append({
                "id": f"html_cmt_{idx+1}",
                "user_id": user_id,
                "user_name": user_name,
                "user_avatar": user_avatar,
                "user_link": user_link,
                "message": message,
                "created_time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "like_count": 0,
                "reply_count": 0,
                "source": "facebook_html_scrape"
            })

    return parsed


def parse_raw_text_comments(raw_text: str) -> List[Dict]:
    """
    Đọc và chuẩn hóa dữ liệu comment được dán trực tiếp từ người dùng.
    Hỗ trợ đa định dạng:
    1. Mã HTML copy từ Facebook Web (Inspect Element / F12) -> bóc tách chính xác Tên thật, Link profile & ID!
    2. Văn bản bôi đen copy từ giao diện Facebook (Tên, Nội dung, Thích Phản hồi).
    3. User: Nội dung
    4. User \t Nội dung \t Thời gian (Excel/Google Sheets)
    5. Từng dòng nội dung đơn thuần
    """
    if not raw_text or not raw_text.strip():
        return []

    # 1. Thử nhận diện & cào trực tiếp từ mã HTML (nếu dán mã nguồn Facebook)
    if "<div" in raw_text or "<span" in raw_text or 'role="article"' in raw_text or 'aria-label=' in raw_text:
        html_cmts = parse_facebook_html_comments(raw_text)
        if html_cmts:
            return html_cmts

    lines = [l.strip() for l in raw_text.strip().split("\n") if l.strip()]
    if not lines:
        return []

    # 2. Thử nhận diện định dạng bôi đen copy từ Facebook Web (Block comment có nút Thích / Phản hồi)
    action_pattern = re.compile(r'\b(Thích|Phản hồi|Chia sẻ|Like|Reply|Share)\b', re.IGNORECASE)
    fb_text_comments = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if action_pattern.search(line):
            if i >= 2:
                user_candidate = lines[i-2]
                msg_candidate = lines[i-1]
                # Kiểm tra độ dài tên hợp lý
                if len(user_candidate) <= 50 and not action_pattern.search(user_candidate):
                    fb_text_comments.append({
                        "id": f"fb_copy_{len(fb_text_comments)+1}",
                        "user_id": f"user_{re.sub(r'[^a-zA-Z0-9]', '', user_candidate).lower() or len(fb_text_comments)+1}",
                        "user_name": user_candidate,
                        "user_avatar": "",
                        "user_link": "",
                        "message": msg_candidate,
                        "created_time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "like_count": 0,
                        "reply_count": 0,
                        "source": "facebook_text_copy"
                    })
            i += 1
        else:
            i += 1

    if fb_text_comments and len(fb_text_comments) >= 1:
        return fb_text_comments

    # 3. Định dạng tiêu chuẩn (User: Nội dung hoặc Excel \t)
    parsed_comments = []
    for idx, line in enumerate(lines):
        user_name = "Người dùng"
        message = line
        created_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Định dạng Tab-separated (copy từ Excel/Google Sheets)
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if len(parts) >= 2:
                user_name = parts[0]
                message = parts[1]
                if len(parts) >= 3 and any(c.isdigit() for c in parts[2]):
                    created_time = parts[2]
        # Định dạng "Tên: Bình luận"
        elif ":" in line and not line.startswith("http"):
            parts = line.split(":", 1)
            if len(parts[0]) <= 35 and parts[1].strip():
                user_name = parts[0].strip()
                message = parts[1].strip()

        parsed_comments.append({
            "id": f"text_cmt_{idx+1}",
            "user_id": f"user_{re.sub(r'[^a-zA-Z0-9]', '', user_name).lower() or idx+1}",
            "user_name": user_name,
            "user_avatar": "",
            "user_link": "",
            "message": message,
            "created_time": created_time,
            "like_count": 0,
            "reply_count": 0,
            "source": "manual_import"
        })

    return parsed_comments


def analyze_and_filter_comments(
    comments: List[Dict],
    filter_keywords: str = "",
    exclude_keywords: str = "",
    has_phone_only: bool = False,
    has_email_only: bool = False,
    no_links_only: bool = False,
    dedup_mode: str = "all",  # "all", "first_comment_only", "duplicates_only"
    min_duplicate_count: int = 2
) -> Dict[str, Any]:
    """
    Phân tích, trích xuất dữ liệu và thống kê trùng lặp chuyên sâu:
    1. Trích xuất SĐT, Email, Link cho từng comment.
    2. Thống kê trùng User (đếm số lần comment của từng người).
    3. Thống kê trùng Nội dung (nhóm các comment cùng nội dung).
    4. Lọc theo điều kiện người dùng chỉ định.
    5. Trích xuất Top từ khóa thịnh hành.
    """
    if not comments:
        return {
            "summary": {
                "total_comments": 0,
                "unique_users": 0,
                "duplicate_users_count": 0,
                "duplicate_rate_percent": 0.0,
                "phone_numbers_count": 0,
                "unique_phones_count": 0,
                "emails_count": 0,
                "links_count": 0,
                "filtered_count": 0
            },
            "comments": [],
            "duplicate_users": [],
            "duplicate_contents": [],
            "top_keywords": [],
            "all_phones": []
        }

    # 1. Thu thập & Trích xuất siêu dữ liệu sơ bộ cho từng comment
    user_counts = Counter()
    user_first_seen = {}
    user_content_counts = Counter()
    user_content_first_seen = {}
    content_map = {}
    processed_items = []

    for c in comments:
        user_id = str(c.get("user_id") or "").strip()
        user_name = str(c.get("user_name") or "Khách").strip()
        msg = str(c.get("message") or "").strip()

        post_id = str(c.get("post_id") or "default_post").strip()

        # Xác định định danh user duy nhất:
        if user_id:
            uid = user_id
        elif user_name and user_name not in ["Người dùng Facebook", "Facebook User", "Khách", "Anonymous", "unknown"]:
            # Dữ liệu nhập từ văn bản / Excel có tên người cụ thể
            uid = user_name
        else:
            # Comment ẩn danh từ Facebook Graph API (Facebook giấu danh tính vì quyền riêng tư)
            # Mỗi comment thuộc về 1 người riêng biệt, không gộp chung lại!
            uid = f"fb_anon_{c.get('id', id(c))}"

        # Đếm tần suất user THEO LEVEL POST ID (same post id):
        # Người dùng chỉ bị tính là trùng khi bình luận từ 2 lần trở lên TRONG CÙNG 1 BÀI VIẾT!
        post_user_key = f"{post_id}::{uid}"
        user_counts[post_user_key] += 1
        if post_user_key not in user_first_seen:
            user_first_seen[post_user_key] = c.get("id")

        # Trích xuất SĐT
        raw_phones = PHONE_REGEX.findall(msg)
        clean_phones = list(dict.fromkeys(normalize_phone_number(p) for p in raw_phones if len(normalize_phone_number(p)) >= 10))

        # Trích xuất Email
        emails = list(dict.fromkeys(EMAIL_REGEX.findall(msg)))

        # Trích xuất Links
        links = list(dict.fromkeys(URL_REGEX.findall(msg)))

        # Chuẩn hóa nội dung để so khớp trùng (bỏ dấu tiếng Việt, emoji, dấu câu, khoảng trắng thừa)
        normalized_content = normalize_comment_content(msg)
        if normalized_content:
            if normalized_content not in content_map:
                content_map[normalized_content] = {
                    "original_text": msg,
                    "count": 0,
                    "users": set(),
                    "comment_ids": []
                }
            content_map[normalized_content]["count"] += 1
            content_map[normalized_content]["users"].add(user_name)
            content_map[normalized_content]["comment_ids"].append(c.get("id"))

            # Đếm tần suất CÙNG NGƯỜI + CÙNG NỘI DUNG (Same User + Same Content):
            user_content_key = f"{post_id}::{uid}::{normalized_content}"
            user_content_counts[user_content_key] += 1
            if user_content_key not in user_content_first_seen:
                user_content_first_seen[user_content_key] = c.get("id")

        item = dict(c)
        item["post_id"] = post_id
        item["_uid"] = uid
        item["phones"] = clean_phones
        item["has_phone"] = len(clean_phones) > 0
        item["emails"] = emails
        item["has_email"] = len(emails) > 0
        item["links"] = links
        item["has_link"] = len(links) > 0
        item["user_comment_count"] = 0  # Sẽ gán sau
        item["is_first_comment"] = False
        item["is_duplicate_user"] = False
        item["user_same_content_count"] = 0
        item["is_duplicate_user_and_content"] = False
        item["is_first_user_and_content"] = False
        item["normalized_content"] = normalized_content
        processed_items.append(item)

    # 2. Gán nhãn trùng User & trùng CÙNG NGƯỜI + CÙNG NỘI DUNG THEO LEVEL POST ID
    for item in processed_items:
        post_id = item["post_id"]
        uid = item["_uid"]
        post_user_key = f"{post_id}::{uid}"
        cnt = user_counts[post_user_key]
        item["user_comment_count"] = cnt
        item["is_duplicate_user"] = (cnt > 1)
        item["is_first_comment"] = (item.get("id") == user_first_seen[post_user_key])

        norm_txt = item.get("normalized_content", "")
        if norm_txt:
            user_content_key = f"{post_id}::{uid}::{norm_txt}"
            uc_cnt = user_content_counts[user_content_key]
            item["user_same_content_count"] = uc_cnt
            item["is_duplicate_user_and_content"] = (uc_cnt > 1)
            item["is_first_user_and_content"] = (item.get("id") == user_content_first_seen[user_content_key])
        else:
            item["user_same_content_count"] = 1
            item["is_duplicate_user_and_content"] = False
            item["is_first_user_and_content"] = True

    # 3. Lập danh sách Thống kê Trùng Người THEO LEVEL POST ID (Minigame Deduplication)
    duplicate_users = []
    for post_user_key, cnt in user_counts.most_common():
        post_id_val, uid = post_user_key.split("::", 1)
        # Không gộp comment ẩn danh của Facebook vào danh sách trùng lặp
        if cnt >= min_duplicate_count and not uid.startswith("fb_anon_"):
            sample_cmt = next((it for it in processed_items if it.get("post_id") == post_id_val and it.get("_uid") == uid), {})
            user_comments = [it.get("message") for it in processed_items if it.get("post_id") == post_id_val and it.get("_uid") == uid]
            duplicate_users.append({
                "post_id": post_id_val,
                "user_id": uid,
                "user_name": sample_cmt.get("user_name", "Người dùng"),
                "user_avatar": sample_cmt.get("user_avatar", ""),
                "total_comments": cnt,
                "first_comment_time": sample_cmt.get("created_time", ""),
                "sample_messages": user_comments[:5]
            })

    # 4. Lập danh sách Thống kê Trùng Nội Dung (Seeding / Bot Detection)
    duplicate_contents = []
    for norm_text, data in sorted(content_map.items(), key=lambda x: x[1]["count"], reverse=True):
        if data["count"] >= min_duplicate_count and len(norm_text) > 2:
            duplicate_contents.append({
                "sample_text": data["original_text"],
                "normalized_text": norm_text,
                "count": data["count"],
                "unique_users_count": len(data["users"]),
                "sample_users": list(data["users"])[:5],
                "is_likely_bot": (len(data["users"]) > 1 and data["count"] >= 3)
            })

    # 5. Phân tích Từ khóa thịnh hành (Word Frequency)
    stop_words = {
        "và", "có", "là", "cho", "của", "được", "trong", "đã", "đang", "để", "không", "một", "các",
        "với", "này", "khi", "nhưng", "thì", "mà", "ở", "tại", "sẽ", "như", "hay", "cũng", "rất",
        "mình", "bạn", "ạ", "em", "anh", "chị", "ơi", "nè", "nhe", "nhé", "dạ", "cho", "với", "ko", "k"
    }
    word_counter = Counter()
    for item in processed_items:
        words = re.findall(r'[a-zA-ZàáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđĐ]+', item["message"].lower())
        for w in words:
            if len(w) >= 2 and w not in stop_words:
                word_counter[w] += 1

    top_keywords = [{"word": w, "count": c} for w, c in word_counter.most_common(30)]

    # 6. Áp dụng Bộ lọc (Filtering)
    filtered_items = []
    inc_keywords = [k.strip().lower() for k in filter_keywords.split(",") if k.strip()]
    exc_keywords = [k.strip().lower() for k in exclude_keywords.split(",") if k.strip()]

    for item in processed_items:
        msg_lower = item["message"].lower()

        # Lọc từ khóa cần có
        if inc_keywords and not any(ik in msg_lower for ik in inc_keywords):
            continue

        # Lọc từ khóa loại trừ
        if exc_keywords and any(ek in msg_lower for ek in exc_keywords):
            continue

        # Lọc SĐT
        if has_phone_only and not item["has_phone"]:
            continue

        # Lọc Email
        if has_email_only and not item["has_email"]:
            continue

        # Lọc bỏ comment chứa link
        if no_links_only and item["has_link"]:
            continue

        # Chế độ Deduplication:
        if dedup_mode == "first_comment_only":
            if not item["is_first_comment"]:
                continue
        elif dedup_mode == "duplicates_only":
            if not item["is_duplicate_user"]:
                continue
        elif dedup_mode == "same_user_same_content":
            if not item["is_duplicate_user_and_content"]:
                continue

        filtered_items.append(item)

    # 7. Tính toán các chỉ số KPI tổng quan
    total_comments = len(comments)
    unique_users = len(user_counts)
    dup_users_count = len(duplicate_users)
    dup_comments_count = total_comments - unique_users
    dup_rate = round((dup_comments_count / total_comments * 100), 1) if total_comments > 0 else 0.0

    all_phones = []
    for it in processed_items:
        all_phones.extend(it["phones"])
    unique_phones = list(dict.fromkeys(all_phones))

    all_emails = []
    for it in processed_items:
        all_emails.extend(it["emails"])

    all_links = []
    for it in processed_items:
        all_links.extend(it["links"])

    return {
        "summary": {
            "total_comments": total_comments,
            "unique_users": unique_users,
            "duplicate_users_count": dup_users_count,
            "duplicate_rate_percent": dup_rate,
            "phone_numbers_count": len(all_phones),
            "unique_phones_count": len(unique_phones),
            "emails_count": len(all_emails),
            "links_count": len(all_links),
            "filtered_count": len(filtered_items)
        },
        "comments": filtered_items,
        "duplicate_users": duplicate_users,
        "duplicate_contents": duplicate_contents,
        "top_keywords": top_keywords,
        "all_phones": unique_phones
    }


def export_comments_to_excel(
    comments: List[Dict],
    summary: Dict,
    output_path: str,
    post_title: str = "Social Scan"
) -> str:
    """
    Xuất báo cáo dữ liệu comment ra file Excel (.xlsx) định dạng chuyên nghiệp:
    - Sheet 1: Danh sách bình luận chi tiết (Avatar, Người dùng, UID, Thời gian, Nội dung, SĐT trích xuất, Trạng thái trùng).
    - Sheet 2: Bảng xếp hạng trùng người bình luận (Top Fans / Minigame).
    - Sheet 3: Bảng gom nhóm câu nói trùng lặp (Seeding / Bot).
    """
    wb = openpyxl.Workbook()

    # Font styles
    title_font = Font(name="Calibri", size=14, bold=True, color="1F2937")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    bold_font = Font(name="Calibri", size=11, bold=True)
    regular_font = Font(name="Calibri", size=11)
    phone_font = Font(name="Calibri", size=11, bold=True, color="059669")
    dup_font = Font(name="Calibri", size=11, color="D97706")

    # Header Fills
    primary_header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    orange_header_fill = PatternFill(start_color="D97706", end_color="D97706", fill_type="solid")
    purple_header_fill = PatternFill(start_color="7C3AED", end_color="7C3AED", fill_type="solid")
    zebra_fill = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="E5E7EB"),
        right=Side(style="thin", color="E5E7EB"),
        top=Side(style="thin", color="E5E7EB"),
        bottom=Side(style="thin", color="E5E7EB")
    )

    # ════════════════════════════════════════════════════════════════
    # SHEET 1: DANH SÁCH BÌNH LUẬN
    # ════════════════════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "Danh Sách Bình Luận"

    # Header Info
    ws1.cell(row=1, column=1, value="BÁO CÁO DỮ LIỆU BÌNH LUẬN SOCIAL SCAN").font = title_font
    ws1.cell(row=2, column=1, value=f"Nguồn: {post_title} — Xuất lúc: {datetime.now().strftime('%d/%m/%Y %H:%M')}").font = regular_font
    ws1.cell(row=3, column=1, value=f"Tổng bình luận: {summary.get('total_comments', 0)} | Người duy nhất: {summary.get('unique_users', 0)} | SĐT: {summary.get('unique_phones_count', 0)} | Tỷ lệ trùng: {summary.get('duplicate_rate_percent', 0)}%").font = bold_font

    headers1 = ["STT", "Người Dùng", "User ID", "Thời Gian", "Nội Dung Bình Luận", "Số Điện Thoại", "Email", "Số Lần Comment", "Trạng Thái", "Có Link"]
    for col_idx, h in enumerate(headers1, 1):
        cell = ws1.cell(row=5, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = primary_header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_idx, c in enumerate(comments, 6):
        phones_str = ", ".join(c.get("phones", []))
        emails_str = ", ".join(c.get("emails", []))
        user_cnt = c.get("user_comment_count", 1)
        
        if c.get("is_first_comment") and not c.get("is_content_duplicate"):
            status_text = "Độc nhất"
        elif c.get("is_content_duplicate") and user_cnt > 1:
            status_text = f"Trùng tài khoản & câu ({user_cnt} lần)"
        elif user_cnt > 1:
            status_text = f"Trùng tài khoản ({user_cnt} lần)"
        elif c.get("is_content_duplicate"):
            status_text = "Trùng câu"
        else:
            status_text = "Độc nhất"

        row_cells = [
            ws1.cell(row=row_idx, column=1, value=row_idx - 5),
            ws1.cell(row=row_idx, column=2, value=c.get("user_name", "")),
            ws1.cell(row=row_idx, column=3, value=str(c.get("user_id", ""))),
            ws1.cell(row=row_idx, column=4, value=c.get("created_time", "")),
            ws1.cell(row=row_idx, column=5, value=c.get("message", "")),
            ws1.cell(row=row_idx, column=6, value=phones_str),
            ws1.cell(row=row_idx, column=7, value=emails_str),
            ws1.cell(row=row_idx, column=8, value=user_cnt),
            ws1.cell(row=row_idx, column=9, value=status_text),
            ws1.cell(row=row_idx, column=10, value="Có" if c.get("has_link") else "Không")
        ]

        for cell in row_cells:
            cell.font = regular_font
            cell.border = thin_border
            if row_idx % 2 == 0:
                cell.fill = zebra_fill

        # Tô nổi bật cột SĐT và cột trạng thái trùng
        if phones_str:
            row_cells[5].font = phone_font
        if not c.get("is_first_comment") or c.get("is_content_duplicate"):
            row_cells[8].font = dup_font

    # ════════════════════════════════════════════════════════════════
    # SHEET 2: THỐNG KÊ TÀI KHOẢN TRÙNG LẶP
    # ════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet(title="Tài Khoản Trùng Lặp")
    ws2.cell(row=1, column=1, value="BẢNG XẾP HẠNG TÀI KHOẢN BÌNH LUẬN NHIỀU LẦN (SPAM/LẶP LẠI)").font = title_font

    headers2 = ["Hạng", "Tên Người Dùng", "User ID", "Tổng Số Comment", "Mốc Comment Đầu Tiên", "Mẫu Nội Dung Đã Bình Luận"]
    for col_idx, h in enumerate(headers2, 1):
        cell = ws2.cell(row=3, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = orange_header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    dup_users = summary.get("duplicate_users", [])
    if not dup_users:
        user_counts = Counter(c.get("user_name") for c in comments)
        dup_users = [{"user_name": u, "total_comments": cnt, "user_id": "", "first_comment_time": "", "sample_messages": []}
                     for u, cnt in user_counts.most_common() if cnt > 1]

    for row_idx, u in enumerate(dup_users, 4):
        samples = " | ".join(u.get("sample_messages", [])[:3])
        row_cells = [
            ws2.cell(row=row_idx, column=1, value=row_idx - 3),
            ws2.cell(row=row_idx, column=2, value=u.get("user_name", "")),
            ws2.cell(row=row_idx, column=3, value=str(u.get("user_id", ""))),
            ws2.cell(row=row_idx, column=4, value=u.get("total_comments", 0)),
            ws2.cell(row=row_idx, column=5, value=u.get("first_comment_time", "")),
            ws2.cell(row=row_idx, column=6, value=samples)
        ]
        for cell in row_cells:
            cell.font = regular_font
            cell.border = thin_border
            if row_idx % 2 == 0:
                cell.fill = zebra_fill

    # ════════════════════════════════════════════════════════════════
    # SHEET 3: THỐNG KÊ TRÙNG NỘI DUNG (SEEDING / BOT)
    # ════════════════════════════════════════════════════════════════
    ws3 = wb.create_sheet(title="Trùng Nội Dung Seeding")
    ws3.cell(row=1, column=1, value="BẢNG GOM NHÓM NỘI DUNG BÌNH LUẬN TRÙNG LẶP (SEEDING / BOT)").font = title_font

    headers3 = ["STT", "Nội Dung Lặp Lại", "Số Lần Xuất Hiện", "Số Người Khác Nhau Cùng Comment", "Dấu Hiệu Seeding Bot"]
    for col_idx, h in enumerate(headers3, 1):
        cell = ws3.cell(row=3, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = purple_header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    dup_contents = summary.get("duplicate_contents", [])
    for row_idx, dc in enumerate(dup_contents, 4):
        is_bot = "Có thể là Seeding / Bot" if dc.get("is_likely_bot") else "Bình thường"
        row_cells = [
            ws3.cell(row=row_idx, column=1, value=row_idx - 3),
            ws3.cell(row=row_idx, column=2, value=dc.get("sample_text", "")),
            ws3.cell(row=row_idx, column=3, value=dc.get("count", 0)),
            ws3.cell(row=row_idx, column=4, value=dc.get("unique_users_count", 0)),
            ws3.cell(row=row_idx, column=5, value=is_bot)
        ]
        for cell in row_cells:
            cell.font = regular_font
            cell.border = thin_border
            if row_idx % 2 == 0:
                cell.fill = zebra_fill

    # Tự động điều chỉnh độ rộng cột cho cả 3 sheet
    for ws in [ws1, ws2, ws3]:
        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 60)

    # Đảm bảo thư mục lưu file tồn tại
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    wb.save(output_path)
    return output_path
