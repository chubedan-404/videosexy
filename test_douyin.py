import json
import yt_dlp

def test_yt_dlp():
    print("yt-dlp version:", yt_dlp.version.__version__)
    
    ydl_opts = {
        'extract_flat': True,
        'playlistend': 5,
        'quiet': True,
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    }
    
    print("yt-dlp options configured successfully.")

if __name__ == "__main__":
    test_yt_dlp()
