import os
import sys
import time
import shutil
import subprocess
import json
import re
import urllib.parse
import requests
import srt
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import yt_dlp
import whisper
import torch

try:
    from faster_whisper import WhisperModel as FasterWhisperModel
    HAS_FASTER_WHISPER = True
except ImportError:
    HAS_FASTER_WHISPER = False

from tts_engine import TTSEngine
from gemini_translator import GeminiTranslator, TRANSLATION_STYLES, load_gemini_config
from openai_translator import OpenAITranslator
from douyin_fetcher import clean_social_hashtags
from sub_eraser import SubtitleEraser
import timing_planner as tp
import sync_engine as se
from shorten_service import make_shortener


# Bộ quy tắc tự động chèn emoji theo ngữ cảnh tiếng Việt
EMOJI_RULES = [
    (r"\b(em bé|trẻ con|bé yêu|bé cưng|con yêu|trẻ sơ sinh)\b", " 👶"),
    (r"\b(mẹ bỉm|mẹ ơi|các mom|mẹ yêu|bà mẹ)\b", " 👩‍🍼"),
    (r"\b(ăn dặm|nấu ăn|món ăn|bữa ăn|nấu cháo|cháo dinh dưỡng)\b", " 🍲"),
    (r"\b(sữa bột|bình sữa|pha sữa|uống sữa|hộp sữa)\b", " 🍼"),
    (r"\b(ngủ ngon|giấc ngủ|đi ngủ|ru ngủ|buồn ngủ)\b", " 😴"),
    (r"\b(đồ chơi|gấu bông|xếp hình)\b", " 🧸"),
    (r"\b(review|mua sắm|sản phẩm|đập hộp|unboxing|giỏ hàng|sale)\b", " 🛍️"),
    (r"\b(tiết kiệm|giá rẻ|tiền|chi phí|mua rẻ)\b", " 💰"),
    (r"\b(bí quyết|mẹo hay|mẹo vặt|kinh nghiệm|hướng dẫn|lưu ý)\b", " 💡"),
    (r"\b(yêu thích|dễ thương|đáng yêu|tuyệt vời|thích thú)\b", " 💖"),
    (r"\b(ngon miệng|ngon tuyệt|hấp dẫn|thơm ngon)\b", " 😋"),
    (r"\b(cảnh báo|nguy hiểm|chú ý)\b", " ⚠️"),
    (r"\b(xuất sắc|bất ngờ|vi diệu|kỳ diệu|thành công)\b", " ✨"),
]

def auto_enrich_emojis(text: str) -> str:
    """Tự động chèn emoji cảm xúc vào câu thoại tiếng Việt nếu phù hợp."""
    if not text:
        return text
    # Nếu câu đã có emoji rồi thì không chèn thêm
    has_emoji = any(ord(char) > 0x1F000 for char in text)
    if has_emoji:
        return text
    enriched = text
    added = 0
    for pattern, emoji_str in EMOJI_RULES:
        if added >= 2:
            break
        if re.search(pattern, enriched, re.IGNORECASE):
            if not enriched.endswith(emoji_str):
                enriched += emoji_str
                added += 1
    return enriched


# Bảng ánh xạ Google Translate language codes
LANG_MAP = {
    "vi": "vi", "en": "en", "ko": "ko", "ja": "ja",
    "zh": "zh-CN", "zh-CN": "zh-CN", "zh-TW": "zh-TW",
    "th": "th", "id": "id", "fr": "fr", "de": "de", "es": "es",
}

# Whisper language codes
WHISPER_LANG_MAP = {
    "zh": "zh", "zh-CN": "zh", "zh-TW": "zh",
    "vi": "vi", "en": "en", "ko": "ko", "ja": "ja",
    "th": "th", "id": "id", "fr": "fr", "de": "de", "es": "es",
    "auto": None,  # None = Whisper tự detect
}


class VideoProcessor:
    def __init__(self, download_dir: str, cookie_file: str = None):
        self.download_dir = download_dir
        self.cookie_file = cookie_file if cookie_file and os.path.exists(cookie_file) else None
        os.makedirs(self.download_dir, exist_ok=True)
        self.whisper_model = None
        self.faster_whisper_model = None
        self.current_model_name = None

        # FFmpeg & FFprobe tĩnh hoặc system
        local_ffmpeg = os.path.join(os.path.dirname(__file__), "ffmpeg")
        if os.path.exists(local_ffmpeg):
            self.ffmpeg_cmd = local_ffmpeg
            print(f"[🎬 FFmpeg] Sử dụng FFmpeg tĩnh: {local_ffmpeg}")
        else:
            self.ffmpeg_cmd = "ffmpeg"

        local_ffprobe = os.path.join(os.path.dirname(__file__), "ffprobe")
        if os.path.exists(local_ffprobe):
            self.ffprobe_cmd = local_ffprobe
        else:
            self.ffprobe_cmd = "ffprobe"

        # Thiết bị phần cứng & faster-whisper acceleration
        if torch.cuda.is_available():
            self.device = "cuda"
            self.fw_device = "cuda"
            self.fw_compute_type = "float16"
            print("[🚀 Hardware] NVIDIA GPU (CUDA)")
        elif torch.backends.mps.is_available():
            self.device = "mps"
            self.fw_device = "cpu"
            self.fw_compute_type = "int8"  # CTranslate2 int8 ARM NEON cực nhanh trên Apple Silicon
            print("[🚀 Hardware] Apple Silicon (MPS / NEON int8)")
        else:
            self.device = "cpu"
            self.fw_device = "cpu"
            self.fw_compute_type = "int8"
            print("[ℹ️ Hardware] CPU mode (int8 optimized)")

        # TTS Engine (edge-tts)
        self.tts_engine = TTSEngine(download_dir=self.download_dir, ffmpeg_cmd=self.ffmpeg_cmd)
        # Subtitle Eraser (Delogo / Inpainting)
        self.sub_eraser = SubtitleEraser(ffmpeg_cmd=self.ffmpeg_cmd, ffprobe_cmd=self.ffprobe_cmd)

    # ──────────────────────────────────────────────────────────────────────────
    # DOWNLOAD
    # ──────────────────────────────────────────────────────────────────────────

    def _download_tiktok_direct(self, video_url: str, video_id: str, output_path: str) -> bool:
        """
        Tải video TikTok không watermark trực tiếp qua TikWM API.
        Vượt qua triệt để lỗi chặn status code 0 / anti-bot của yt-dlp.
        Không cần cookie trình duyệt, tốc độ cao (3-5s).
        """
        target_urls = []
        if video_url:
            target_urls.append(video_url)
        if video_id:
            target_urls.append(f"https://www.tiktok.com/video/{video_id}")
            target_urls.append(f"https://www.tiktok.com/@user/video/{video_id}")

        headers_api = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept': 'application/json, text/plain, */*',
        }

        # Thử lần lượt các URL và mirror domain
        for test_url in target_urls:
            for domain in ['www.tikwm.com', 'tikwm.com']:
                try:
                    api_endpoint = f"https://{domain}/api/?url={urllib.parse.quote(test_url, safe='')}"
                    print(f"[⚡ TikTok Direct] Thử tải qua {domain} cho video {video_id}...")
                    resp = requests.get(api_endpoint, headers=headers_api, timeout=12)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if data.get('code') != 0 or not data.get('data'):
                        continue

                    v_data = data['data']
                    # Ưu tiên: hdplay -> play -> wmplay
                    play_url = v_data.get('hdplay') or v_data.get('play') or v_data.get('wmplay')
                    if not play_url:
                        continue

                    if play_url.startswith('//'):
                        play_url = 'https:' + play_url
                    elif play_url.startswith('/'):
                        play_url = f'https://{domain}' + play_url

                    # Lưu ý: Không để Referer là bên thứ 3 vì tiktokcdn sẽ chặn bằng lỗi 503
                    ua_str = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                    print(f"[⚡ TikTok Direct] Đang tải video sạch watermark về máy...")
                    downloaded = False
                    try:
                        curl_cmd = [
                            'curl', '-L', '-s', '--fail',
                            '--connect-timeout', '10',
                            '--max-time', '60',
                            '-A', ua_str,
                            '-o', output_path,
                            play_url
                        ]
                        cp = subprocess.run(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=65)
                        if cp.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 100 * 1024:
                            downloaded = True
                    except Exception as curl_err:
                        print(f"  [⚠️ curl stream] Thử bằng requests: {curl_err}")

                    if not downloaded:
                        # Fallback sang requests nếu curl gặp trục trặc
                        dl_headers = {'User-Agent': ua_str}
                        with requests.get(play_url, headers=dl_headers, stream=True, timeout=60) as dl_resp:
                            dl_resp.raise_for_status()
                            with open(output_path, 'wb') as f:
                                for chunk in dl_resp.iter_content(chunk_size=512 * 1024):
                                    if chunk:
                                        f.write(chunk)

                    if os.path.exists(output_path) and os.path.getsize(output_path) > 100 * 1024:
                        size_mb = os.path.getsize(output_path) / 1024 / 1024
                        print(f"[✅ TikTok Direct] Tải thành công ({size_mb:.1f}MB) không watermark!")
                        return True
                    else:
                        if os.path.exists(output_path):
                            os.remove(output_path)
                except Exception as e:
                    print(f"[⚠️ TikTok Direct] Lỗi qua {domain}: {e}")
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            pass
                    continue

        return False

    def download_video(self, video_url: str, video_id: str) -> str:
        """
        Tải video không watermark từ Douyin / TikTok / Facebook.
        Tự động nhận diện nền tảng, dùng cookie trình duyệt, retry nhiều format.
        """
        output_path = os.path.join(self.download_dir, f"{video_id}.mp4")
        if os.path.exists(output_path):
            print(f"[✅ Cache] Video {video_id}.mp4 đã tồn tại.")
            return output_path

        # Nhận diện nền tảng để dùng header/config phù hợp
        is_tiktok = 'tiktok.com' in video_url or 'tiktok' in video_url.lower()
        is_facebook = any(d in video_url for d in ['facebook.com', 'fb.watch', 'fb.gg', 'm.facebook.com'])
        platform_name = "TikTok" if is_tiktok else ("Facebook" if is_facebook else "Douyin")

        # ⚡ TIKTOK DIRECT FAST-PATH:
        # TikTok thường chặn yt-dlp (status code 0 / anti-bot). Ưu tiên tải trực tiếp không watermark.
        if is_tiktok:
            if self._download_tiktok_direct(video_url, video_id, output_path):
                return output_path

        if is_tiktok:
            base_headers = {
                'User-Agent': (
                    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                    'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                    'Version/17.0 Mobile/15E148 Safari/604.1'
                ),
                'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
                'Referer': 'https://www.tiktok.com/',
            }
        elif is_facebook:
            base_headers = {
                'User-Agent': (
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                ),
                'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
                'Referer': 'https://www.facebook.com/',
            }
        else:
            base_headers = {
                'User-Agent': (
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                ),
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Referer': 'https://www.douyin.com/',
            }

        # Thứ tự ưu tiên lấy cookie
        def apply_cookies(opts: dict) -> dict:
            if self.cookie_file:
                opts['cookiefile'] = self.cookie_file
                print(f"[🍪 Cookie] Dùng file: {self.cookie_file}")
                return opts
            # Thử lấy từ trình duyệt theo thứ tự ưu tiên
            for browser in ['chrome', 'chromium', 'safari', 'edge', 'firefox']:
                try:
                    test_opts = {**opts, 'cookiesfrombrowser': (browser,)}
                    # Kiểm tra nhanh xem browser có cookie không
                    with yt_dlp.YoutubeDL({
                        'cookiesfrombrowser': (browser,),
                        'quiet': True,
                        'skip_download': True,
                    }):
                        pass
                    opts['cookiesfrombrowser'] = (browser,)
                    print(f"[🍪 Cookie] Lấy từ trình duyệt: {browser}")
                    return opts
                except Exception:
                    continue
            print(f"[⚠️ Cookie] Không tìm thấy cookie trình duyệt — thử không có cookie")
            return opts

        # Danh sách format thử theo thứ tự (high → low quality)
        format_list = [
            'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
            'best[ext=mp4]/best',
            'best',
        ]

        def build_opts(fmt: str) -> dict:
            opts = {
                'format': fmt,
                'outtmpl': output_path,
                'quiet': False,
                'no_warnings': True,
                'merge_output_format': 'mp4',
                'http_headers': base_headers,
                'user_agent': base_headers['User-Agent'],
                'socket_timeout': 120,
                'retries': 5,
                'fragment_retries': 5,
                'continuedl': True,
                'noprogress': True,
                'concurrent_fragment_downloads': 3,
            }
            # Extractor args theo nền tảng
            if is_tiktok:
                opts['extractor_args'] = {
                    'tiktok': {
                        'webpage_download': ['1'],
                    }
                }
            elif is_facebook:
                # Facebook extractor trong yt-dlp không cần extractor_args đặc biệt
                pass
            else:
                opts['extractor_args'] = {
                    'douyin': {'app_version': '26.1.0'},
                }
            return apply_cookies(opts)

        print(f"[⬇️ {platform_name}] Bắt đầu tải video {video_id}...")
        last_error = None

        for attempt, fmt in enumerate(format_list, 1):
            try:
                print(f"  → Lần {attempt}/{len(format_list)}: format={fmt[:35]}...")
                with yt_dlp.YoutubeDL(build_opts(fmt)) as ydl:
                    ydl.download([video_url])

                if os.path.exists(output_path):
                    size_mb = os.path.getsize(output_path) / 1024 / 1024
                    print(f"[✅ Download] Thành công! File: {size_mb:.1f}MB")
                    return output_path

            except yt_dlp.utils.DownloadError as e:
                last_error = e
                err_str = str(e).lower()

                # Cứu cánh TikWM cho TikTok trước khi báo lỗi
                if is_tiktok:
                    print(f"  [⚡ Fallback] yt-dlp lỗi, thử tải trực tiếp TikWM cho TikTok {video_id}...")
                    if self._download_tiktok_direct(video_url, video_id, output_path):
                        return output_path

                # TikTok: Unable to extract webpage video data → cần cookie
                if 'unable to extract webpage video data' in err_str or 'unable to extract' in err_str:
                    raise Exception(
                        f"❌ {platform_name} không cho phép tải video này mà không có Cookie đăng nhập.\n\n"
                        "📋 CÁCH FIX (2 phút):\n"
                        "  1. Mở Chrome → Vào tiktok.com → Đăng nhập tài khoản\n"
                        "  2. Cài extension: 'Get cookies.txt LOCALLY' (Chrome Web Store)\n"
                        "  3. Click vào extension → chọn 'Export' → lưu thành cookies.txt\n"
                        "  4. Copy file cookies.txt vào thư mục app (cùng chỗ với app.py)\n"
                        "  5. Restart app và thử lại"
                    ) from e

                # Timeout → thử format đơn giản hơn
                if any(k in err_str for k in ['timeout', 'timed out', 'connection aborted', 'connection reset']):
                    print(f"  ⏱️ Timeout lần {attempt}, thử format khác...")
                    continue

                # 403/429 → IP bị block
                if any(k in err_str for k in ['http error 403', 'http error 429', '403', '429']):
                    raise Exception(
                        f"❌ {platform_name} đang chặn IP của bạn (lỗi 403/429).\n"
                        "💡 Giải pháp: Bật VPN (server TQ hoặc HK) hoặc đổi sang mạng 4G/5G."
                    ) from e

                # Lỗi cookie/login
                if any(k in err_str for k in ['login required', 'sign in', 'captcha']):
                    raise Exception(
                        f"❌ {platform_name} yêu cầu đăng nhập.\n"
                        "💡 Cần thêm file cookies.txt từ Chrome đã đăng nhập."
                    ) from e

                # Lỗi khác → thử tiếp
                print(f"  ⚠️ Lỗi: {str(e)[:80]}")
                continue

            except Exception as e:
                last_error = e
                print(f"  ⚠️ Exception: {str(e)[:80]}")
                if is_tiktok:
                    if self._download_tiktok_direct(video_url, video_id, output_path):
                        return output_path
                continue

        # Cứu cánh cuối cùng nếu hết mọi lần thử
        if is_tiktok:
            print(f"[⚡ TikTok Direct] Kích hoạt cứu cánh TikWM cuối cùng cho video {video_id}...")
            if self._download_tiktok_direct(video_url, video_id, output_path):
                return output_path

        # Hết tất cả lần thử
        err_msg = str(last_error) if last_error else "Lỗi không xác định"
        if any(k in err_msg.lower() for k in ['timeout', 'timed out', 'connection aborted']):
            raise Exception(
                f"Không thể tải video {platform_name} sau {len(format_list)} lần thử.\n"
                "💡 Kiểm tra: kết nối internet, VPN, hoặc thêm cookies.txt"
            )
        raise Exception(f"Không thể tải video: {err_msg}")

    # ──────────────────────────────────────────────────────────────────────────
    # AUDIO EXTRACTION
    # ──────────────────────────────────────────────────────────────────────────

    def extract_audio(self, video_path: str, video_id: str) -> str:
        """Tách âm thanh từ video sang WAV 16kHz mono cho Whisper."""
        audio_path = os.path.join(self.download_dir, f"{video_id}.wav")
        if os.path.exists(audio_path):
            return audio_path

        cmd = [
            self.ffmpeg_cmd, '-y',
            '-i', video_path,
            '-vn', '-acodec', 'pcm_s16le',
            '-ar', '16000', '-ac', '1',
            audio_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return audio_path

    def separate_vocal_and_bgm(self, video_id: str, video_path: str) -> str:
        """
        Dùng Meta Demucs v4 (htdemucs) để tách giọng nói (vocals) ra khỏi nhạc nền & âm thanh hiện trường (no_vocals).
        Trả về đường dẫn tới file WAV 'no_vocals' đã tách.
        Có cơ chế cache: nếu đã tách trước đó, tái sử dụng ngay trong 0s.
        """
        final_no_vocals = os.path.join(self.download_dir, f"{video_id}_no_vocals.wav")
        if os.path.exists(final_no_vocals) and os.path.getsize(final_no_vocals) > 1000:
            print(f"[🎵 Demucs Cache] Đã tìm thấy âm thanh no_vocals có sẵn cho {video_id}: {final_no_vocals}")
            return final_no_vocals

        print(f"[🎧 AI Vocal Separation] Bắt đầu tách Vocal bằng Demucs v4 cho {video_id}...")

        # 1. Trích xuất audio chất lượng cao 44.1kHz Stereo từ video gốc
        temp_orig_audio = os.path.join(self.download_dir, f"{video_id}_orig_hq.wav")
        try:
            cmd_extract = [
                self.ffmpeg_cmd, '-y',
                '-i', video_path,
                '-vn', '-acodec', 'pcm_s16le',
                '-ar', '44100', '-ac', '2',
                temp_orig_audio
            ]
            subprocess.run(cmd_extract, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[❌ Demucs Error] Lỗi trích xuất audio gốc: {e}")
            return None

        # 2. Xác định thiết bị tăng tốc (MPS cho Apple Silicon Mac, CUDA cho NVIDIA, CPU fallback)
        device = "cpu"
        try:
            if torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
        except Exception:
            device = "cpu"

        demucs_out_dir = os.path.join(self.download_dir, f"demucs_{video_id}")
        os.makedirs(demucs_out_dir, exist_ok=True)

        cmd_demucs = [
            sys.executable, "-m", "demucs",
            "--two-stems", "vocals",
            "-n", "htdemucs",
            "-d", device,
            "-o", demucs_out_dir,
            temp_orig_audio
        ]

        print(f"[🤖 Demucs] Đang chạy model htdemucs trên thiết bị [{device.upper()}]...")
        try:
            subprocess.run(cmd_demucs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            # Demucs lưu tại: {demucs_out_dir}/htdemucs/{base_name}/no_vocals.wav
            base_name = os.path.splitext(os.path.basename(temp_orig_audio))[0]
            separated_bgm = os.path.join(demucs_out_dir, "htdemucs", base_name, "no_vocals.wav")

            if os.path.exists(separated_bgm) and os.path.getsize(separated_bgm) > 1000:
                shutil.move(separated_bgm, final_no_vocals)
                print(f"[✅ Demucs Success] Tách Vocal thành công! File BGM & SFX gốc: {final_no_vocals}")
                return final_no_vocals
            else:
                print(f"[⚠️ Demucs Warning] Không tìm thấy file no_vocals.wav tại {separated_bgm}")
                return None
        except subprocess.CalledProcessError as e:
            print(f"[❌ Demucs Error] Lỗi khi chạy Demucs: {e.stderr}")
            return None
        except Exception as e:
            print(f"[❌ Demucs Error] Lỗi không xác định: {e}")
            return None
        finally:
            if os.path.exists(temp_orig_audio):
                try:
                    os.remove(temp_orig_audio)
                except Exception:
                    pass
            if os.path.exists(demucs_out_dir):
                try:
                    shutil.rmtree(demucs_out_dir, ignore_errors=True)
                except Exception:
                    pass

    # ──────────────────────────────────────────────────────────────────────────
    # TRANSCRIPTION (Whisper AI)
    # ──────────────────────────────────────────────────────────────────────────

    def transcribe_audio(
        self,
        video_id: str,
        model_name: str = "large-v3-turbo",
        source_lang: str = "auto",
        progress_callback=None
    ) -> list:
        """
        Dùng faster-whisper (hoặc OpenAI Whisper fallback) để tạo transcript từ âm thanh video.

        :param video_id: ID video
        :param model_name: Model Whisper (large-v3-turbo/medium/small/base/tiny)
        :param source_lang: Ngôn ngữ nguồn ('auto' = tự detect, 'zh'=tiếng Trung, 'vi'=tiếng Việt...)
        :return: Danh sách segments [{"index", "start", "end", "source_text", "translated_text"}]
        """
        video_path = os.path.join(self.download_dir, f"{video_id}.mp4")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Không tìm thấy file video: {video_path}")

        audio_path = self.extract_audio(video_path, video_id)
        whisper_lang = WHISPER_LANG_MAP.get(source_lang, None)

        # 1. Thử dùng Faster-Whisper trước (Tốc độ x4-x8, nhẹ RAM)
        if HAS_FASTER_WHISPER:
            try:
                fw_model_name = "large-v3-turbo" if model_name in ["turbo", "large-v3-turbo"] else model_name
                print(f"[⚡ Faster-Whisper] Tải model '{fw_model_name}' trên [{self.fw_device}:{self.fw_compute_type}]...")
                if progress_callback:
                    progress_callback(f"Đang tải Faster-Whisper model '{fw_model_name}'...", step=2)

                if self.faster_whisper_model is None or self.current_model_name != fw_model_name:
                    try:
                        self.faster_whisper_model = FasterWhisperModel(
                            fw_model_name,
                            device=self.fw_device,
                            compute_type=self.fw_compute_type
                        )
                    except Exception as e_load:
                        print(f"[⚠️ Faster-Whisper Load Warning] {e_load}, fallback cpu float32...")
                        self.faster_whisper_model = FasterWhisperModel(
                            fw_model_name,
                            device="cpu",
                            compute_type="float32"
                        )
                    self.current_model_name = fw_model_name

                print(f"[⚡ Faster-Whisper] Đang transcribe (lang={whisper_lang or 'auto-detect'})...")
                if progress_callback:
                    progress_callback(f"Đang nhận dạng giọng nói (Faster-Whisper {fw_model_name})...", step=2)

                segments_iter, info = self.faster_whisper_model.transcribe(
                    audio_path,
                    language=whisper_lang,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )

                detected_lang = getattr(info, "language", source_lang) or source_lang
                prob = getattr(info, "language_probability", 1.0)
                print(f"[⚡ Faster-Whisper] Phát hiện ngôn ngữ: {detected_lang} (độ tin cậy: {prob:.2f})")

                subtitles = []
                for idx, segment in enumerate(segments_iter, start=1):
                    subtitles.append({
                        "index": idx,
                        "start": self._format_timestamp(segment.start),
                        "end": self._format_timestamp(segment.end),
                        "source_text": segment.text.strip(),
                        "translated_text": "",
                        "source_lang": detected_lang,
                    })

                if subtitles:
                    return subtitles
            except Exception as e_fw:
                print(f"[⚠️ Faster-Whisper Lỗi] {e_fw} → Chuyển sang OpenAI Whisper...")

        # 2. Fallback về OpenAI Whisper cũ nếu Faster-Whisper không khả dụng
        fallback_model_name = "medium" if model_name in ["turbo", "large-v3-turbo"] else model_name
        print(f"[🎙️ Whisper Fallback] Tải model '{fallback_model_name}' trên [{self.device}]...")
        if progress_callback:
            progress_callback(f"Đang tải Whisper model '{fallback_model_name}'...", step=2)

        if self.whisper_model is None or self.current_model_name != fallback_model_name:
            try:
                self.whisper_model = whisper.load_model(fallback_model_name, device=self.device)
            except Exception as e:
                print(f"[⚠️] Lỗi GPU {self.device}, chuyển về CPU: {e}")
                self.whisper_model = whisper.load_model(fallback_model_name, device="cpu")
            self.current_model_name = fallback_model_name

        print(f"[🎙️ Whisper] Đang transcribe (lang={whisper_lang or 'auto-detect'})...")
        if progress_callback:
            progress_callback(f"Đang nhận dạng giọng nói (Whisper {fallback_model_name})...", step=2)
        result = self.whisper_model.transcribe(audio_path, language=whisper_lang)

        detected_lang = result.get("language", source_lang)
        print(f"[🎙️ Whisper] Phát hiện ngôn ngữ: {detected_lang}")

        subtitles = []
        for idx, segment in enumerate(result.get("segments", []), start=1):
            subtitles.append({
                "index": idx,
                "start": self._format_timestamp(segment["start"]),
                "end": self._format_timestamp(segment["end"]),
                "source_text": segment["text"].strip(),
                "translated_text": "",
                "source_lang": detected_lang,
            })

        return subtitles

    # ──────────────────────────────────────────────────────────────────────────
    # TRANSLATION
    # ──────────────────────────────────────────────────────────────────────────

    def translate_subtitles(
        self,
        subtitles: list,
        source_lang: str = "auto",
        target_lang: str = "vi",
        engine: str = "gemini",
        ai_model: str = "",
        style: str = "natural",
        video_title: str = "",
        progress_callback=None
    ) -> list:
        """
        Dịch phụ đề từ ngôn ngữ nguồn sang ngôn ngữ đích bằng Gemini AI hoặc Google Translate.

        :param subtitles: Danh sách subtitle objects
        :param source_lang: Ngôn ngữ nguồn (auto = tự detect)
        :param target_lang: Ngôn ngữ đích (vi, en, ko, ja, zh-CN, th, id...)
        :param engine: 'gemini' | 'google'
        :param style: me_va_be | natural | review_sales | funny_viral | storytelling
        :param video_title: Tiêu đề video để Gemini hiểu bối cảnh
        :return: subtitles với translated_text được điền
        """
        if not subtitles:
            return subtitles

        sl = LANG_MAP.get(source_lang, source_lang) if source_lang != "auto" else "auto"
        tl = LANG_MAP.get(target_lang, target_lang)

        cfg = load_gemini_config()

        # ── Thứ tự ưu tiên AI ────────────────────────────────────────────────
        # Mặc định: 9Router trước, hỏng thì tự chuyển sang Gemini 2.5.
        # Người dùng vẫn có thể ép một engine cụ thể từ ô chọn trên giao diện;
        # khi đó engine được chọn đứng đầu chuỗi, engine còn lại làm dự phòng.
        if engine == "google":
            chain = ["google"]
        elif engine == "gemini":
            chain = ["gemini", "openai"]
        else:  # "", "auto", "openai", "chatgpt"
            chain = ["openai", "gemini"]

        # Engine mà người dùng chỉ định đích danh (nếu có), dùng để biết tên model
        # trong ô "Tên Combo" thuộc về bên nào.
        if engine == "gemini":
            explicit_provider = "gemini"
        elif engine in ("openai", "chatgpt"):
            explicit_provider = "openai"
        else:
            explicit_provider = ""
        labels = {"openai": "9Router / ChatGPT", "gemini": "Gemini"}

        def _usable(result):
            # Kết quả chỉ "dùng được" khi mọi câu GỐC CÓ CHỮ đều được dịch ra chữ.
            # Trước đây chỉ đếm độ dài và loại None, nên khi AI trả về đủ số phần
            # tử nhưng vài câu là chuỗi rỗng, bản dịch thiếu vẫn được nhận và câu
            # gốc (thường là tiếng Trung) bị giữ lại mà giao diện báo thành công.
            if not result or len(result) != len(subtitles):
                return False
            for t, sub in zip(result, subtitles):
                if t is None:
                    return False
                if (sub.get("source_text") or "").strip() and not str(t).strip():
                    return False
            return True

        translated = None
        used_provider = ""
        errors = []

        for provider in chain:
            if provider == "google":
                break
            label = labels[provider]
            # ai_model chỉ áp cho đúng engine mà người dùng chỉ định, không
            # đem tên model của bên này gán nhầm cho bên kia.
            model_override = ai_model if (ai_model and provider == explicit_provider) else ""
            try:
                if provider == "gemini":
                    client = GeminiTranslator(model=model_override or None)
                    if not client.is_configured():
                        errors.append(f"{label}: chưa cấu hình API Key")
                        continue
                else:
                    client = OpenAITranslator(
                        api_key=cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY"),
                        model=model_override or cfg.get("openai_model", "gpt-4o-mini"),
                        base_url=cfg.get("openai_base_url"),
                    )
                    if not client.is_configured():
                        errors.append(f"{label}: chưa cấu hình endpoint hoặc API Key")
                        continue

                if progress_callback:
                    progress_callback(f"Đang bản địa hóa kịch bản bằng {label} ({style})...", step=3)
                print(f"[🤖 AI] Dịch kịch bản bằng {label}...")

                result = client.translate_script(subtitles, video_title, tl, style)
                if _usable(result):
                    translated = result
                    used_provider = provider
                    break

                detail = getattr(client, "last_error", "") or "phản hồi không khớp số câu phụ đề"
                errors.append(f"{label}: {detail}")
                print(f"[⚠️ AI] {label} không dùng được: {detail}")

            except Exception as exc:
                errors.append(f"{label}: {exc}")
                print(f"[⚠️ AI] {label} lỗi: {exc}")

        # ── Phao cứu sinh: Google Translate ──────────────────────────────────
        # Chất lượng kém hơn hẳn nên không bao giờ được im lặng. Mỗi câu đều
        # được đánh dấu nguồn dịch để giao diện cảnh báo người dùng.
        if translated is None:
            if chain != ["google"]:
                print(f"[🌐 AI Fallback] Cả 9Router và Gemini đều hỏng ({' | '.join(errors)}). Dùng tạm Google Translate.")
            source_texts = [item.get("source_text", "").strip() for item in subtitles]
            if progress_callback:
                note = "" if chain == ["google"] else " (AI hỏng, chất lượng sẽ kém hơn)"
                progress_callback(f"Đang dịch {len(source_texts)} câu bằng Google Translate{note}...", step=3)
            translated = self._translate_batch(source_texts, source=sl, target=tl)
            used_provider = "google" if chain == ["google"] else "google_fallback"

        warning = " | ".join(errors) if used_provider == "google_fallback" else ""
        for item, t_text in zip(subtitles, translated):
            raw_text = t_text if t_text else item.get("source_text", "")
            item["translated_text"] = clean_social_hashtags(raw_text)
            item["target_lang"] = target_lang
            item["translation_source"] = used_provider
            if warning:
                item["translation_warning"] = warning

        # Giữ nguyên câu thoại tự nhiên hoàn chỉnh, KHÔNG cắt vụn làm hỏng ngữ điệu giọng đọc
        return subtitles

    def _translate_batch(self, texts: list, source: str = "auto", target: str = "vi") -> list:
        """Dịch hàng loạt thông minh bằng bulk chunking + hậu kiểm từng câu (chính xác 100%)."""
        if not texts:
            return []

        tl = LANG_MAP.get(target, target)
        sl = LANG_MAP.get(source, source) if source != "auto" else "auto"

        results = [""] * len(texts)
        chunk_size = 25

        for chunk_start in range(0, len(texts), chunk_size):
            chunk = texts[chunk_start:chunk_start + chunk_size]

            clean_chunk = [t.strip() if t.strip() else "..." for t in chunk]
            combined_text = "\n".join(clean_chunk)

            chunk_translated = []
            try:
                url = (
                    f"https://clients5.google.com/translate_a/t?client=dict-chrome-ex"
                    f"&sl={sl}&tl={tl}&q=" + urllib.parse.quote(combined_text)
                )
                res = requests.get(url, timeout=10).json()
                if isinstance(res, list) and res:
                    raw_block = str(res[0][0] if isinstance(res[0], list) else res[0])
                    lines = raw_block.split("\n")
                    if len(lines) == len(chunk):
                        chunk_translated = [l.strip() if l.strip() != "..." else "" for l in lines]
            except Exception as e:
                print(f"[⚠️ Bulk Translate Error] {e}")

            if len(chunk_translated) != len(chunk):
                chunk_translated = [""] * len(chunk)

            for i, trans_text in enumerate(chunk_translated):
                results[chunk_start + i] = clean_social_hashtags(trans_text)

        # Hậu kiểm: nếu câu nào chưa được dịch hoặc vẫn còn dính chữ Trung (đối với ngôn ngữ không phải Trung/Nhật)
        def has_cjk(text: str) -> bool:
            return any('\u4e00' <= char <= '\u9fff' for char in text)

        is_cjk_target = tl in ("zh", "zh-CN", "zh-TW", "ja")
        for i, (orig, trans) in enumerate(zip(texts, results)):
            if not orig.strip():
                results[i] = ""
                continue

            if not trans or (not is_cjk_target and has_cjk(trans)):
                try:
                    url = (
                        f"https://clients5.google.com/translate_a/t?client=dict-chrome-ex"
                        f"&sl=auto&tl={tl}&q=" + urllib.parse.quote(orig.strip())
                    )
                    res = requests.get(url, timeout=5).json()
                    if isinstance(res, list) and res:
                        t_single = str(res[0][0] if isinstance(res[0], list) else res[0]).strip()
                        results[i] = clean_social_hashtags(t_single if t_single else orig)
                    else:
                        results[i] = clean_social_hashtags(trans) if trans else orig
                except Exception:
                    results[i] = clean_social_hashtags(trans) if trans else orig

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # VOICE-OVER (Edge TTS)
    # ──────────────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────────────
    # CHẾ ĐỘ NEO CỨNG MỐC NGUỒN (source_anchored)
    #
    # Chạy song song với luồng cũ, chưa thay mặc định, đúng như mục 15 của
    # KE_HOACH_DONG_BO_VOICE_PHU_DE.md. Khác biệt cốt lõi so với luồng cũ: không
    # câu nào được đẩy câu kế tiếp ra sau, nên lời và phụ đề không trôi khỏi hình.
    # ──────────────────────────────────────────────────────────────────────────

    def _anchored_tts(self, text: str, voice: str, rate: str, pitch: str, out_path: str) -> bool:
        """
        Tạo giọng cho một câu, có thử lại, KHÔNG tự đổi sang giọng khác.

        File đã có sẵn thì dùng lại ngay. Vì tên file đặt theo mã băm của nội
        dung, sửa hai câu rồi ép lại chỉ tạo lại đúng hai câu đó, và mất mạng
        giữa chừng cũng chạy tiếp được chứ không phải làm lại từ đầu.
        """
        if os.path.exists(out_path) and os.path.getsize(out_path) > 200:
            return True
        from tts_engine import generate_audio_sync
        for attempt in range(3):
            try:
                if generate_audio_sync(text=text, voice=voice, output_path=out_path,
                                       rate=rate, pitch=pitch):
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 200:
                        self.tts_engine._trim_audio_silence_inplace(out_path)
                        return True
            except Exception as exc:
                print(f"[⚠️ TTS neo cứng] lần {attempt + 1}: {exc}")
            time.sleep(0.8 * (attempt + 1))
        return False

    def _anchored_speed(self, src: str, dst: str, factor: float) -> bool:
        """Ép nhanh một câu bằng atempo. Có sẵn kết quả thì dùng lại."""
        if os.path.exists(dst) and os.path.getsize(dst) > 200:
            return True
        try:
            res = subprocess.run(
                [self.ffmpeg_cmd, "-y", "-v", "error", "-i", src,
                 "-filter:a", f"atempo={factor:.3f}", "-vn", dst],
                capture_output=True, timeout=60)
            return res.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 200
        except Exception as exc:
            print(f"[⚠️ atempo] {exc}")
            return False

    def _mix_anchored_track(self, video_id: str, mix_plan: list) -> str:
        """
        Ghép các file giọng vào đúng mốc neo của chúng thành một luồng tiếng.

        Chia lô để tránh vượt giới hạn số input của FFmpeg trên video nhiều câu.
        """
        if not mix_plan:
            return None
        output = os.path.join(self.download_dir, f"{video_id}_voice.mp3")
        batch_size = 30
        batches = []

        for start in range(0, len(mix_plan), batch_size):
            chunk = mix_plan[start:start + batch_size]
            inputs, filters, labels = [], [], []
            for i, item in enumerate(chunk):
                inputs.extend(["-i", item["path"]])
                filters.append(f"[{i}:a]adelay={item['delay_ms']}|{item['delay_ms']}[d{i}]")
                labels.append(f"[d{i}]")
            out = (output if len(mix_plan) <= batch_size
                   else os.path.join(self.download_dir, f"{video_id}_anchor_batch{start}.mp3"))
            cmd = [self.ffmpeg_cmd, "-y", "-v", "error"] + inputs + [
                "-filter_complex",
                ";".join(filters) + ";" + "".join(labels)
                + f"amix=inputs={len(chunk)}:duration=longest:normalize=0:dropout_transition=0[aout]",
                "-map", "[aout]", "-c:a", "mp3", "-b:a", "192k", out]
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            batches.append(out)

        if len(batches) > 1:
            inputs, labels = [], []
            for i, b in enumerate(batches):
                inputs.extend(["-i", b])
                labels.append(f"[{i}:a]")
            subprocess.run(
                [self.ffmpeg_cmd, "-y", "-v", "error"] + inputs + [
                    "-filter_complex",
                    "".join(labels) + f"amix=inputs={len(batches)}:duration=longest:normalize=0[aout]",
                    "-map", "[aout]", "-c:a", "mp3", "-b:a", "192k", output],
                check=True, capture_output=True, timeout=600)
            for b in batches:
                try:
                    os.remove(b)
                except OSError:
                    pass
        return output

    def housekeeping(self, keep_videos: int = 8) -> dict:
        """
        Dọn ổ đĩa lúc khởi động: chỉ giữ file trung gian của vài video gần nhất.

        Video thành phẩm (_hardsub.mp4) và phụ đề luôn được giữ. Thứ bị xoá là
        file nặng mà tạo lại được: bản tải về, wav tách âm, stem no_vocals và
        thư mục giọng theo câu. Trước đây thư mục downloads từng phình tới 5.9GB.
        """
        import glob
        import re as _re
        HEAVY = ["{vid}.mp4", "{vid}.wav", "{vid}_orig_hq.wav", "{vid}_no_vocals.wav"]
        try:
            names = os.listdir(self.download_dir)
        except OSError:
            return {"freed_mb": 0, "removed": 0}

        # Gom theo video_id, xếp theo lần sửa gần nhất. Mã video TikTok toàn số
        # nhưng mã YouTube có cả chữ và dấu, nên lọc theo chữ số thì mọi file
        # trung gian của video YouTube nằm lại mãi. Chỉ những mã thật sự có file
        # trung gian mang tên đó mới bị đụng tới, nên nới rộng ở đây là an toàn.
        seen = {}
        for n in names:
            vid = n.split("_")[0].split(".")[0]
            if not vid or not _re.fullmatch(r"[A-Za-z0-9-]{6,}", vid):
                continue
            path = os.path.join(self.download_dir, n)
            try:
                seen[vid] = max(seen.get(vid, 0), os.path.getmtime(path))
            except OSError:
                continue

        stale = [v for v, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[keep_videos:]]
        freed, removed = 0, 0
        for vid in stale:
            for pattern in HEAVY:
                for path in glob.glob(os.path.join(self.download_dir, pattern.format(vid=vid))):
                    try:
                        freed += os.path.getsize(path)
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
            folder = os.path.join(self.download_dir, f"anchor_{vid}")
            if os.path.isdir(folder):
                for root, _, files in os.walk(folder):
                    for f in files:
                        try:
                            freed += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1

        tam = self.don_file_tam()
        freed += tam["freed_bytes"]
        removed += tam["removed"]

        mb = round(freed / 1024 / 1024, 1)
        if removed:
            print(f"[🧹 Dọn ổ đĩa] Xoá {removed} mục của {len(stale)} video cũ, giải phóng {mb} MB")
        return {"freed_mb": mb, "removed": removed, "stale_videos": len(stale)}

    # Những thứ tool tự sinh ra rồi không bao giờ cần lại: đoạn nghe thử giọng,
    # bản chạy thử để xem trước, file đo thời lượng câu. Housekeeping cũ gom
    # file theo mã video nên không thấy chúng, và chúng nằm lại mãi.
    TIEN_TO_TAM = ("preview_", "lvpreview", "timing_", "tmp_", "clean_tmp_", "test_tmp_")
    TAM_GIU_GIO = 24

    def don_file_tam(self, gio=None):
        """Xoá file tạm cũ hơn một ngày. Video thành phẩm và phụ đề không đụng tới."""
        moc = (gio or time.time()) - self.TAM_GIU_GIO * 3600
        freed, removed, ten = 0, 0, []
        try:
            names = os.listdir(self.download_dir)
        except OSError:
            return {"freed_bytes": 0, "removed": 0, "files": []}
        for n in names:
            if not n.startswith(self.TIEN_TO_TAM):
                continue
            path = os.path.join(self.download_dir, n)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) >= moc:
                    continue
                size = os.path.getsize(path)
                os.remove(path)
            except OSError:
                continue
            freed += size
            removed += 1
            ten.append(n)
        return {"freed_bytes": freed, "removed": removed, "files": ten}

    def xem_truoc_file_tam(self, gio=None):
        """Sẽ xoá bao nhiêu, nặng bao nhiêu. Dùng để hỏi người dùng bằng số thật."""
        moc = (gio or time.time()) - self.TAM_GIU_GIO * 3600
        tong, dem = 0, 0
        try:
            names = os.listdir(self.download_dir)
        except OSError:
            return {"bytes": 0, "count": 0}
        for n in names:
            if not n.startswith(self.TIEN_TO_TAM):
                continue
            path = os.path.join(self.download_dir, n)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < moc:
                    tong += os.path.getsize(path)
                    dem += 1
            except OSError:
                continue
        return {"bytes": tong, "count": dem}

    def _purge_old_anchor_dirs(self, keep_video_id: str, keep_recent: int = 3) -> None:
        """Chỉ giữ thư mục giọng của vài video gần nhất, xoá phần còn lại."""
        try:
            dirs = [d for d in os.listdir(self.download_dir)
                    if d.startswith("anchor_")
                    and os.path.isdir(os.path.join(self.download_dir, d))]
        except OSError:
            return
        current = f"anchor_{keep_video_id}"
        others = [d for d in dirs if d != current]
        others.sort(key=lambda d: os.path.getmtime(os.path.join(self.download_dir, d)),
                    reverse=True)
        for stale in others[keep_recent - 1:]:
            shutil.rmtree(os.path.join(self.download_dir, stale), ignore_errors=True)
            print(f"[🗑️ Dọn] Đã xoá thư mục giọng cũ: {stale}")

    def _detect_silences(self, media_path: str, noise_db: int = -30, min_dur: float = 0.35) -> list:
        """
        Các quãng IM LẶNG THẬT trong audio, trả về [(start_ms, end_ms)].

        Dùng để một câu được mượn tới hết quãng nghỉ có thật của video thay vì
        trần cứng 500ms, nhờ đó bớt câu bị coi là lố oan.
        """
        if not media_path or not os.path.exists(media_path):
            return []
        try:
            res = subprocess.run(
                [self.ffmpeg_cmd, "-i", media_path, "-af",
                 f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
                capture_output=True, text=True, timeout=120)
        except Exception as exc:
            print(f"[⚠️ silencedetect] {exc}")
            return []
        silences, cur = [], None
        for line in res.stderr.splitlines():
            if "silence_start:" in line:
                try:
                    cur = float(line.split("silence_start:")[1].strip())
                except ValueError:
                    cur = None
            elif "silence_end:" in line and cur is not None:
                try:
                    end = float(line.split("silence_end:")[1].split("|")[0].strip())
                    silences.append((int(cur * 1000), int(end * 1000)))
                except ValueError:
                    pass
                cur = None
        return silences

    def _detect_scene_cuts(self, video_path: str, threshold: float = 0.4) -> list:
        """Mốc cắt cảnh hình (ms), để cảnh báo khi giọng vắt qua một cú cắt."""
        if not video_path or not os.path.exists(video_path):
            return []
        try:
            res = subprocess.run(
                [self.ffmpeg_cmd, "-i", video_path, "-filter:v",
                 f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                capture_output=True, text=True, timeout=180)
        except Exception as exc:
            print(f"[⚠️ scenedetect] {exc}")
            return []
        cuts = []
        for line in res.stderr.splitlines():
            if "pts_time:" in line and "showinfo" in line:
                try:
                    cuts.append(int(float(line.split("pts_time:")[1].split()[0]) * 1000))
                except (ValueError, IndexError):
                    pass
        return sorted(set(cuts))

    def _video_sync_analysis(self, video_id: str, video_path: str) -> tuple:
        """
        (silences, scene_cuts) của video, tính một lần rồi cache theo lần sửa file.

        Bảng Bác sĩ đồng bộ gọi lập kế hoạch nhiều lần; quét silence và cắt cảnh
        khá nặng nên chỉ chạy lần đầu cho mỗi video.
        """
        if not hasattr(self, "_analysis_cache"):
            self._analysis_cache = {}
        try:
            mtime = os.path.getmtime(video_path)
        except OSError:
            mtime = 0
        key = (video_id, video_path, mtime)
        if self._analysis_cache.get("key") == key:
            return self._analysis_cache["silences"], self._analysis_cache["scene_cuts"]
        # Dò thẳng trên đúng file đang dùng (có thể là đoạn đã cắt), không dùng
        # wav của video đầy đủ vì mốc khoảng lặng sẽ lệch.
        silences = self._detect_silences(video_path)
        scene_cuts = self._detect_scene_cuts(video_path)
        self._analysis_cache.update(key=key, silences=silences, scene_cuts=scene_cuts)
        return silences, scene_cuts

    def generate_timeline_visuals(self, video_id: str) -> dict:
        """
        Dựng dải ảnh thu nhỏ và ảnh sóng âm của video, để timeline nhìn ra editor
        thay vì chỉ toàn thanh màu trừu tượng.

        Trả về đường dẫn hai ảnh PNG cùng thời lượng. Cache theo lần sửa video nên
        chỉ dựng lại khi video đổi; timeline tự co giãn ảnh theo mức phóng to.
        """
        video = os.path.join(self.download_dir, f"{video_id}.mp4")
        if not os.path.exists(video):
            video = os.path.join(self.download_dir, f"{video_id}_hardsub.mp4")
        if not os.path.exists(video):
            return {"success": False, "error": "Không tìm thấy video để dựng ảnh timeline."}

        dur = self._get_media_duration_sec(video)
        if dur <= 0:
            dur = 10.0
        strip = os.path.join(self.download_dir, f"{video_id}_filmstrip.png")
        wave = os.path.join(self.download_dir, f"{video_id}_wave.png")
        try:
            vmtime = os.path.getmtime(video)
        except OSError:
            vmtime = 0

        def fresh(p):
            return os.path.exists(p) and os.path.getsize(p) > 500 and os.path.getmtime(p) >= vmtime

        n = min(60, max(8, int(round(dur))))
        if not fresh(strip):
            try:
                subprocess.run(
                    [self.ffmpeg_cmd, "-y", "-v", "error", "-i", video, "-vf",
                     f"fps={n}/{dur:.3f},scale=-1:54,tile={n}x1", "-frames:v", "1", strip],
                    check=True, capture_output=True, timeout=120)
            except Exception as exc:
                print(f"[⚠️ Filmstrip] {exc}")
        has_audio = True
        if not fresh(wave):
            try:
                subprocess.run(
                    [self.ffmpeg_cmd, "-y", "-v", "error", "-i", video, "-filter_complex",
                     "showwavespic=s=1400x60:colors=#7dd3fc", "-frames:v", "1", wave],
                    check=True, capture_output=True, timeout=120)
            except Exception as exc:
                print(f"[⚠️ Waveform] {exc}")
                has_audio = False

        stamp = int(vmtime)
        out = {"success": True, "duration": round(dur, 3), "frames": n}
        if os.path.exists(strip):
            out["filmstrip_url"] = f"/downloads/{video_id}_filmstrip.png?t={stamp}"
        if os.path.exists(wave) and has_audio:
            out["waveform_url"] = f"/downloads/{video_id}_wave.png?t={stamp}"
        return out

    def apply_overlays(self, video_path: str, overlays: list) -> str:
        """
        Ghép ảnh/logo overlay lên video bằng MỘT lượt riêng sau khi đã ép sub.

        Tách khỏi bộ dựng ffmpeg của burn để không phải đụng vào chuỗi input phức
        tạp ở đó. Mỗi overlay là {filename, pos, width_pct, start, end}. Vị trí và
        thời điểm hiện đặt bằng biểu thức, không cần biết trước kích thước ảnh.
        """
        items = []
        for ov in overlays or []:
            name = str(ov.get("filename", ""))
            # Chỉ nhận đúng tên file overlay do tool tự đặt, chặn đường dẫn lạ.
            if not re.fullmatch(r"overlay_[A-Za-z0-9]+\.(png|jpe?g|webp)", name):
                continue
            path = os.path.join(self.download_dir, name)
            if os.path.isfile(path):
                items.append((path, ov))
        if not items:
            return video_path

        w_in, h_in, _ = self.get_video_dimensions(video_path)
        margin = "(main_w*0.03)"
        pos_expr = {
            "tl": (margin, margin),
            "tr": (f"main_w-overlay_w-{margin}", margin),
            "bl": (margin, f"main_h-overlay_h-{margin}"),
            "br": (f"main_w-overlay_w-{margin}", f"main_h-overlay_h-{margin}"),
            "tc": ("(main_w-overlay_w)/2", margin),
            "bc": ("(main_w-overlay_w)/2", f"main_h-overlay_h-{margin}"),
            "center": ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2"),
        }

        cmd = [self.ffmpeg_cmd, "-y", "-v", "error", "-i", video_path]
        for path, _ in items:
            cmd += ["-i", path]
        parts, base = [], "[0:v]"
        for i, (_, ov) in enumerate(items, start=1):
            try:
                wpct = min(0.9, max(0.03, float(ov.get("width_pct", 0.2))))
            except (TypeError, ValueError):
                wpct = 0.2
            ow = max(16, int(w_in * wpct))
            parts.append(f"[{i}:v]scale={ow}:-1[ov{i}]")
            x, y = pos_expr.get(str(ov.get("pos", "tr")), pos_expr["tr"])
            enable = ""
            s, e = ov.get("start"), ov.get("end")
            if s not in (None, "") or e not in (None, ""):
                try:
                    s = float(s or 0)
                    e = float(e) if e not in (None, "") else 10 ** 9
                    enable = f":enable='between(t,{s:.3f},{e:.3f})'"
                except (TypeError, ValueError):
                    enable = ""
            out = f"[v{i}]"
            parts.append(f"{base}[ov{i}]overlay=x={x}:y={y}{enable}{out}")
            base = out
        final_label = base
        out_path = os.path.splitext(video_path)[0] + ".ovl.mp4"
        cmd += ["-filter_complex", ";".join(parts), "-map", final_label, "-map", "0:a?",
                "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-c:a", "copy", out_path]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            os.replace(out_path, video_path)
        except Exception as exc:
            print(f"[⚠️ Overlay] {exc}")
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
        return video_path

    def suggest_trim(self, video_id: str) -> dict:
        """
        Đề xuất điểm cắt: bỏ đoạn IM ở đầu và cuối video (dead air), và hít điểm
        cắt vào mốc cắt cảnh gần nhất để không cắt giữa một cảnh.

        Chỉ gợi ý; người dùng nhìn rồi chỉnh hoặc bỏ. Tái dùng bộ dò khoảng lặng
        và cắt cảnh đã có (đã cache theo video).
        """
        video = os.path.join(self.download_dir, f"{video_id}.mp4")
        if not os.path.exists(video):
            video = os.path.join(self.download_dir, f"{video_id}_hardsub.mp4")
        if not os.path.exists(video):
            return {"success": False, "error": "Không tìm thấy video."}

        dur = self._get_media_duration_sec(video)
        if dur <= 0:
            return {"success": False, "error": "Không đọc được thời lượng video."}
        dur_ms = int(dur * 1000)
        silences, scene_cuts = self._video_sync_analysis(video_id, video)

        # Đoạn im ngay đầu và ngay cuối.
        lead = max((s1 for s0, s1 in silences if s0 <= 200), default=0)
        tail = min((s0 for s0, s1 in silences if s1 >= dur_ms - 200), default=dur_ms)
        start_ms = max(0, lead)
        end_ms = min(dur_ms, tail)

        # Hít vào mốc cắt cảnh gần nhất trong 700ms.
        def snap(t):
            best = None
            for c in scene_cuts:
                if abs(c - t) <= 700 and (best is None or abs(c - t) < abs(best - t)):
                    best = c
            return best if best is not None else t
        start_ms, end_ms = snap(start_ms), snap(end_ms)

        # Đoạn đề xuất quá ngắn hoặc gần như cả video thì coi như không cần cắt.
        if end_ms - start_ms < 1000 or (start_ms <= 150 and end_ms >= dur_ms - 150):
            return {"success": True, "duration": round(dur, 3),
                    "trim_start": 0, "trim_end": round(dur, 3),
                    "head_ms": 0, "tail_ms": 0, "changed": False}

        return {"success": True, "duration": round(dur, 3),
                "trim_start": round(start_ms / 1000, 3),
                "trim_end": round(end_ms / 1000, 3),
                "head_ms": start_ms, "tail_ms": dur_ms - end_ms, "changed": True}

    def _run_anchored_sync(
        self,
        video_id: str,
        subtitles: list,
        voice: str = "vi-VN-HoaiMyNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        max_speedup: float = tp.DEFAULT_MAX_SPEEDUP,
        allow_shorten: bool = True,
        overrides: dict = None,
        autofix: bool = False,
        shorten_cues: list = None,
        video_path: str = None,
        progress_callback=None,
    ):
        """
        Phần LÕI của chế độ neo cứng, KHÔNG render video.

        Gom câu, tạo giọng, đo thật, ép nhanh, nhờ AI rút gọn, và (nếu autofix)
        tự tính bộ ghi đè sửa các câu còn lố rồi chạy lại một lượt. Dọn file nháp,
        giữ đúng file bản cuối. Đây là pha Kế hoạch, tách khỏi pha Render để bảng
        Bác sĩ đồng bộ lặp được phần nhẹ mà không đụng phần encode video.

        shorten_cues: danh sách cue_id mà người dùng bấm "rút gọn tự động". Các câu
        này được nhờ AI viết gọn còn vừa ngân sách rồi ghim chữ, dù toggle rút gọn
        chung có tắt. Nếu rút xong nghi mất dữ kiện thì câu bị tô cam.

        Trả về (result, work_dir, effective_overrides, video_ms) với result gồm
        `cues` đã chốt timeline và `report`. `video_path` cho phép chạy trên đoạn
        đã cắt thay vì video đầy đủ.
        """
        video_path = video_path or os.path.join(self.download_dir, f"{video_id}.mp4")
        video_ms = int(self._get_media_duration_sec(video_path) * 1000)

        # Đồng hồ từng chặng, ghi thẳng vào audit.log để biết lần chạy chậm là
        # chậm ở đâu: quét video, khớp giọng, hay các lượt viết lại thêm.
        import json as _json
        import logging as _logging
        _clock = time.time()

        def _phase(name):
            nonlocal _clock
            now = time.time()
            _logging.info("sync_stage %s", _json.dumps(
                {"video": video_id, "name": name, "seconds": round(now - _clock, 1)},
                ensure_ascii=False))
            _clock = now

        cues = tp.group_into_cues(subtitles)
        if not cues:
            raise RuntimeError("Không có câu thoại nào để lồng tiếng.")

        def emit(msg):
            print(f"[⚓ Neo cứng] {msg}")
            if progress_callback:
                try:
                    progress_callback(msg, step=3)
                except TypeError:
                    progress_callback(msg)

        work_dir = os.path.join(self.download_dir, f"anchor_{video_id}")
        os.makedirs(work_dir, exist_ok=True)
        self._purge_old_anchor_dirs(keep_video_id=video_id)

        import hashlib

        def cue_path(cue_id, revision, text):
            key = hashlib.md5(f"{text}|{voice}|{rate}|{pitch}".encode("utf-8")).hexdigest()[:12]
            return os.path.join(work_dir, f"{key}.mp3")

        rewrite_fn = None
        if allow_shorten:
            rewrite_fn = make_shortener(load_gemini_config, progress=emit)

        effective = dict(overrides or {})
        # Khoảng lặng thật để mượn đúng quãng nghỉ, và mốc cắt cảnh để cảnh báo
        # giọng vắt qua cú cắt hình. Tính một lần, cache theo video.
        silences, scene_cuts = self._video_sync_analysis(video_id, video_path)
        _phase("quét khoảng lặng và cắt cảnh")
        emit(f"Gom {len(subtitles)} dòng phụ đề thành {len(cues)} câu nói, "
             f"video dài {video_ms / 1000:.1f}s "
             f"({len(silences)} quãng lặng, {len(scene_cuts)} cắt cảnh)")

        def synth(ov):
            return se.synthesize_anchored(
                cues, video_ms,
                tts_fn=self._anchored_tts,
                measure_fn=lambda p: int(self._get_media_duration_sec(p) * 1000),
                speed_fn=self._anchored_speed,
                rewrite_fn=rewrite_fn,
                path_fn=cue_path,
                voice=voice, rate=rate, pitch=pitch,
                max_speedup=max_speedup,
                overrides=ov,
                silences=silences,
                scene_cuts=scene_cuts,
                progress=emit,
            )

        result = synth(effective)
        _phase("viết gọn và tạo giọng lượt chính")

        # Rút gọn AI cho ĐÚNG những câu người dùng bấm nút, kể cả khi toggle rút
        # gọn chung đang tắt. Ghim chữ mới để render tái dựng đúng, và tô cam nếu
        # nghi mất dữ kiện.
        shortened_ids = []
        if shorten_cues:
            manual_shortener = rewrite_fn or make_shortener(load_gemini_config, progress=emit)
            by_id = {c["cue_id"]: c for c in result["cues"]}
            reqs = []
            for cid in shorten_cues:
                s = by_id.get(cid)
                if not s:
                    continue
                reqs.append(tp.build_shorten_request({
                    "cue_id": cid, "text": s["final_text"],
                    "measured_ms": s.get("measured_ms"), "budget_ms": s.get("budget_ms"),
                    "target_syllables": tp.max_syllables_for(
                        s.get("budget_ms") or 0, speedup=tp.DEFAULT_MAX_SPEEDUP),
                }))
            out = {}
            if reqs:
                emit(f"Đang nhờ AI rút gọn {len(reqs)} câu bạn chọn...")
                try:
                    out = manual_shortener(reqs) or {}
                except Exception as exc:
                    emit(f"Rút gọn từng câu lỗi: {exc}")
            for cid in shorten_cues:
                new_text = (out.get(cid) or "").strip()
                s = by_id.get(cid)
                if new_text and s and tp.count_syllables(new_text) < tp.count_syllables(s["final_text"]):
                    effective[cid] = {**effective.get(cid, {}), "text": new_text}
                    shortened_ids.append(cid)
            if shortened_ids:
                result = synth(effective)
                _phase("chạy lại sau khi rút gọn tay")

        if autofix:
            new_ov = se.compute_autofix_overrides(result["report"]["cues"], effective)
            if new_ov != effective:
                emit("Đang tự sửa các câu còn lố...")
                effective = new_ov
                result = synth(effective)
                _phase("chạy lại để tự sửa câu lố")

        # Câu vừa rút gọn tay bị ghim nên synth bỏ qua kiểm dữ kiện. Chấm lại ở đây
        # so với câu GỐC rồi dựng lại báo cáo, để câu nghi mất số/phủ định hiện cam.
        if shortened_ids:
            by_id = {c["cue_id"]: c for c in result["cues"]}
            for cid in shortened_ids:
                s = by_id.get(cid)
                if s:
                    s["fact_warnings"] = se.check_facts_preserved(s["original_text"], s["final_text"])
            result["report"] = se.build_report(result["cues"], video_ms, voice, max_speedup,
                                               effective, scene_cuts)

        report = result["report"]
        emit(report["summary_text"])

        # Giữ đúng file của bản cuối để lần sau dùng lại, xoá các bản nháp giữa
        # chừng. Không dọn thì mỗi lần ép lại là thư mục phình thêm một lượt.
        keep = {os.path.basename(c["audio_path"]) for c in result["cues"] if c.get("audio_path")}
        removed = 0
        for name in os.listdir(work_dir):
            if name not in keep:
                try:
                    os.remove(os.path.join(work_dir, name))
                    removed += 1
                except OSError:
                    pass
        if removed:
            emit(f"Đã dọn {removed} file giọng nháp, giữ lại {len(keep)} file để dùng lại lần sau")

        report["work_dir"] = work_dir
        return result, work_dir, effective, video_ms

    def _cue_audio_url(self, audio_path):
        """Đường dẫn web để nghe thử giọng của một câu, hoặc rỗng nếu không có."""
        if not audio_path or not os.path.isfile(audio_path):
            return ""
        rel = os.path.relpath(audio_path, self.download_dir)
        if rel.startswith(".."):
            return ""
        return "/downloads/" + rel.replace(os.sep, "/")

    def plan_anchored_sync(
        self,
        video_id: str,
        subtitles: list,
        voice: str = "vi-VN-HoaiMyNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        max_speedup: float = tp.DEFAULT_MAX_SPEEDUP,
        allow_shorten: bool = True,
        overrides: dict = None,
        autofix: bool = False,
        shorten_cues: list = None,
        trim_start=None,
        trim_end=None,
        progress_callback=None,
    ):
        """
        Pha Kế hoạch cho bảng Bác sĩ đồng bộ. Chạy khớp giọng và trả báo cáo từng
        câu kèm link nghe thử, KHÔNG render video. Nếu có trim thì lập kế hoạch
        trên đúng đoạn đã cắt để khớp bản xuất.

        Trả về dict gồm `report` (mảng cues đủ trạng thái), `export_overrides` là
        bộ ghi đè sẵn sàng để truyền cho lúc render nhằm tái dựng ĐÚNG kế hoạch
        này (ghim chữ cuối từng câu để render không gọi lại AI rút gọn), và
        video_ms.
        """
        video_path = None
        if trim_start or trim_end:
            video_path, subtitles = self.prepare_trimmed_source(
                video_id, subtitles, trim_start, trim_end)
        result, work_dir, effective, video_ms = self._run_anchored_sync(
            video_id, subtitles, voice=voice, rate=rate, pitch=pitch,
            max_speedup=max_speedup, allow_shorten=allow_shorten,
            overrides=overrides, autofix=autofix, shorten_cues=shorten_cues,
            video_path=video_path, progress_callback=progress_callback,
        )
        report = result["report"]

        # Gắn link nghe thử cho từng câu.
        by_id = {c["cue_id"]: c for c in result["cues"]}
        for info in report["cues"]:
            src = by_id.get(info["cue_id"], {})
            info["audio_url"] = self._cue_audio_url(src.get("audio_path"))

        # Bộ ghi đè để render tái dựng đúng kế hoạch: ghim chữ cuối của MỌI câu
        # (chặn AI rút gọn lại ở render, bước duy nhất không tất định), kèm trần
        # ép nhanh riêng và cờ cho phép lố mà người dùng đã chốt.
        export_overrides = {}
        for c in result["cues"]:
            cid = c["cue_id"]
            entry = {"text": c["final_text"]}
            ov = effective.get(cid, {})
            if ov.get("max_speedup"):
                entry["max_speedup"] = ov["max_speedup"]
            if ov.get("allow_overflow"):
                entry["allow_overflow"] = True
            export_overrides[cid] = entry

        return {
            "success": True,
            "report": report,
            "export_overrides": export_overrides,
            "video_ms": video_ms,
        }

    def generate_voiceover_anchored(
        self,
        video_id: str,
        subtitles: list,
        voice: str = "vi-VN-HoaiMyNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        max_speedup: float = tp.DEFAULT_MAX_SPEEDUP,
        allow_shorten: bool = True,
        overrides: dict = None,
        video_path: str = None,
        progress_callback=None,
    ):
        """
        Tạo lồng tiếng theo chế độ neo cứng mốc nguồn, có mix thành một luồng tiếng.

        Trả về (đường_dẫn_audio, phụ_đề_hiển_thị_mới, báo_cáo).
        Phụ đề hiển thị được sinh RA TỪ timeline giọng đã chốt, không phải ngược lại.

        Khi render từ bảng Bác sĩ, `overrides` ghim chữ cuối từng câu nên hàm này
        KHÔNG gọi lại AI rút gọn, bảo đảm video khớp đúng bản người dùng đã duyệt.
        """
        result, work_dir, effective, video_ms = self._run_anchored_sync(
            video_id, subtitles, voice=voice, rate=rate, pitch=pitch,
            max_speedup=max_speedup, allow_shorten=allow_shorten,
            overrides=overrides, autofix=False, video_path=video_path,
            progress_callback=progress_callback,
        )
        report = result["report"]
        voice_path = self._mix_anchored_track(video_id, se.build_mix_plan(result["cues"]))
        display_subs = se.build_display_subtitles(result["cues"])
        return voice_path, display_subs, report

    def generate_voiceover(
        self,
        video_id: str,
        subtitles: list,
        voice: str = "vi-VN-HoaiMyNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        characters: object = None,
        multi_voice: bool = True,
        sync_mode: str = "triple_sync",
        max_atempo: float = 1.35,
        return_sync_info: bool = False
    ):
        """
        Tạo voice-over đa ngôn ngữ dùng Microsoft Edge TTS với Triple-Sync và Đa nhân vật (Multi-Voice).

        :param video_id: ID video
        :param subtitles: Danh sách subtitle objects (cần trường 'translated_text')
        :param voice: Tên giọng Edge TTS mặc định
        :param rate: Tốc độ đọc (+0%, +10%, -10%...)
        :param pitch: Cao độ giọng đọc (+0Hz, +25Hz, -15Hz...)
        :param characters: Cấu hình nhân vật {id: {voice, pitch, rate}}
        :param multi_voice: True để đọc theo giọng từng nhân vật
        :param sync_mode: "triple_sync" | "standard"
        :param max_atempo: Tốc độ tăng tốc tối đa của atempo
        :param return_sync_info: Trả về tuple nếu True
        :return: Đường dẫn file audio hoặc tuple
        """
        return self.tts_engine.generate_voiceover(
            video_id=video_id,
            subtitles=subtitles,
            voice=voice,
            text_field="translated_text",
            rate=rate,
            pitch=pitch,
            characters=characters,
            multi_voice=multi_voice,
            sync_mode=sync_mode,
            max_atempo=max_atempo,
            return_sync_info=return_sync_info
        )

    def _get_media_duration_sec(self, file_path: str) -> float:
        """Đo thời lượng video hoặc audio tính theo giây bằng ffprobe/ffmpeg."""
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            val = res.stdout.strip()
            if val:
                return float(val)
        except Exception:
            pass
        try:
            cmd = [self.ffmpeg_cmd, "-i", file_path]
            res = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
            for line in res.stderr.split("\n"):
                if "Duration:" in line:
                    dur_str = line.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = dur_str.split(":")
                    return float(h) * 3600 + float(m) * 60 + float(s)
        except Exception:
            pass
        return 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # VIDEO DIMENSIONS & ASPECT RATIO
    # ──────────────────────────────────────────────────────────────────────────

    def get_video_dimensions(self, video_path: str) -> tuple:
        """Lấy chiều rộng, chiều cao và kiểm tra xem video có phải size ngang (w, h, is_landscape) không."""
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                video_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            info = json.loads(res.stdout)
            streams = info.get("streams", [])
            if streams:
                w = int(streams[0].get("width", 1080))
                h = int(streams[0].get("height", 1920))
                # is_landscape nếu width > height hoặc tỷ lệ > 9:16 (ví dụ 16:9, 4:3, 1:1)
                is_landscape = (w / h) > (9 / 16 + 0.05)
                return w, h, is_landscape
        except Exception as e:
            print(f"[⚠️ ffprobe Warning] Không lấy được kích thước video: {e}")
        return 1080, 1920, False

    def generate_thumbnails(
        self,
        video_id: str,
        count: int = 3,
        overlay_title: str = None,
        target_format: str = "16:9"
    ) -> dict:
        """
        Trích xuất các khung hình chất lượng cao làm Thumbnail cho YouTube Shorts / Video Dài.
        - Trích xuất 3 khung hình tại 15%, 35%, 60% thời lượng video.
        - Tạo thêm 1 ảnh bìa Thumbnail với dải chữ tiêu đề giật tít màu vàng/đỏ nổi bật (dành cho YouTube Video Dài).
        """
        video_path = os.path.join(self.download_dir, f"{video_id}_hardsub.mp4")
        if not os.path.exists(video_path):
            video_path = os.path.join(self.download_dir, f"{video_id}.mp4")
        if not os.path.exists(video_path):
            return {"success": False, "error": f"Không tìm thấy video {video_id} để tạo thumbnail."}

        total_dur = self._get_media_duration_sec(video_path)
        if total_dur <= 0:
            total_dur = 10.0

        percentages = [0.15, 0.35, 0.60] if count == 3 else [i / (count + 1) for i in range(1, count + 1)]
        thumbs = []

        for idx, pct in enumerate(percentages, 1):
            t_sec = round(total_dur * pct, 2)
            out_name = f"{video_id}_thumb_{idx}.jpg"
            out_path = os.path.join(self.download_dir, out_name)

            cmd = [
                self.ffmpeg_cmd, '-y',
                '-ss', str(t_sec),
                '-i', video_path,
                '-vframes', '1',
                '-q:v', '2',
                out_path
            ]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 500:
                    m, sc = divmod(int(t_sec), 60)
                    thumbs.append({
                        "id": idx,
                        "filename": out_name,
                        "url": f"/downloads/{out_name}",
                        "timestamp_sec": t_sec,
                        "timestamp_str": f"{m:02d}:{sc:02d}"
                    })
            except Exception as e:
                print(f"[⚠️ Thumbnail #{idx} Error] {e}")

        cover_info = None
        if thumbs:
            base_thumb = os.path.join(self.download_dir, thumbs[0]["filename"])
            cover_name = f"{video_id}_thumb_cover.jpg"
            cover_path = os.path.join(self.download_dir, cover_name)

            clean_title = (overlay_title or f"Video #{video_id}").replace("'", "").replace(":", " -")[:60]
            
            font_candidates = [
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/Library/Fonts/Arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
                "C:/Windows/Fonts/arial.ttf"
            ]
            font_arg = "font='Arial'"
            for fc in font_candidates:
                if os.path.exists(fc):
                    font_arg = f"fontfile='{fc}'"
                    break

            drawtext_filter = (
                f"drawbox=y=ih-ih/3.5:color=black@0.75:width=iw:height=ih/3.5:t=fill,"
                f"drawtext=text='{clean_title}':{font_arg}:fontsize=h/18:fontcolor=yellow:"
                f"x=(w-text_w)/2:y=h-h/4.5:bordercolor=black:borderw=3"
            )
            cmd_cover = [
                self.ffmpeg_cmd, '-y',
                '-i', base_thumb,
                '-vf', drawtext_filter,
                '-q:v', '2',
                cover_path
            ]
            try:
                subprocess.run(cmd_cover, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(cover_path) and os.path.getsize(cover_path) > 500:
                    cover_info = {
                        "filename": cover_name,
                        "url": f"/downloads/{cover_name}",
                        "title": clean_title
                    }
            except Exception:
                import shutil
                try:
                    shutil.copy2(base_thumb, cover_path)
                    cover_info = {
                        "filename": cover_name,
                        "url": f"/downloads/{cover_name}",
                        "title": clean_title
                    }
                except Exception:
                    pass

        return {
            "success": len(thumbs) > 0,
            "video_id": video_id,
            "thumbnails": thumbs,
            "cover_thumbnail": cover_info
        }

    # ──────────────────────────────────────────────────────────────────────────
    # ASS SUBTITLE GENERATOR (HEADER HOOK + 9:16 BOTTOM SUB)
    # ──────────────────────────────────────────────────────────────────────────

    def save_ass_file(
        self,
        video_id: str,
        subtitles: list,
        header_hook: dict = None,
        style_config: dict = None,
        is_padded_916: bool = False,
        play_res_x: int = 1080,
        play_res_y: int = 1920
    ) -> str:
        """
        Lưu phụ đề định dạng Advanced SubStation Alpha (.ass) chất lượng cao.
        Hỗ trợ:
        - Header Hook (Tiêu đề trên đầu video, font to, box/viền màu nổi bật).
        - Subtitles (Phụ đề dịch ở đáy video không che hình ảnh).
        """
        cfg = style_config or {}
        box_mode = cfg.get("box_mode", "white_box")
        raw_sub_font = cfg.get("font_name", "Montserrat")
        sub_font_name = raw_sub_font.split(",")[0].replace("'", "").replace('"', '').strip() or "Montserrat"
        sub_font_size = int(cfg.get("font_size", 28))
        margin_v = int(cfg.get("margin_v", 45))

        # Nếu là padded 9:16 (video ngang ở giữa), đẩy subtitle xuống khu vực đáy trống
        if is_padded_916:
            margin_v = max(margin_v, 65)
            sub_font_size = max(sub_font_size, 34)

        # Màu sắc Subtitle
        if box_mode == "white_box":
            sub_primary = "&H00000000"
            sub_outline = "&H00FFFFFF"
            sub_back = "&H00FFFFFF"
            sub_border = 3
        elif box_mode == "black_box":
            sub_primary = "&H00FFFFFF"
            sub_outline = "&H00000000"
            sub_back = "&H00000000"
            sub_border = 3
        else:  # outline
            sub_primary = "&H00FFFFFF"
            sub_outline = "&H00000000"
            sub_back = "&H80000000"
            sub_border = 1

        # Hỗ trợ màu chữ tùy chọn từ bảng màu hex
        custom_tc = cfg.get("text_color")
        if custom_tc and str(custom_tc).startswith("#") and len(str(custom_tc)) == 7:
            hex_str = str(custom_tc).lstrip("#")
            r = hex_str[0:2]
            g = hex_str[2:4]
            b = hex_str[4:6]
            sub_primary = f"&H00{b}{g}{r}"

        # Cấu hình Header Hook
        hh_cfg = header_hook or {}
        hh_enabled = bool(hh_cfg.get("enabled", False) and hh_cfg.get("text", "").strip())
        hh_text = hh_cfg.get("text", "").strip() if hh_enabled else ""
        hh_style = hh_cfg.get("style", "gold_black")
        hh_font_size = int(hh_cfg.get("font_size", 42))

        # Màu sắc Header Hook theo style (BGR hex)
        if hh_style == "red_box":
            # Chữ trắng, Hộp nền Đỏ nổi bật
            hh_primary = "&H00FFFFFF"
            hh_outline = "&H002020E0"
            hh_back = "&H002020E0"
            hh_border = 3
        elif hh_style == "me_va_be":
            # Chữ trắng, Hộp nền Pastel Hồng/Tím
            hh_primary = "&H00FFFFFF"
            hh_outline = "&H00A855F7"
            hh_back = "&H00A855F7"
            hh_border = 3
        elif hh_style == "black_box":
            # Chữ vàng chanh, Hộp đen
            hh_primary = "&H0000FFFF"
            hh_outline = "&H00000000"
            hh_back = "&H00000000"
            hh_border = 3
        else:  # gold_black (mặc định viral TikTok)
            # Chữ Vàng chanh, Viền Đen đậm
            hh_primary = "&H0000E5FF"
            hh_outline = "&H00000000"
            hh_back = "&H80000000"
            hh_border = 1

        # Header template ASS
        ass_lines = [
            "[Script Info]",
            "Title: Tool Mẹ & Bé Auto Subtitle",
            "ScriptType: v4.00+",
            f"PlayResX: {play_res_x}",
            f"PlayResY: {play_res_y}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: SubDefault,{sub_font_name},{sub_font_size},{sub_primary},&H000000FF,{sub_outline},{sub_back},-1,0,0,0,100,100,0,0,{sub_border},2,1,2,30,30,{margin_v},1",
        ]

        if hh_enabled:
            # Alignment=8 là Top-Center
            hh_margin_v = 75 if is_padded_916 else 50
            ass_lines.append(
                f"Style: HeaderHook,{sub_font_name},{hh_font_size},{hh_primary},&H000000FF,{hh_outline},{hh_back},-1,0,0,0,100,100,0,0,{hh_border},3,2,8,25,25,{hh_margin_v},1"
            )

        ass_lines.extend([
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
        ])

        # Helpers chuyển đổi thời gian
        def srt_to_seconds(ts_str: str) -> float:
            time_part, ms_part = ts_str.split(",")
            h, m, s = map(int, time_part.split(":"))
            return h * 3600 + m * 60 + s + int(ms_part) / 1000.0

        def seconds_to_ass(sec: float) -> str:
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            cs = int((sec - int(sec)) * 100)
            return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

        # Đổi timestamp SRT -> ASS (h:mm:ss.cc)
        def to_ass_ts(srt_ts: str) -> str:
            t_part, ms_part = srt_ts.split(",")
            cs = int(ms_part) // 10
            h, m, s = t_part.split(":")
            return f"{int(h)}:{m}:{s}.{cs:02d}"

        # Xác định thời gian kết thúc video
        last_end_str = "0:10:00.00"
        if subtitles:
            last_end_str = to_ass_ts(subtitles[-1]["end"])

        # Nếu có Header Hook, chèn dialogue xuất hiện suốt video
        if hh_enabled and hh_text:
            ass_lines.append(f"Dialogue: 1,0:00:00.00,{last_end_str},HeaderHook,,0,0,0,,{hh_text}")

        anim_style = cfg.get("animation_style", "karaoke_pop")
        # Mặc định TẮT: font phụ đề (Arial/Montserrat) không có glyph emoji màu
        # nên libass render emoji thành ô vuông tofu trên video. Ai dùng font có
        # emoji thì tự bật lại.
        do_emoji = cfg.get("auto_emoji", False)
        color_by_speaker = bool(cfg.get("color_by_speaker", True))
        show_speaker_name = bool(cfg.get("show_speaker_name", False))

        # Thuật toán màu tương phản thông minh (Contrast-Aware)
        # Nếu hộp nền trắng (white_box), highlight bằng Đỏ Crimson/Xanh Navy để không bị chìm
        # Nếu hộp nền đen (black_box) hoặc outline, highlight bằng Vàng Chanh / Cyan rực rỡ
        if box_mode == "white_box":
            hl_color_code = "&H001818E6&"   # Đỏ Crimson nổi bật trên nền trắng
            hl_border_code = "&H00FFFFFF&"
        elif box_mode == "black_box":
            hl_color_code = "&H0000E5FF&"   # Vàng chanh TikTok trên nền đen
            hl_border_code = "&H00000000&"
        else:
            hl_color_code = "&H0000E5FF&"   # Vàng chanh viền đen
            hl_border_code = "&H00000000&"

        def hex_to_ass(h: str) -> str:
            if h and str(h).startswith("#") and len(str(h)) == 7:
                c = str(h).lstrip("#")
                return f"&H00{c[4:6]}{c[2:4]}{c[0:2]}&"
            return ""

        # Thêm các câu phụ đề dịch (hỗ trợ Animation từng từ CapCut style & màu nhân vật)
        for item in subtitles:
            raw_text = (item.get("translated_text") or item.get("source_text", "")).strip()
            if not raw_text:
                continue

            if show_speaker_name and item.get("speaker_name"):
                raw_text = f"[{item['speaker_name']}]: {raw_text}"

            if cfg.get("uppercase"):
                raw_text = raw_text.upper()

            if do_emoji:
                raw_text = auto_enrich_emojis(raw_text)

            speaker_c = item.get("speaker_color")
            spk_ass_c = hex_to_ass(speaker_c) if (color_by_speaker and speaker_c) else ""

            t_start = srt_to_seconds(item["start"])
            t_end = srt_to_seconds(item["end"])
            duration = max(t_end - t_start, 0.3)

            words = raw_text.split()
            if anim_style in ["karaoke_pop", "capcut_bounce", "word_highlight", "neon_glow", "tag_box"] and len(words) > 1:
                # Tạo chuỗi sự kiện từng từ nảy to / đổi màu kiểu CapCut
                n_words = len(words)
                for i in range(n_words):
                    w_start = t_start + (i / n_words) * duration
                    w_end = t_start + ((i + 1) / n_words) * duration
                    w_start_str = seconds_to_ass(w_start)
                    w_end_str = seconds_to_ass(w_end)

                    # Build text với từ thứ i được highlight
                    parts = []
                    for j, w in enumerate(words):
                        if j == i:
                            if anim_style == "capcut_bounce":
                                # CapCut spring bounce: nảy to 125% rồi co về 100% trong 140ms
                                parts.append(f"{{\\c{hl_color_code}\\3c{hl_border_code}\\b1\\fscx125\\fscy125\\t(0,140,\\fscx100\\fscy100)}}{w}{{\\r}}")
                            elif anim_style == "karaoke_pop":
                                # Nảy to 116% giữ nguyên trong suốt thời gian từ
                                parts.append(f"{{\\c{hl_color_code}\\3c{hl_border_code}\\b1\\fscx116\\fscy116}}{w}{{\\r}}")
                            elif anim_style == "word_highlight":
                                # Đổi màu chữ theo nhịp đọc không đổi kích cỡ
                                parts.append(f"{{\\c{hl_color_code}\\3c{hl_border_code}\\b1}}{w}{{\\r}}")
                            elif anim_style == "neon_glow":
                                # Neon Cyan phát sáng viền Tím
                                parts.append(f"{{\\c&H00FFFF00&\\3c&H00A855F7&\\bord4\\b1}}{w}{{\\r}}")
                            elif anim_style == "tag_box":
                                # Chữ đen nổi trên viền hộp vàng chanh
                                parts.append(f"{{\\c&H00000000&\\3c&H0000E5FF&\\bord5\\b1}}{w}{{\\r}}")
                            else:
                                parts.append(f"{{\\c{hl_color_code}\\b1}}{w}{{\\r}}")
                        else:
                            if spk_ass_c:
                                parts.append(f"{{\\c{spk_ass_c}}}{w}{{\\r}}")
                            else:
                                parts.append(w)
                    line_text = " ".join(parts)
                    ass_lines.append(f"Dialogue: 0,{w_start_str},{w_end_str},SubDefault,,0,0,0,,{line_text}")
            else:
                # Classic mode (hiển thị cả câu tĩnh)
                start_ass = to_ass_ts(item["start"])
                end_ass = to_ass_ts(item["end"])
                text_ass = raw_text.replace("\n", "\\N")
                ass_prefix = f"{{\\c{spk_ass_c}}}" if spk_ass_c else ""
                ass_lines.append(f"Dialogue: 0,{start_ass},{end_ass},SubDefault,,0,0,0,,{ass_prefix}{text_ass}")

        ass_content = "\n".join(ass_lines)
        file_path = os.path.join(self.download_dir, f"{video_id}_sub.ass")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(ass_content)
        return file_path

    def _balance_words_into_chunks(self, words: list, max_words: int = 11) -> list:
        """Chia danh sách từ thành các chunk cân bằng 1-2 dòng, không để lẻ 1-2 từ."""
        total_words = len(words)
        if total_words <= max_words:
            return [" ".join(words)]
        num_chunks = (total_words + max_words - 1) // max_words
        words_per_chunk = total_words // num_chunks
        remainder = total_words % num_chunks
        chunks = []
        start = 0
        for i in range(num_chunks):
            count = words_per_chunk + (1 if i < remainder else 0)
            chunks.append(" ".join(words[start:start+count]))
            start += count
        return chunks

    def _split_subtitles_into_short_cues(
        self,
        subtitles: list,
        max_words_per_cue: int = 11,
        max_duration_sec: float = 3.8
    ) -> list:
        """
        Chia nhỏ các câu phụ đề dài (>11 từ hoặc >3.8s) thành từng câu ngắn 1-2 dòng,
        tính toán lại timestamp chính xác để phụ đề hiển thị gọn gàng ở đáy video
        và KHÔNG BAO GIỜ bị tràn che hết màn hình video.
        """
        import re

        def parse_to_sec(ts_str):
            try:
                parts = str(ts_str).replace(",", ".").split(":")
                if len(parts) == 3:
                    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                elif len(parts) == 2:
                    return float(parts[0]) * 60 + float(parts[1])
            except Exception:
                pass
            return 0.0

        refined = []
        cue_idx = 1

        for sub in subtitles:
            text = (sub.get("translated_text") or sub.get("source_text") or "").strip()
            source = (sub.get("source_text") or "").strip()
            start_sec = parse_to_sec(sub.get("start", "00:00:00,000"))
            end_sec = parse_to_sec(sub.get("end", "00:00:01,000"))
            total_dur = max(0.4, end_sec - start_sec)

            words = text.split()
            if len(words) <= max_words_per_cue and total_dur <= max_duration_sec:
                refined.append({
                    "index": cue_idx,
                    "start": self._format_timestamp(start_sec),
                    "end": self._format_timestamp(end_sec),
                    "source_text": source,
                    "translated_text": text,
                    "source_lang": sub.get("source_lang", "zh")
                })
                cue_idx += 1
                continue

            # Tách theo câu (. ! ? ; \n)
            sentences = re.split(r'([.!?;\n]+)', text)
            combined_sentences = []
            for i in range(0, len(sentences)-1, 2):
                combined_sentences.append(sentences[i] + sentences[i+1])
            if len(sentences) % 2 == 1 and sentences[-1].strip():
                combined_sentences.append(sentences[-1])
            if not combined_sentences:
                combined_sentences = [text]

            chunks = []
            for sent in combined_sentences:
                sent = sent.strip()
                if not sent:
                    continue
                s_words = sent.split()
                chunks.extend(self._balance_words_into_chunks(s_words, max_words=max_words_per_cue))

            if not chunks:
                chunks = [text]

            total_chars = sum(len(c) for c in chunks) or 1
            cur_start = start_sec
            for c_idx, chunk in enumerate(chunks):
                c_dur = total_dur * (len(chunk) / total_chars)
                c_end = cur_start + c_dur
                if c_idx == len(chunks) - 1:
                    c_end = end_sec

                refined.append({
                    "index": cue_idx,
                    "start": self._format_timestamp(cur_start),
                    "end": self._format_timestamp(c_end),
                    "source_text": source if c_idx == 0 else "",
                    "translated_text": chunk,
                    "source_lang": sub.get("source_lang", "zh")
                })
                cue_idx += 1
                cur_start = c_end

        return refined

    # ──────────────────────────────────────────────────────────────────────────
    # SRT FILE
    # ──────────────────────────────────────────────────────────────────────────

    def save_source_transcript(self, video_id: str, subtitles: list, meta: dict = None) -> str:
        """
        Lưu bản nhận dạng gốc NGAY SAU Whisper, trước khi dịch và đồng bộ.

        Cần thiết vì file .srt hiện có được ghi SAU khi đồng bộ, nên nó đã mang
        mốc thời gian bị dịch chuyển. Khi có trục trặc thì không thể biết lỗi
        đến từ Whisper nghe sai hay từ khâu đồng bộ. Có bản nguồn bất biến thì
        đối chiếu được, và dịch lại được mà không phải chạy lại nhận dạng.
        """
        path = os.path.join(self.download_dir, f"{video_id}_source.json")
        data = {
            "schema_version": 1,
            "video_id": video_id,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "meta": meta or {},
            "segments": [
                {
                    "index": s.get("index"),
                    "start": s.get("start"),
                    "end": s.get("end"),
                    "start_ms": tp.parse_timestamp_ms(s.get("start")),
                    "end_ms": tp.parse_timestamp_ms(s.get("end")),
                    "source_text": s.get("source_text", ""),
                    "source_lang": s.get("source_lang", ""),
                }
                for s in subtitles or []
            ],
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            print(f"[📌 Nguồn] Đã lưu bản nhận dạng gốc: {len(data['segments'])} câu -> {os.path.basename(path)}")
        except Exception as exc:
            print(f"[⚠️ Nguồn] Không lưu được bản gốc: {exc}")
        return path

    def load_source_transcript(self, video_id: str) -> dict:
        """Đọc lại bản nhận dạng gốc nếu có."""
        path = os.path.join(self.download_dir, f"{video_id}_source.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_srt_file(self, video_id: str, subtitles: list, lang: str = "translated") -> str:
        """Lưu phụ đề ra file .srt giữ trọn vẹn từng câu thoại tự nhiên."""
        srt_items = []
        for idx, item in enumerate(subtitles, start=1):
            text = (item.get("translated_text") or item.get("source_text") or "").strip()
            if lang == "source":
                text = (item.get("source_text") or item.get("translated_text") or "").strip()

            if not text:
                continue

            start_val = item.get("start", "00:00:00,000")
            end_val = item.get("end", "00:00:01,000")
            start_td = self._parse_timestamp(start_val)
            end_td = self._parse_timestamp(end_val)

            if end_td <= start_td:
                end_td = start_td + timedelta(seconds=1)

            srt_items.append(srt.Subtitle(
                index=idx,
                start=start_td,
                end=end_td,
                content=text
            ))

        file_path = os.path.join(self.download_dir, f"{video_id}_{lang}.srt")
        with open(file_path, "w", encoding="utf-8") as f:
            if srt_items:
                f.write(srt.compose(srt_items))
            else:
                # Ghi dummy subtitle rỗng nếu không có phụ đề để tránh file 0-byte làm FFmpeg lỗi
                dummy = [srt.Subtitle(index=1, start=timedelta(seconds=0), end=timedelta(seconds=1), content=" ")]
                f.write(srt.compose(dummy))
        return file_path

    # ──────────────────────────────────────────────────────────────────────────
    # HARDSUB BURN
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _escape_srt_path(path: str) -> str:
        """
        Escape đường dẫn file SRT để dùng an toàn trong FFmpeg filter string.
        Xử lý ký tự đặc biệt: dấu cách, dấu hai chấm, ký tự tiếng Việt, ngoặc...
        """
        # Escape theo thứ tự: backslash trước, rồi các ký tự đặc biệt
        path = path.replace('\\', '\\\\')
        path = path.replace("'", "\\'")     # dấu nháy đơn
        path = path.replace(':', '\\:')    # dấu hai chấm (phân tách filter args)
        path = path.replace(',', '\\,')    # dấu phẩy (phân tách filter options)
        path = path.replace('[', '\\[').replace(']', '\\]')  # ngoặc vuông
        return path

    # ──────────────────────────────────────────────────────────────────────────
    # KIỂM TRA TRƯỚC VÀ SAU KHI XUẤT (Giai đoạn 1 & 1B của kế hoạch đồng bộ)
    # ──────────────────────────────────────────────────────────────────────────

    def _assert_bgm_stem_usable(self, stem_path: str, video_path: str) -> None:
        """
        Chặn xuất bản khi chế độ giữ tiếng nền không có stem nền dùng được.

        Tên file `_no_vocals.wav` không tự chứng minh đã tách đúng, nên ở đây
        kiểm tra cả sự tồn tại, kích thước và thời lượng so với video gốc.
        """
        msg_head = "Chưa tách được giọng gốc nên chưa thể xuất bản giữ tiếng nền."
        if not stem_path or not os.path.exists(stem_path):
            raise RuntimeError(
                f"{msg_head} Demucs không tạo được file nền. "
                "Hãy thử lại, hoặc chủ động chọn chế độ 'Thay thế toàn bộ âm thanh' nếu bạn chấp nhận mất tiếng nền."
            )
        if os.path.getsize(stem_path) < 10000:
            raise RuntimeError(f"{msg_head} File nền tách ra rỗng ({os.path.getsize(stem_path)} byte).")

        stem_dur = self._get_media_duration_sec(stem_path)
        video_dur = self._get_media_duration_sec(video_path)
        if stem_dur <= 0:
            raise RuntimeError(f"{msg_head} Không đọc được thời lượng file nền.")
        # Stem phải bám timeline nguồn. Lệch quá 1 giây nghĩa là tách hỏng hoặc
        # lệch mốc, trộn vào sẽ cho ra tiếng nền trôi so với hình.
        if video_dur > 0 and abs(stem_dur - video_dur) > 1.0:
            raise RuntimeError(
                f"{msg_head} Thời lượng nền {stem_dur:.2f}s lệch quá nhiều so với video {video_dur:.2f}s."
            )
        print(f"[✅ Stem nền] Hợp lệ: {stem_dur:.2f}s, bám đúng timeline video {video_dur:.2f}s")

    def _find_cues_outside_video(self, subtitles: list, video_dur_sec: float) -> list:
        """Liệt kê các câu bắt đầu sau khi video đã kết thúc."""
        if video_dur_sec <= 0:
            return []
        outside = []
        for sub in subtitles or []:
            try:
                start = self._parse_timestamp(sub.get("start", "00:00:00,000")).total_seconds()
            except Exception:
                continue
            if start >= video_dur_sec:
                outside.append({
                    "index": sub.get("index"),
                    "start_sec": round(start, 3),
                    "text": (sub.get("translated_text") or sub.get("source_text") or "")[:60],
                })
        return outside

    def _assert_voice_fits_video(self, voice_path: str, video_path: str,
                                 subtitles: list, pad_sec: float) -> None:
        """
        Chặn xuất bản khi lời đọc dài hơn hình.

        Trước đây cờ `-shortest` của FFmpeg lặng lẽ cắt phần voice thừa, nên
        video xuất ra thiếu lời cuối mà vẫn được báo hoàn tất.
        """
        video_dur = self._get_media_duration_sec(video_path)
        voice_dur = self._get_media_duration_sec(voice_path)
        if video_dur <= 0 or voice_dur <= 0:
            return

        allowed = video_dur + pad_sec + 0.25  # dung sai frame/codec
        outside = self._find_cues_outside_video(subtitles, video_dur + pad_sec)

        if voice_dur > allowed:
            thieu = voice_dur - allowed
            detail = ""
            if outside:
                ids = ", ".join(str(c["index"]) for c in outside[:8])
                detail = (f" Có {len(outside)} câu bắt đầu sau khi video kết thúc"
                          f" (câu {ids}{'...' if len(outside) > 8 else ''}).")
            raise RuntimeError(
                f"Lời đọc dài hơn video {thieu:.2f}s nên sẽ bị cắt mất phần cuối "
                f"(voice {voice_dur:.2f}s, video {video_dur:.2f}s, đã nới {pad_sec:.2f}s).{detail} "
                "Cách xử lý: rút gọn bớt câu dịch ở bảng phụ đề, hoặc nâng ô 'Tốc độ đọc tối đa' lên mức cao hơn rồi xuất lại."
            )

        if outside:
            ids = ", ".join(str(c["index"]) for c in outside[:8])
            raise RuntimeError(
                f"Có {len(outside)} câu phụ đề bắt đầu sau khi video đã kết thúc "
                f"(câu {ids}{'...' if len(outside) > 8 else ''}), những câu này sẽ không bao giờ hiện lên. "
                "Hãy sửa mốc thời gian hoặc rút gọn kịch bản rồi xuất lại."
            )

    def _output_duration_flags(self, video_path: str, pad_sec: float) -> list:
        """
        Cờ giới hạn thời lượng cho FFmpeg.

        Trước đây dùng `-shortest`. Nhưng `-shortest` dừng theo luồng NGẮN NHẤT,
        nên khi lời đọc ngắn hơn hình thì phần cuối của VIDEO bị cắt mất. Đo thực
        tế: hình 10s ghép với voice 9s cho ra file chỉ 9s. Ở đây chốt cứng thời
        lượng đầu ra theo video gốc cộng phần nới, phần audio thiếu để im lặng.
        """
        dur = self._get_media_duration_sec(video_path)
        if dur <= 0:
            return ["-shortest"]
        return ["-t", f"{dur + max(0.0, pad_sec):.3f}"]

    def _verify_exported_video(self, output_path: str, video_input: str,
                               expect_audio: bool, pad_sec: float) -> None:
        """Kiểm tra file vừa xuất trước khi coi là thành phẩm."""
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 10000:
            size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            raise RuntimeError(f"File xuất ra không hợp lệ ({size} byte). Giữ lại log để kiểm tra.")

        try:
            probe = subprocess.run(
                [self.ffprobe_cmd, "-v", "error", "-show_entries",
                 "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", output_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
            )
            streams = [s.strip() for s in probe.stdout.splitlines() if s.strip()]
        except Exception as exc:
            raise RuntimeError(f"Không đọc được file vừa xuất: {exc}") from exc

        if "video" not in streams:
            raise RuntimeError("File xuất ra không có luồng hình.")
        if expect_audio and "audio" not in streams:
            raise RuntimeError("File xuất ra không có luồng tiếng dù đang bật lồng tiếng.")

        out_dur = self._get_media_duration_sec(output_path)
        in_dur = self._get_media_duration_sec(video_input)
        if out_dur <= 0:
            raise RuntimeError("File xuất ra không đọc được thời lượng.")
        # Cho phép lệch theo phần nới hình cộng dung sai frame/codec.
        if in_dur > 0 and abs(out_dur - (in_dur + pad_sec)) > 1.0:
            raise RuntimeError(
                f"Thời lượng video xuất ra {out_dur:.2f}s lệch so với dự kiến "
                f"{in_dur + pad_sec:.2f}s. Nhiều khả năng có phần bị cắt mất."
            )
        print(f"[✅ Kiểm tra sau xuất] {out_dur:.2f}s, luồng: {', '.join(streams)}")

    def _cleanup_temp_files(self, video_id: str):
        """Xóa các file tạm TTS sau khi burn xong."""
        patterns = [
            f"{video_id}_tts_*.mp3",
            f"{video_id}_voice*.mp3",
        ]
        import glob
        for pattern in patterns:
            for f in glob.glob(os.path.join(self.download_dir, pattern)):
                try:
                    os.remove(f)
                    print(f"[🗑️ Cleanup] Đã xóa: {os.path.basename(f)}")
                except Exception:
                    pass
        # Xóa file WAV (audio extract tạm)
        wav_path = os.path.join(self.download_dir, f"{video_id}.wav")
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
                print(f"[🗑️ Cleanup] Đã xóa: {video_id}.wav")
            except Exception:
                pass

    def create_export_package(
        self,
        video_id: str,
        target_lang: str = "vi",
        original_title: str = "",
        translated_title: str = "",
        seo_suggestions: dict = None
    ) -> str:
        """
        Đóng gói trọn bộ đăng đa nền tảng (ZIP):
        - Video hardsub (.mp4)
        - File phụ đề (.srt)
        - File bai_dang_san_sang.txt chứa Tiêu đề, Mô tả, Hashtags TikTok/Reels/Shorts
        """
        import zipfile
        zip_path = os.path.join(self.download_dir, f"{video_id}_package.zip")
        video_path = os.path.join(self.download_dir, f"{video_id}_hardsub.mp4")
        srt_path = os.path.join(self.download_dir, f"{video_id}_{target_lang}.srt")

        txt_path = os.path.join(self.download_dir, f"{video_id}_bai_dang.txt")
        seo = seo_suggestions or {}

        lines = [
            "============================================================",
            "📦 GÓI ĐĂNG BÀI ĐA NỀN TẢNG — TOOL MẸ & BÉ",
            "============================================================",
            f"🎬 Video ID: {video_id}",
            f"📌 Tiêu đề gốc: {original_title}",
            f"🌟 Tiêu đề tiếng Việt: {translated_title or original_title}",
            "",
            "────────────────────────────────────────────────────────────",
            "1. 📱 TIÊU ĐỀ GỢI Ý ĐĂNG TIKTOK / REELS / SHORTS:",
            "────────────────────────────────────────────────────────────",
        ]

        titles = seo.get("titles", [])
        if titles:
            for i, t in enumerate(titles, 1):
                lines.append(f"  {i}. {t}")
        else:
            lines.append(f"  1. {translated_title or original_title}")

        lines.extend([
            "",
            "────────────────────────────────────────────────────────────",
            "2. 🏷️ HASHTAGS VIRAL THEO NỀN TẢNG (CHỈ CẦN COPY & PASTE):",
            "────────────────────────────────────────────────────────────",
        ])

        ht_tiktok = " ".join(seo.get("platforms", {}).get("tiktok", {}).get("hashtags", []) or seo.get("hashtags", {}).get("tiktok", []) or ["#xuhuong", "#trending", "#viral", "#fyp"])
        ht_reels = " ".join(seo.get("platforms", {}).get("instagram", {}).get("hashtags", []) or seo.get("hashtags", {}).get("instagram", []) or ["#reels", "#trending", "#viral"])
        ht_shorts = " ".join(seo.get("platforms", {}).get("youtube", {}).get("hashtags", []) or seo.get("hashtags", {}).get("youtube", []) or ["#shorts", "#xuhuong", "#trending"])

        lines.append(f"• TikTok:\n  {ht_tiktok}\n")
        lines.append(f"• Facebook Reels / Instagram:\n  {ht_reels}\n")
        lines.append(f"• YouTube Shorts:\n  {ht_shorts}\n")

        lines.extend([
            "────────────────────────────────────────────────────────────",
            "3. 📝 MÔ TẢ & KÊU GỌI HÀNH ĐỘNG (CTA):",
            "────────────────────────────────────────────────────────────",
            f"  {translated_title or original_title} ✨",
            "  👉 Bấm Follow kênh để xem thêm nhiều video thú vị mỗi ngày nhé!",
            "  💬 Bình luận câu hỏi hoặc cảm nghĩ của bạn bên dưới nha!",
            "============================================================"
        ])

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            if os.path.exists(video_path):
                zipf.write(video_path, arcname=f"{video_id}_video.mp4")
            if os.path.exists(srt_path):
                zipf.write(srt_path, arcname=f"{video_id}_subtitles.srt")
            if os.path.exists(txt_path):
                zipf.write(txt_path, arcname="bai_dang_san_sang.txt")

        return zip_path

    def prepare_trimmed_source(self, video_id: str, subtitles: list,
                               trim_start, trim_end) -> tuple:
        """
        Cắt video nguồn còn đoạn [trim_start, trim_end] giây và dời mốc phụ đề về 0.

        Trả về (đường_dẫn_video_dùng, phụ_đề_mới). Không cắt gì đáng kể thì trả
        nguyên bản gốc. Dùng chung cho cả lúc render lẫn lúc lập kế hoạch đồng bộ,
        để bảng Bác sĩ và bản xuất luôn nhìn cùng một đoạn.
        """
        original = os.path.join(self.download_dir, f"{video_id}.mp4")
        try:
            t0 = max(0.0, float(trim_start or 0))
        except (TypeError, ValueError):
            t0 = 0.0
        full = self._get_media_duration_sec(original)
        try:
            t1 = float(trim_end) if trim_end not in (None, "") else full
        except (TypeError, ValueError):
            t1 = full
        if full > 0:
            t1 = min(t1, full)
        # Không cắt nếu đoạn quá ngắn, hoặc gần như trùng cả video.
        if t1 - t0 < 0.5 or (t0 <= 0.05 and (full <= 0 or t1 >= full - 0.05)):
            return original, subtitles

        trimmed = os.path.join(self.download_dir, f"{video_id}_src.mp4")
        subprocess.run(
            [self.ffmpeg_cmd, "-y", "-v", "error", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}",
             "-i", original, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-movflags", "+faststart", trimmed],
            check=True, timeout=600)
        # Stem tách giọng cũ là của video đầy đủ; xoá để tách lại từ đoạn đã cắt.
        stale = os.path.join(self.download_dir, f"{video_id}_no_vocals.wav")
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass

        span = t1 - t0
        new_subs = []
        for s in subtitles or []:
            st = self._parse_timestamp(s.get("start", "00:00:00,000")).total_seconds() - t0
            en = self._parse_timestamp(s.get("end", "00:00:01,000")).total_seconds() - t0
            if en <= 0.02 or st >= span - 0.02:
                continue  # câu nằm ngoài đoạn đã chọn
            ns = dict(s)
            ns["start"] = self._format_timestamp(max(0.0, st))
            ns["end"] = self._format_timestamp(min(span, en))
            new_subs.append(ns)
        return trimmed, new_subs

    def burn_hardsub(
        self,
        video_id: str,
        subtitles: list,
        style_config: dict = None,
        voice: str = "vi-VN-HoaiMyNeural",
        voice_rate: str = "+0%",
        anti_copyright: dict = None,
        bgm_track: str = None,
        bgm_volume: float = 0.15,
        **kwargs
    ) -> str:
        """
        Ép phụ đề cứng (Hardsub) đè sub gốc + xử lý audio + BGM Mixer + Bộ Lọc Chống Bản Quyền AI.
        """
        video_input = os.path.join(self.download_dir, f"{video_id}.mp4")
        final_output = os.path.join(self.download_dir, f"{video_id}_hardsub.mp4")
        # Ghi ra file tạm rồi mới đổi tên: nếu render hỏng giữa chừng thì bản
        # thành phẩm cũ không bị ghi đè bằng một file dở dang.
        output_video = os.path.join(self.download_dir, f"{video_id}_hardsub.partial.mp4")
        expect_audio = True

        cfg = style_config or {}
        # Cắt chọn đoạn video nguồn: nếu có, dùng đoạn đã cắt và phụ đề đã dời mốc
        # cho toàn bộ phần còn lại của hàm.
        trim_start = cfg.get("trim_start", kwargs.get("trim_start"))
        trim_end = cfg.get("trim_end", kwargs.get("trim_end"))
        if trim_start or trim_end:
            trimmed_path, trimmed_subs = self.prepare_trimmed_source(
                video_id, subtitles, trim_start, trim_end)
            if trimmed_path != video_input:
                video_input = trimmed_path
                subtitles.clear()
                subtitles.extend(trimmed_subs)
        box_mode = cfg.get("box_mode", "white_box")
        font_name = cfg.get("font_name", "Montserrat")
        font_size = int(cfg.get("font_size", 24))
        margin_v = int(cfg.get("margin_v", 45))
        audio_mode = cfg.get("audio_mode", "replace_voice")
        orig_bgm_vol = float(cfg.get("orig_bgm_volume", kwargs.get("orig_bgm_volume", 0.75)))
        orig_bgm_vol = max(0.05, min(1.0, orig_bgm_vol))

        # Cấu hình Tỷ lệ khung hình (9:16 Shorts vs 16:9 Long Video)
        target_aspect_ratio = kwargs.get("target_aspect_ratio") or cfg.get("target_aspect_ratio", "original")
        w_in, h_in, is_landscape = self.get_video_dimensions(video_input)

        # Điều chỉnh kích thước và vị trí phụ đề thích ứng
        if target_aspect_ratio == "16:9" or (target_aspect_ratio == "original" and is_landscape):
            if "font_size" not in cfg:
                font_size = 20
            if "margin_v" not in cfg:
                margin_v = 45
        elif target_aspect_ratio == "9:16":
            if "font_size" not in cfg:
                font_size = 28
            if "margin_v" not in cfg:
                margin_v = 45

        # Cấu hình Triple-Sync (3 Tầng Đồng Bộ)
        triple_sync = kwargs.get("triple_sync", True)
        max_atempo = float(kwargs.get("max_atempo", 1.35))
        sync_mode = "triple_sync" if triple_sync else "standard"

        # BGM parameters
        bgm_val = cfg.get("bgm_track") or bgm_track
        bgm_vol = float(cfg.get("bgm_volume", bgm_volume))
        bgm_path = None
        if bgm_val and bgm_val != "none":
            preset_file = os.path.join(os.path.dirname(__file__), "static", "bgm", f"{bgm_val}.mp3")
            if os.path.exists(preset_file):
                bgm_path = preset_file
            elif os.path.exists(bgm_val):
                bgm_path = bgm_val

        # Cấu hình chống bản quyền (P0)
        anti_cfg = anti_copyright or {}
        is_anti = bool(anti_cfg.get("enabled", True))

        # Đảm bảo mọi subtitle đều có trường index hợp lệ
        for idx, s in enumerate(subtitles, 1):
            if not s.get("index"):
                s["index"] = idx

        # 🔹 TẦNG 2 & 3: TẠO VOICEOVER TRƯỚC VÀ ĐỒNG BỘ MỐC THỜI GIAN
        voice_audio = None
        synced_subs = None
        pad_duration_sec = 0.0

        characters = kwargs.get("characters") or cfg.get("characters")
        multi_voice = bool(kwargs.get("multi_voice", cfg.get("multi_voice", True)))
        voice_pitch = kwargs.get("voice_pitch") or cfg.get("voice_pitch", "+0Hz")

        # Chế độ neo cứng: mỗi câu ở đúng mốc nguồn, không đẩy câu sau.
        anchored = str(cfg.get("sync_mode", "")) == "source_anchored"
        anchor_report = None
        # Cảnh báo giọng: câu nào phải dùng gTTS thay Edge TTS thì báo rõ, không
        # để chất lượng tụt âm thầm. Neo cứng không dùng gTTS nên luôn để trống.
        self.last_voice_warning = ""

        if anchored and audio_mode in ("replace_voice", "keep_bgm_voice"):
            voice_audio, anchored_subs, anchor_report = self.generate_voiceover_anchored(
                video_id, subtitles,
                voice=voice, rate=voice_rate, pitch=voice_pitch,
                max_speedup=max_atempo,
                allow_shorten=bool(cfg.get("allow_shorten", True)),
                # Ghi đè từ bảng Bác sĩ đồng bộ: ghim chữ cuối từng câu, trần ép
                # nhanh riêng và cờ cho phép lố, để render tái dựng đúng bản đã duyệt.
                overrides=cfg.get("sync_overrides") or None,
                # Đoạn đã cắt (nếu có) để neo cứng chạy trên đúng đoạn xuất ra.
                video_path=video_input,
                progress_callback=kwargs.get("progress_callback"),
            )
            self.last_anchor_report = anchor_report
            if not anchor_report.get("can_export"):
                chan = [x for x in anchor_report["issues"] if x["severity"] == "block"]
                dau = "; ".join(f"câu {x['index']}: {x['detail']}" for x in chan[:3])
                raise RuntimeError(
                    f"Có {len(chan)} câu chưa xử lý được nên chưa xuất bản. {dau}"
                    + ("..." if len(chan) > 3 else "")
                )
            # Phụ đề hiển thị sinh ra TỪ giọng đã chốt, thay cho danh sách cũ.
            subtitles.clear()
            subtitles.extend(anchored_subs)
            synced_subs = None

        elif audio_mode in ("replace_voice", "keep_bgm_voice"):
            try:
                tts_res = self.generate_voiceover(
                    video_id, subtitles,
                    voice=voice,
                    rate=voice_rate,
                    pitch=voice_pitch,
                    characters=characters,
                    multi_voice=multi_voice,
                    sync_mode=sync_mode,
                    max_atempo=max_atempo,
                    return_sync_info=True
                )
                if isinstance(tts_res, tuple) and len(tts_res) == 3:
                    voice_audio, synced_subs, cumulative_delay_ms = tts_res
                else:
                    voice_audio = tts_res
                    synced_subs = subtitles

                gtts_n = len(getattr(self.tts_engine, "gtts_fallback_indices", []) or [])
                if gtts_n:
                    self.last_voice_warning = (
                        f"{gtts_n} câu phải dùng giọng dự phòng gTTS vì Edge TTS lỗi. "
                        "Giọng máy hơn Edge, nên nghe lại các câu đó.")

                # Kiểm tra nới video nếu voice dài hơn video (Tầng 3)
                if voice_audio and os.path.exists(voice_audio) and triple_sync:
                    v_dur = self._get_media_duration_sec(video_input)
                    a_dur = self._get_media_duration_sec(voice_audio)
                    if a_dur > v_dur + 0.15:
                        diff = a_dur - v_dur + 0.25
                        # Giới hạn nới tối đa 3.0s để tránh đơ video
                        pad_duration_sec = round(min(diff, 3.0), 3)
                        print(f"[⚡ Triple-Sync Tầng 3] Nới thời lượng video thêm {pad_duration_sec:.2f}s (Video: {v_dur:.2f}s -> Audio: {a_dur:.2f}s)")
            except Exception as e:
                print(f"[⚠️ Voice Error] {e}")
                voice_audio = None

            if not voice_audio or not os.path.exists(voice_audio) or os.path.getsize(voice_audio) < 100:
                raise RuntimeError("Tạo lồng tiếng AI (TTS) thất bại. Vui lòng kiểm tra kết nối mạng hoặc thử lại với giọng đọc khác!")

        if anchored and (not voice_audio or not os.path.exists(voice_audio)):
            raise RuntimeError("Chế độ neo cứng không ghép được luồng tiếng.")

        # Chọn phụ đề đã đồng bộ mốc thời gian để ép sub
        # TTS có thể thất bại ở một câu; vẫn phải giữ câu gốc trong subtitle.
        # Không dùng danh sách synced rút gọn vì sẽ làm mất subtitle trên video.
        if synced_subs:
            synced_by_index = {s.get("index"): s for s in synced_subs if s.get("index") is not None}
            active_subs = [synced_by_index.get(s.get("index"), s) for s in subtitles]
            subtitles.clear()
            subtitles.extend(active_subs)
        else:
            active_subs = subtitles

        # Kiểm tra trước khi xuất: lời đọc phải nằm gọn trong hình.
        # Trước đây cờ -shortest lặng lẽ cắt phần thừa nên video thiếu lời cuối
        # mà vẫn được báo là hoàn tất.
        if voice_audio and os.path.exists(voice_audio):
            self._assert_voice_fits_video(voice_audio, video_input, active_subs, pad_duration_sec)

        # Quyết định dùng định dạng .ass hay .srt
        burn_sub = cfg.get("burn_subtitles", True)
        sub_filter = ""
        srt_path = None
        safe_srt_path = None
        ass_path = None

        if not burn_sub:
            # Chế độ Video Sạch: không ép sub, chỉ giữ voice + BGM + anti-copyright
            print(f"[🧹 Clean Mode] Tắt ép phụ đề - Video sạch (burn_subtitles=False)")
            sub_filter = ""
        elif active_subs and len(active_subs) > 0:
            has_speakers = any(s.get("speaker_id") for s in active_subs)
            color_by_speaker = bool(cfg.get("color_by_speaker", True))
            use_ass = bool(
                style_config.get("header_hook", {}).get("enabled") or 
                style_config.get("use_ass") or 
                style_config.get("animation") or
                (has_speakers and color_by_speaker)
            )
            if use_ass:
                ass_path = self.save_ass_file(
                    video_id, active_subs,
                    header_hook=style_config.get("header_hook"),
                    style_config=style_config
                )
                escaped_ass = self._escape_srt_path(ass_path)
                sub_filter = f"ass='{escaped_ass}'"
            else:
                # Split câu dài thành các đoạn ngắn hiển thị 1-2 dòng
                display_subs = self._split_long_subtitles_for_display(active_subs, max_words=8, max_chars=40)
                if cfg.get("uppercase"):
                    for s in display_subs:
                        if s.get("translated_text"):
                            s["translated_text"] = s["translated_text"].upper()
                print(f"[📝 Sub Split] {len(active_subs)} câu gốc → {len(display_subs)} đoạn hiển thị ngắn")

                # Lưu file SRT hiển thị (đã split) với tên an toàn
                srt_path = self.save_srt_file(video_id, display_subs, lang="display")
                safe_srt_path = os.path.join(self.download_dir, f"{video_id}_sub.srt")
                if srt_path != safe_srt_path:
                    import shutil
                    shutil.copy2(srt_path, safe_srt_path)

                # Đảm bảo file safe_srt_path chắc chắn tồn tại và có nội dung hợp lệ cho libass
                if not os.path.exists(safe_srt_path) or os.path.getsize(safe_srt_path) == 0:
                    with open(safe_srt_path, "w", encoding="utf-8") as f:
                        f.write("1\n00:00:00,000 --> 00:00:01,000\n \n\n")

                # Màu sắc box che chữ gốc
                if box_mode == "white_box":
                    primary_color = "&H00000000"
                    outline_color = "&H00FFFFFF"
                    back_color = "&H00FFFFFF"
                    border_style = 3
                elif box_mode == "black_box":
                    primary_color = "&H00FFFFFF"
                    outline_color = "&H00000000"
                    back_color = "&H00000000"
                    border_style = 3
                else:  # outline only
                    primary_color = "&H00FFFFFF"
                    outline_color = "&H00000000"
                    back_color = "&H80000000"
                    border_style = 1

                # Hỗ trợ màu chữ tùy chọn từ bảng màu hex
                custom_tc = cfg.get("text_color")
                if custom_tc and str(custom_tc).startswith("#") and len(str(custom_tc)) == 7:
                    hex_str = str(custom_tc).lstrip("#")
                    r = hex_str[0:2]
                    g = hex_str[2:4]
                    b = hex_str[4:6]
                    primary_color = f"&H00{b}{g}{r}"

                # Làm sạch tên font cho FFmpeg libass
                raw_font = cfg.get("font_name") or font_name or "Arial"
                clean_font = raw_font.split(",")[0].replace("'", "").replace('"', '').strip()
                if not clean_font:
                    clean_font = "Arial"

                # Mặc định an toàn cho margin_v
                effective_margin_v = int(cfg.get("margin_v", margin_v or 45))

                # Clamp font_size hợp lý cho video dọc (576p/720p)
                effective_font_size = font_size
                if w_in and h_in and h_in > w_in:
                    # Video dọc: giới hạn font tối đa 22px tương đối
                    effective_font_size = min(font_size, 22)
                print(f"[🔤 Font] FontName={clean_font}, FontSize={effective_font_size} (gốc {font_size}), MarginV={effective_margin_v}, Video={w_in}x{h_in}")

                # Escape path an toàn cho FFmpeg filter string
                # QUAN TRỌNG: thêm original_size để libass scale font đúng theo kích thước video thật
                escaped_srt = self._escape_srt_path(safe_srt_path)
                sub_filter = (
                    f"subtitles='{escaped_srt}'"
                    f":original_size={w_in}x{h_in}"
                    f":force_style='"
                    f"FontName={clean_font},FontSize={effective_font_size},"
                    f"PrimaryColour={primary_color},OutlineColour={outline_color},"
                    f"BackColour={back_color},BorderStyle={border_style},"
                    f"MarginV={effective_margin_v},Alignment=2,Bold=1'"
                )

        # Xây dựng chuỗi Video Filter (Canvas 9:16 / 16:9 + Anti-Copyright + Tầng 3 Video Padding + Subtitle đè sub gốc)
        v_filters = []
        if target_aspect_ratio == "9:16" and is_landscape:
            v_filters.append("scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black")
        elif target_aspect_ratio == "16:9" and not is_landscape:
            v_filters.append("scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black")

        if is_anti:
            if anti_cfg.get("micro_zoom", True):
                v_filters.append("scale=1.025*iw:1.025*ih,crop=iw/1.025:ih/1.025")
            if anti_cfg.get("flip_horizontal", False):
                v_filters.append("hflip")
            if anti_cfg.get("film_grain", True):
                v_filters.append("noise=alls=3:allf=t+u")

        # Nới thời lượng video bằng cách freeze khung hình cuối nếu cần
        if pad_duration_sec > 0:
            v_filters.append(f"tpad=stop_mode=clone:stop_duration={pad_duration_sec:.3f}")

        # Cấu hình xoá phụ đề gốc (Subtitle Eraser / Inpainting)
        erase_sub = bool(kwargs.get("erase_subtitles", cfg.get("erase_subtitles", False)))
        erase_coords = kwargs.get("erase_coords") or cfg.get("erase_coords")
        if erase_sub:
            box = self.sub_eraser.parse_coords(erase_coords, w_in, h_in, is_landscape)
            delogo_f = self.sub_eraser.get_delogo_filter(box)
            v_filters.append(delogo_f)
            print(f"[🧹 Sub Eraser] Đã kích hoạt bộ lọc xoá sub gốc: {delogo_f} (box={box})")

        if sub_filter:
            v_filters.append(sub_filter)
        vf_complex = ",".join(v_filters) if v_filters else "null"

        # Chuỗi Audio Filter (Pitch Shift)
        pitch_af = "aresample=44100,asetrate=44100*1.015,aresample=44100,atempo=0.985" if (is_anti and anti_cfg.get("pitch_shift", True)) else ""

        print(f"[🎬 Hardsub] Xuất video (audio={audio_mode}, box={box_mode}, bgm={bool(bgm_path)}, anti_reup={is_anti}, triple_sync={triple_sync})...")

        dur_flags = self._output_duration_flags(video_input, pad_duration_sec)
        render_ok = False
        try:
            cmd = [self.ffmpeg_cmd, '-y', '-i', video_input]
            filter_parts = [f"[0:v]{vf_complex}[vout]"]

            if audio_mode in ("replace_voice", "keep_bgm_voice"):
                no_vocals_audio = None
                if audio_mode == "keep_bgm_voice":
                    no_vocals_audio = self.separate_vocal_and_bgm(video_id, video_input)
                    # KHÔNG âm thầm lùi về replace_voice. Người dùng chọn giữ tiếng
                    # nền mà lại nhận video bị thay sạch âm gốc là kết quả sai, và
                    # trước đây nó vẫn được báo là thành công.
                    self._assert_bgm_stem_usable(no_vocals_audio, video_input)

                smart_ducking = bool(kwargs.get("smart_ducking", True) if "smart_ducking" in kwargs else cfg.get("smart_ducking", True))

                if voice_audio and os.path.exists(voice_audio):
                    cmd.extend(['-i', voice_audio])  # Input 1: Voice

                    # Chuẩn bị luồng âm thanh voice (áp dụng pitch nếu có)
                    v_voice = "[1:a]"
                    if pitch_af:
                        filter_parts.append(f"[1:a]{pitch_af}[va_p]")
                        v_voice = "[va_p]"

                    if no_vocals_audio and os.path.exists(no_vocals_audio):
                        # 🌟 CHẾ ĐỘ TÁCH VOCAL AI: Mix Voice AI (Input 1) + no_vocals gốc (Input 2)
                        cmd.extend(['-i', no_vocals_audio])  # Input 2: Original BGM/SFX
                        filter_parts.append(f"[2:a]volume={orig_bgm_vol}[orig_bgma]")

                        if bgm_path and os.path.exists(bgm_path):
                            cmd.extend(['-stream_loop', '-1', '-i', bgm_path])  # Input 3: Preset BGM
                            filter_parts.append(f"[3:a]volume={min(max(bgm_vol, 0.0), 0.30)}[preset_bgma]")
                            if smart_ducking:
                                # 🎛️ Smart Audio Ducking: Voice làm sidechain trigger tự động hạ cả 2 nguồn BGM
                                filter_parts.append(f"[orig_bgma][preset_bgma]amix=inputs=2:duration=first:normalize=0[combined_bgm]")
                                filter_parts.append(f"{v_voice}asplit=2[v_main][v_side]")
                                filter_parts.append(f"[combined_bgm][v_side]sidechaincompress=threshold=0.05:ratio=3:attack=40:release=320:makeup=1[ducked_bgm]")
                                filter_parts.append(f"[v_main][ducked_bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,alimiter=limit=0.95[aout]")
                            else:
                                filter_parts.append(f"{v_voice}[orig_bgma][preset_bgma]amix=inputs=3:duration=first:dropout_transition=2:normalize=1,alimiter=limit=0.95[aout]")
                        else:
                            if smart_ducking:
                                # 🎛️ Smart Audio Ducking cho BGM gốc
                                filter_parts.append(f"{v_voice}asplit=2[v_main][v_side]")
                                filter_parts.append(f"[orig_bgma][v_side]sidechaincompress=threshold=0.05:ratio=3:attack=40:release=320:makeup=1[ducked_bgm]")
                                filter_parts.append(f"[v_main][ducked_bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,alimiter=limit=0.95[aout]")
                            else:
                                filter_parts.append(f"{v_voice}[orig_bgma]amix=inputs=2:duration=first:dropout_transition=2:normalize=1,alimiter=limit=0.95[aout]")
                    elif bgm_path and os.path.exists(bgm_path):
                        cmd.extend(['-stream_loop', '-1', '-i', bgm_path])  # Input 2: BGM
                        filter_parts.append(f"[2:a]volume={min(max(bgm_vol, 0.0), 0.30)}[bgma]")
                        if smart_ducking:
                            # 🎛️ Smart Audio Ducking cho Preset BGM
                            filter_parts.append(f"{v_voice}asplit=2[v_main][v_side]")
                            filter_parts.append(f"[bgma][v_side]sidechaincompress=threshold=0.05:ratio=3:attack=40:release=320:makeup=1[ducked_bgm]")
                            filter_parts.append(f"[v_main][ducked_bgm]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,alimiter=limit=0.95[aout]")
                        else:
                            filter_parts.append(f"{v_voice}[bgma]amix=inputs=2:duration=first:dropout_transition=2:normalize=1,alimiter=limit=0.95[aout]")
                    else:
                        filter_parts.append(f"{v_voice}anull[aout]")

                    cmd.extend([
                        '-filter_complex', ";".join(filter_parts),
                        '-map', '[vout]', '-map', '[aout]',
                        '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                        '-c:a', 'aac', '-b:a', '128k',
                        *dur_flags,
                        output_video
                    ])
                else:
                    # Nếu không có file voice
                    if no_vocals_audio and os.path.exists(no_vocals_audio):
                        cmd.extend(['-i', no_vocals_audio])
                        filter_parts.append(f"[1:a]volume={orig_bgm_vol}[aout]")
                        cmd.extend([
                            '-filter_complex', ";".join(filter_parts),
                            '-map', '[vout]', '-map', '[aout]',
                            '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                            '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                            '-c:a', 'aac', '-b:a', '128k',
                            *dur_flags,
                            output_video
                        ])
                    elif bgm_path and os.path.exists(bgm_path):
                        cmd.extend(['-stream_loop', '-1', '-i', bgm_path])
                        filter_parts.append(f"[1:a]volume={min(max(bgm_vol, 0.0), 0.30)}[aout]")
                        cmd.extend([
                            '-filter_complex', ";".join(filter_parts),
                            '-map', '[vout]', '-map', '[aout]',
                            '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                            '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                            '-c:a', 'aac', '-b:a', '128k',
                            *dur_flags,
                            output_video
                        ])
                    else:
                        expect_audio = False
                        cmd.extend([
                            '-vf', vf_complex, '-an',
                            '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                            '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                            output_video
                        ])
            elif audio_mode == "mute":
                if bgm_path and os.path.exists(bgm_path):
                    cmd.extend(['-stream_loop', '-1', '-i', bgm_path])
                    filter_parts.append(f"[1:a]volume={min(max(bgm_vol, 0.0), 0.30)}[aout]")
                    cmd.extend([
                        '-filter_complex', ";".join(filter_parts),
                        '-map', '[vout]', '-map', '[aout]',
                        '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                        '-c:a', 'aac', '-b:a', '128k',
                        *dur_flags,
                        output_video
                    ])
                else:
                    expect_audio = False
                    cmd.extend([
                        '-vf', vf_complex, '-an',
                        '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                        output_video
                    ])
            else:  # keep_original
                if bgm_path and os.path.exists(bgm_path):
                    cmd.extend(['-stream_loop', '-1', '-i', bgm_path])
                    filter_parts.append(f"[1:a]volume={min(max(bgm_vol, 0.0), 0.30)}[bgma]")
                    if pitch_af:
                        filter_parts.append(f"[0:a]{pitch_af}[orig_p];[orig_p][bgma]amix=inputs=2:duration=first:dropout_transition=2:normalize=1,alimiter=limit=0.95[aout]")
                    else:
                        filter_parts.append(f"[0:a][bgma]amix=inputs=2:duration=first:dropout_transition=2:normalize=1,alimiter=limit=0.95[aout]")
                    cmd.extend([
                        '-filter_complex', ";".join(filter_parts),
                        '-map', '[vout]', '-map', '[aout]',
                        '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                        '-c:a', 'aac', '-b:a', '128k',
                        *dur_flags,
                        output_video
                    ])
                else:
                    if pitch_af:
                        cmd.extend([
                            '-vf', vf_complex,
                            '-af', pitch_af,
                            '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                            '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                            '-c:a', 'aac', '-b:a', '128k',
                            output_video
                        ])
                    else:
                        cmd.extend([
                            '-vf', vf_complex,
                            '-c:v', 'libx264', '-crf', '22', '-preset', 'ultrafast',
                            '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                            '-c:a', 'copy',
                            output_video
                        ])

            subprocess.run(cmd, check=True)
            # Kiểm tra file thật rồi mới coi là thành phẩm.
            self._verify_exported_video(output_video, video_input, expect_audio, pad_duration_sec)
            os.replace(output_video, final_output)
            # Ghép ảnh/logo overlay bằng một lượt riêng, sau khi đã có thành phẩm.
            overlays = cfg.get("overlays") or kwargs.get("overlays")
            if overlays:
                self.apply_overlays(final_output, overlays)
            render_ok = True
            print(f"[✅ Success] Xuất video hoàn tất: {final_output}")
        finally:
            if safe_srt_path and safe_srt_path != srt_path and os.path.exists(safe_srt_path):
                try:
                    os.remove(safe_srt_path)
                except Exception:
                    pass
            if ass_path and os.path.exists(ass_path):
                try:
                    os.remove(ass_path)
                except Exception:
                    pass
            # Chỉ dọn file tạm khi đã xuất thành công. Render hỏng thì giữ lại
            # voice và wav để còn nghe lại tìm nguyên nhân.
            if render_ok:
                self._cleanup_temp_files(video_id)
            else:
                print(f"[ℹ️ Chẩn đoán] Giữ lại file tạm của {video_id} để kiểm tra nguyên nhân.")

        return final_output

    # ──────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _format_timestamp(self, seconds) -> str:
        if isinstance(seconds, timedelta):
            seconds = seconds.total_seconds()
        else:
            seconds = float(seconds)
        td = timedelta(seconds=seconds)
        total_seconds = int(td.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        millis = int((seconds - int(seconds)) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def _parse_timestamp(self, ts_str) -> timedelta:
        """Chuyển đổi chuỗi timestamp (00:00:01,234 hoặc 00:00:01.234 hoặc số float) sang timedelta."""
        try:
            if isinstance(ts_str, timedelta):
                return ts_str
            if isinstance(ts_str, (int, float)):
                return timedelta(seconds=float(ts_str))

            clean = str(ts_str).strip().replace(".", ",")
            if "," in clean:
                time_part, ms_part = clean.split(",", 1)
            else:
                time_part = clean
                ms_part = "0"

            parts = time_part.split(":")
            if len(parts) == 3:
                h, m, s = map(int, parts)
            elif len(parts) == 2:
                h = 0
                m, s = map(int, parts)
            elif len(parts) == 1:
                return timedelta(seconds=float(clean.replace(",", ".")))
            else:
                return timedelta(seconds=0)

            ms = int(ms_part.ljust(3, "0")[:3])
            return timedelta(hours=h, minutes=m, seconds=s, milliseconds=ms)
        except Exception:
            return timedelta(seconds=0)

    def _split_long_subtitles_for_display(self, subs: list, max_words: int = 8, max_chars: int = 40) -> list:
        """
        Chia nhỏ các câu phụ đề dài thành nhiều phân đoạn hiển thị ngắn (1-2 dòng)
        để không bị tràn chữ, che mất video hay tạo khối sub khổng lồ.
        """
        display_subs = []
        new_idx = 1
        for item in subs:
            text = (item.get("translated_text") or item.get("source_text") or "").strip()
            if not text:
                continue

            words = text.split()
            # Nếu câu ngắn (dưới max_words và dưới max_chars), giữ nguyên
            if len(words) <= max_words and len(text) <= max_chars:
                item_copy = dict(item)
                item_copy["index"] = new_idx
                display_subs.append(item_copy)
                new_idx += 1
                continue

            # Tính thời lượng câu
            start_td = self._parse_timestamp(item.get("start", "00:00:00,000"))
            end_td = self._parse_timestamp(item.get("end", "00:00:01,000"))
            dur_sec = max((end_td - start_td).total_seconds(), 0.5)

            chunks = []
            curr_chunk = []
            curr_len = 0
            for w in words:
                if (len(curr_chunk) >= max_words or (curr_len + len(w) + 1 > max_chars)) and curr_chunk:
                    chunks.append(" ".join(curr_chunk))
                    curr_chunk = [w]
                    curr_len = len(w)
                else:
                    curr_chunk.append(w)
                    curr_len += len(w) + 1
            if curr_chunk:
                chunks.append(" ".join(curr_chunk))

            if not chunks:
                chunks = [text]

            num_chunks = len(chunks)
            chunk_dur = dur_sec / num_chunks

            for i, chunk_text in enumerate(chunks):
                c_start = start_td + timedelta(seconds=i * chunk_dur)
                c_end = start_td + timedelta(seconds=(i + 1) * chunk_dur)
                display_subs.append({
                    "index": new_idx,
                    "start": self._format_timestamp(c_start),
                    "end": self._format_timestamp(c_end),
                    "translated_text": chunk_text,
                    "source_text": chunk_text,
                    "target_lang": item.get("target_lang", "vi")
                })
                new_idx += 1

        return display_subs

    def remove_original_subtitles(
        self,
        video_id: str,
        coords: dict = None,
        engine: str = "ffmpeg_smart"
    ) -> str:
        """
        Xoá sạch phụ đề gốc và xuất ra file video sạch: {video_id}_clean.mp4
        """
        video_input = os.path.join(self.download_dir, f"{video_id}.mp4")
        if not os.path.exists(video_input):
            raise FileNotFoundError(f"Không tìm thấy video {video_id}.mp4")

        clean_output = os.path.join(self.download_dir, f"{video_id}_clean.mp4")
        return self.sub_eraser.erase_subtitles(
            video_input, clean_output, coords=coords, engine=engine
        )

    def preview_original_subtitle_removal(
        self,
        video_id: str,
        coords: dict = None,
        engine: str = "ffmpeg_smart",
        start_seconds: float = 0,
        duration_seconds: float = 5,
    ) -> str:
        """Tạo một đoạn xem thử ngắn cho tính năng xoá sub, không đụng file xuất.

        Bộ lọc được gọi với đúng engine và toạ độ sẽ dùng khi xuất thật. Vì vậy
        preview là bằng chứng trực tiếp cho vùng xoá, nhưng không bắt người dùng
        phải render cả video chỉ để kiểm tra.
        """
        import hashlib

        video_input = os.path.join(self.download_dir, f"{video_id}.mp4")
        if not os.path.exists(video_input):
            raise FileNotFoundError(f"Không tìm thấy video {video_id}.mp4")

        start_seconds = max(0.0, float(start_seconds or 0))
        duration_seconds = min(10.0, max(2.0, float(duration_seconds or 5)))
        signature = json.dumps({"coords": coords or {}, "engine": engine, "start": round(start_seconds, 1)}, sort_keys=True)
        key = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10]
        clip_path = os.path.join(self.download_dir, f".{video_id}_erase_preview_{key}.mp4")
        output_path = os.path.join(self.download_dir, f"{video_id}_erase_preview_{key}.mp4")

        # Re-encode the short clip so its timestamps always start at zero and the
        # inpainting engine receives a normal, self-contained input file.
        clip_cmd = [
            self.ffmpeg_cmd, "-y", "-ss", f"{start_seconds:.3f}", "-i", video_input,
            "-t", f"{duration_seconds:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
            clip_path,
        ]
        result = subprocess.run(clip_cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(clip_path):
            raise RuntimeError(f"Không tạo được đoạn xem thử: {result.stderr[-500:]}")
        try:
            return self.sub_eraser.erase_subtitles(clip_path, output_path, coords=coords, engine=engine)
        finally:
            if os.path.exists(clip_path):
                os.remove(clip_path)
