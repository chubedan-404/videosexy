"""
tiktok_scraper.py — Scrape TikTok / Douyin dùng Playwright headless browser
Không cần login, không cần cookie, không cần extension.
"""

import asyncio
import json
import re
import urllib.parse
from typing import List, Dict, Tuple, Optional

TIKTOK_API_PATTERNS = [
    # TikTok web responses use both www.tiktok.com and api*.tiktok.com hosts.
    r"(?:www\.|api[.-]?[\w.]*\.)?tiktok\.com.*/api/post/item_list",
    r"(?:www\.|api[.-]?[\w.]*\.)?tiktok\.com.*/api/user/post",
    r"(?:www\.|api[.-]?[\w.]*\.)?tiktok\.com.*/api/challenge/item_list",
    r"m\.tiktok\.com/api/post/item_list",
    r"api[.-]?[\w.]*douyin\.com.*/aweme/v1/aweme/post",
    r"aweme\.snssdk\.com/aweme/v1",
]

SCROLL_PAUSE_MS = 1500
MAX_SCROLL_ATTEMPTS = 8


class TikTokPlaywrightScraper:
    """Scrape TikTok/Douyin dùng Playwright headless. Không cần login."""

    def __init__(self, headless: bool = True, timeout_ms: int = 30000):
        self.headless = headless
        self.timeout_ms = timeout_ms

    def fetch_user_videos(self, username: str, max_results: int = 20, platform: str = "tiktok") -> Tuple[List[Dict], str]:
        return asyncio.run(self._async_fetch_user(username, max_results, platform))

    def fetch_hashtag_videos(self, tag: str, max_results: int = 20, platform: str = "tiktok") -> Tuple[List[Dict], str]:
        return asyncio.run(self._async_fetch_hashtag(tag, max_results, platform))

    async def _async_fetch_user(self, username: str, max_results: int, platform: str) -> Tuple[List[Dict], str]:
        if platform == "tiktok":
            url = f"https://www.tiktok.com/@{username}"
        else:
            url = f"https://www.douyin.com/user/{username}"
        videos, channel_name = await self._scrape_page(url, max_results, platform)
        return videos, channel_name or f"@{username}"

    async def _async_fetch_hashtag(self, tag: str, max_results: int, platform: str) -> Tuple[List[Dict], str]:
        tag_clean = tag.lstrip("#")
        if platform == "tiktok":
            url = f"https://www.tiktok.com/tag/{urllib.parse.quote(tag_clean)}"
        else:
            url = f"https://www.douyin.com/hashtag/{urllib.parse.quote(tag_clean)}"
        videos, _ = await self._scrape_page(url, max_results, platform)
        return videos, f"Hashtag: #{tag_clean}"

    async def _scrape_page(self, url: str, max_results: int, platform: str) -> Tuple[List[Dict], str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright chưa cài. Chạy: pip install playwright && playwright install chromium")

        collected: List[Dict] = []
        channel_name = ""

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="vi-VN",
                timezone_id="Asia/Ho_Chi_Minh",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            async def handle_response(response):
                nonlocal channel_name
                is_video_api = any(re.search(p, response.url, re.IGNORECASE) for p in TIKTOK_API_PATTERNS)
                if not is_video_api:
                    return
                try:
                    body = await response.json()
                    videos = self._parse_api_response(body, platform)
                    if videos:
                        collected.extend(videos)
                        print(f"[🎬 Playwright] +{len(videos)} video (tổng: {len(collected)})")
                    if not channel_name:
                        channel_name = self._extract_channel_name(body)
                except Exception:
                    pass

            page.on("response", handle_response)

            print(f"[🌐 Playwright] Mở: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            except Exception as e:
                print(f"[⚠️ Playwright] {e}")

            await asyncio.sleep(3)

            scroll_count = 0
            while len(collected) < max_results and scroll_count < MAX_SCROLL_ATTEMPTS:
                await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                await asyncio.sleep(SCROLL_PAUSE_MS / 1000)
                scroll_count += 1
                print(f"[📜] Scroll #{scroll_count}, có {len(collected)} video")

            await browser.close()

        collected = collected[:max_results]
        for i, v in enumerate(collected, 1):
            v["index"] = i
        return collected, channel_name

    def _parse_api_response(self, body: dict, platform: str) -> List[Dict]:
        items = body.get("itemList") or body.get("items") or body.get("aweme_list") or []
        videos = []
        for item in items:
            try:
                v = self._parse_single_item(item, platform)
                if v:
                    videos.append(v)
            except Exception:
                pass
        return videos

    def _parse_single_item(self, item: dict, platform: str) -> Optional[Dict]:
        video_id = item.get("id") or item.get("aweme_id") or item.get("video", {}).get("id")
        if not video_id:
            return None

        author = item.get("author") or item.get("authorInfo") or {}
        username = author.get("uniqueId") or author.get("unique_id") or author.get("uid") or ""
        nickname = author.get("nickname") or username

        title = (
            item.get("desc") or
            item.get("share_info", {}).get("share_desc") or
            f"Video {video_id}"
        )

        thumbnail = ""
        cover = item.get("video", {}).get("cover") or item.get("video", {}).get("originCover") or {}
        if isinstance(cover, dict):
            urls = cover.get("urlList") or cover.get("url_list") or []
            thumbnail = urls[0] if urls else ""

        duration_sec = item.get("video", {}).get("duration") or item.get("duration") or 0
        if duration_sec > 1000:
            duration_sec = duration_sec // 1000
        minutes, seconds = divmod(int(duration_sec), 60)
        duration_str = f"{minutes}:{seconds:02d}"

        url = (
            f"https://www.tiktok.com/@{username}/video/{video_id}"
            if platform == "tiktok"
            else f"https://www.douyin.com/video/{video_id}"
        )

        return {
            "id": str(video_id),
            "url": url,
            "title": title,
            "translated_title": None,
            "thumbnail": thumbnail,
            "duration": duration_str,
            "uploader": nickname,
            "platform": platform,
            "index": 0,
        }

    def _extract_channel_name(self, body: dict) -> str:
        user_info = body.get("userInfo", {}).get("user") or body.get("author") or body.get("user") or {}
        return user_info.get("nickname") or user_info.get("uniqueId") or ""


if __name__ == "__main__":
    import sys
    username = sys.argv[1] if len(sys.argv) > 1 else "spiderum"
    print(f"Playwright scraping @{username}...")
    scraper = TikTokPlaywrightScraper(headless=True)
    try:
        videos, channel = scraper.fetch_user_videos(username, max_results=5)
        print(f"Kênh: {channel} — {len(videos)} video")
        for v in videos:
            print(f"  [{v['index']}] {v['title'][:70]} ({v['duration']})")
    except Exception as e:
        print(f"Lỗi: {e}")
