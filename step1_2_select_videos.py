import sys
import json
from typing import List, Dict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from douyin_fetcher import DouyinFetcher

console = Console()

def parse_selection(selection_str: str, max_index: int) -> List[int]:
    """
    Chuyển đổi chuỗi nhập lựa chọn của người dùng (ví dụ: '1, 3, 5-8', 'all') 
    thành danh sách chỉ số (1-indexed) hợp lệ.
    """
    selection_str = selection_str.strip().lower()
    if selection_str == 'all' or selection_str == 'a':
        return list(range(1, max_index + 1))
    
    selected_indices = set()
    parts = selection_str.replace(';', ',').split(',')
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                start_str, end_str = part.split('-', 1)
                start, end = int(start_str), int(end_str)
                if start > end:
                    start, end = end, start
                for idx in range(start, end + 1):
                    if 1 <= idx <= max_index:
                        selected_indices.add(idx)
            except ValueError:
                console.print(f"[bold yellow]⚠️ Bỏ qua đoạn khoảng không hợp lệ: {part}[/bold yellow]")
        else:
            try:
                idx = int(part)
                if 1 <= idx <= max_index:
                    selected_indices.add(idx)
                else:
                    console.print(f"[bold yellow]⚠️ Số thứ tự {idx} nằm ngoài danh sách (1-{max_index})[/bold yellow]")
            except ValueError:
                console.print(f"[bold yellow]⚠️ Bỏ qua mục không phải là số: {part}[/bold yellow]")
                
    return sorted(list(selected_indices))

def display_video_table(videos: List[Dict], channel_name: str):
    """
    Hiển thị danh sách video bằng bảng giao diện đẹp mắt với Rich.
    """
    table = Table(
        title=f"🎬 DANH SÁCH VIDEO TỪ KÊNH: [bold cyan]{channel_name}[/bold cyan]",
        show_header=True,
        header_style="bold magenta",
        border_style="dim green"
    )
    
    table.add_column("STT", justify="center", style="bold yellow", width=6)
    table.add_column("Video ID", justify="center", style="cyan", width=19)
    table.add_column("Tiêu đề Video", style="white", min_width=30)
    table.add_column("Thời lượng", justify="center", style="green", width=10)
    table.add_column("Link Web", style="blue", underline=True)

    for video in videos:
        table.add_row(
            str(video["index"]),
            video["id"],
            video["title"],
            video["duration"],
            video["url"]
        )

    console.print(table)

def main():
    console.print(Panel.fit(
        "[bold cyan]SYSTEM AUTOMATION - BƯỚC 1 & BƯỚC 2[/bold cyan]\n"
        "[white]Nhập Link Kênh Douyin ➡️ Hiển thị Danh Sách Video ➡️ Lựa Chọn Video[/white]",
        border_style="bold blue"
    ))

    # Nhập URL
    url_input = Prompt.ask("\n[bold green]📌 Nhập link kênh Douyin (hoặc link chia sẻ từ app)[/bold green]")
    if not url_input.strip():
        console.print("[bold red]❌ URL không được để trống![/bold red]")
        sys.exit(1)

    # Nhập số lượng giới hạn
    limit_input = Prompt.ask("[bold green]🔢 Số lượng video tối đa muốn quét[/bold green]", default="20")
    try:
        max_results = int(limit_input)
    except ValueError:
        max_results = 20

    # Kiểm tra file cookies nếu có
    cookie_file = "cookies.txt" if sys.platform != "win32" else "cookies.txt"
    fetcher = DouyinFetcher(cookie_file=cookie_file)

    with console.status("[bold yellow]⏳ Đang kết nối và lấy thông tin video từ Douyin...[/bold yellow]", spinner="dots"):
        try:
            videos, channel_name = fetcher.fetch_channel_videos(url_input, max_results=max_results)
        except Exception as e:
            console.print(f"\n[bold red]❌ LỖI KHÔNG LẤY ĐƯỢC VIDEO:[/bold red] {e}")
            console.print("\n[bold yellow]💡 Hướng dẫn khắc phục lỗi:[/bold yellow]")
            console.print("1. Nếu Douyin yêu cầu đăng nhập/captcha: Dùng tiện ích 'Get cookies.txt LOCALLY' trên Chrome/Edge để xuất file [bold cyan]cookies.txt[/bold cyan] đặt cùng thư mục script.")
            console.print("2. Nếu bị chặn IP (403/429): Đổi địa chỉ IP (bật/tắt VPN hoặc sử dụng mạng 4G/5G).")
            sys.exit(1)

    if not videos:
        console.print("[bold red]⚠️ Không tìm thấy video nào trong kênh này.[/bold red]")
        sys.exit(0)

    console.print(f"\n[bold green]✅ Lấy thành công {len(videos)} video![/bold green]\n")
    display_video_table(videos, channel_name)

    # Bước 2: Cho người dùng chọn video
    console.print("\n[bold cyan]📌 HƯỚNG DẪN LỰA CHỌN VIDEO:[/bold cyan]")
    console.print(" - Nhập số thứ tự riêng lẻ: [yellow]1, 3, 5[/yellow]")
    console.print(" - Nhập khoảng số thứ tự: [yellow]1-5[/yellow]")
    console.print(" - Kết hợp cả hai: [yellow]1, 3, 5-10[/yellow]")
    console.print(" - Nhập [yellow]all[/yellow] để chọn tất cả\n")

    selection_raw = Prompt.ask("[bold yellow]👉 Nhập danh sách video bạn muốn xử lý[/bold yellow]", default="1")
    selected_indices = parse_selection(selection_raw, len(videos))

    if not selected_indices:
        console.print("[bold red]❌ Bạn chưa lựa chọn video nào hợp lệ.[/bold red]")
        sys.exit(0)

    selected_videos = [v for v in videos if v["index"] in selected_indices]

    # Hiển thị kết quả lựa chọn
    console.print(f"\n[bold green]🎉 BẠN ĐÃ CHỌN {len(selected_videos)} VIDEO SAU ĐỂ XỬ LÝ:[/bold green]")
    selected_table = Table(show_header=True, header_style="bold yellow", border_style="cyan")
    selected_table.add_column("STT", justify="center", style="bold yellow", width=6)
    selected_table.add_column("Video ID", justify="center", style="cyan", width=19)
    selected_table.add_column("Tiêu đề Video", style="white")
    
    for v in selected_videos:
        selected_table.add_row(str(v["index"]), v["id"], v["title"])
    
    console.print(selected_table)

    # Lưu kết quả chọn ra file selected_videos.json để các bước sau (3, 4, 5, 6) tiêu thụ
    output_filename = "selected_videos.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(selected_videos, f, ensure_ascii=False, indent=2)

    console.print(f"\n[bold green]💾 Danh sách video đã chọn đã được lưu vào file [cyan]{output_filename}[/cyan]![/bold green]")
    console.print("[bold cyan]🚀 Sẵn sàng cho Bước 3 (Tải Video không watermark) & Bước 4 (Whisper Transcript)![/bold cyan]")

if __name__ == "__main__":
    main()
