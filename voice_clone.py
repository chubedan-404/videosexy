"""
voice_clone.py
──────────────
Kho giọng riêng: nhân bản một giọng từ file tải lên hoặc từ link YouTube, nghe
thử, đặt tên rồi cất vào kho để dùng lại ở mọi xưởng.

Ba việc tách bạch trong đây:

1. Lấy nguyên liệu. Một file người dùng tải lên, hoặc phần tiếng của một video
   YouTube. Cả hai đều quy về một file wav một kênh.
2. Chọn đoạn mẫu. Model chỉ cần ba tới tám giây giọng sạch, nhưng phải đúng là
   giọng nói chứ không phải khoảng lặng hay nhạc nền, nên đoạn mẫu được chọn
   bằng cách dò khoảng lặng chứ không cắt bừa từ giây số không.
3. Đọc chữ bằng giọng ấy. Việc này gọi sang sidecar chạy Python 3.12, vì model
   không cài được vào môi trường Python 3.9 của tool.

Giọng đã cất giữ nguyên file mẫu trên đĩa. Nhờ vậy đổi engine sau này vẫn
dùng lại được kho cũ, không phải bắt người dùng đi thu âm lại từ đầu.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from typing import Callable, Dict, List, Optional

import config_store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOICE_DIR = os.path.join(BASE_DIR, "voices")
LIB_FILE = os.path.join(VOICE_DIR, "library.json")
RUNTIME = os.path.join(BASE_DIR, "voice_venv", "bin", "python")
WORKER = os.path.join(BASE_DIR, "voice_clone_worker.py")
MARK = "@@VC@@"

# Model cần ba tới tám giây. Lấy tám giây khi có đủ: mẫu dài hơn thì giọng
# nhân bản bám ngữ điệu người gốc sát hơn, mà tám giây vẫn là trần của model.
REF_WANT = 8.0
REF_MIN = 3.0

# Câu đọc thử mặc định. Có dấu hỏi, có số, có tên riêng: ba chỗ dễ lộ nhất khi
# giọng nhân bản bị méo, nên nghe một câu này là biết giọng dùng được chưa.
DEFAULT_SAMPLE = ("Xin chào cả nhà, mình là giọng vừa được nhân bản. "
                  "Hôm nay mùng chín tháng tư, mình thử đọc một câu hỏi xem sao nhé?")

MAX_SOURCE_SECONDS = 3600.0


def _ffmpeg() -> str:
    local = os.path.join(BASE_DIR, "ffmpeg")
    if os.path.isfile(local):
        return local
    return shutil.which("ffmpeg") or "ffmpeg"


def engine_installed() -> bool:
    return os.path.isfile(RUNTIME) and os.path.isfile(WORKER)


# ─────────────────────────────────────────────────────────────────────────────
# SIDECAR: MỘT TIẾN TRÌNH CHẠY SUỐT
# ─────────────────────────────────────────────────────────────────────────────

class _Sidecar:
    """
    Giữ một tiến trình Python 3.12 sống suốt phiên làm việc.

    Nạp model mất khoảng bốn mươi giây còn đọc một câu chỉ mất một giây, nên
    tiến trình phải sống qua nhiều lượt gọi. Một khoá duy nhất cho cả tiến
    trình: model này chạy một luồng, gọi chồng lên nhau chỉ tổ tranh CPU của
    nhau chứ không nhanh hơn.
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._loaded = False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> None:
        if not engine_installed():
            raise RuntimeError(
                "Máy chưa cài bộ nhân bản giọng. Chạy lệnh cài trong tài liệu "
                "rồi mở lại tool.")
        env = os.environ.copy()
        # Bộ chạy ONNX từ chối file model nằm ngoài thư mục model, mà kho
        # Hugging Face mặc định chỉ đặt liên kết tượng trưng trỏ ra ngoài. Tắt
        # liên kết tượng trưng thì file tải về là file thật và model nạp được.
        env["HF_HUB_DISABLE_SYMLINKS"] = "1"
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        self._proc = subprocess.Popen(
            [RUNTIME, WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
            cwd=BASE_DIR)
        self._loaded = False

    def call(self, cmd: str, timeout: float = 600.0, **payload) -> Dict:
        """Gửi một lệnh và chờ đúng câu trả lời của lệnh đó."""
        with self._lock:
            if not self._alive():
                self._spawn()
            job_id = uuid.uuid4().hex[:8]
            line = json.dumps({"cmd": cmd, "id": job_id, **payload}, ensure_ascii=False)
            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                # Tiến trình chết giữa chừng thì dựng lại một lần rồi gửi lại.
                self._spawn()
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()

            deadline = time.time() + timeout
            while time.time() < deadline:
                got = self._proc.stdout.readline()
                if not got:
                    err = ""
                    try:
                        err = (self._proc.stderr.read() or "")[-600:]
                    except Exception:
                        pass
                    self._proc = None
                    raise RuntimeError(err.strip() or "Bộ nhân bản giọng tắt giữa chừng.")
                if not got.startswith(MARK):
                    continue  # thư viện bên dưới in cảnh báo, bỏ qua
                answer = json.loads(got[len(MARK):])
                if answer.get("id") != job_id:
                    continue
                if not answer.get("success"):
                    raise RuntimeError(answer.get("error") or "Bộ nhân bản giọng báo lỗi.")
                self._loaded = True
                return answer
            raise RuntimeError(f"Bộ nhân bản giọng không trả lời sau {int(timeout)} giây.")

    @property
    def model_loaded(self) -> bool:
        return self._loaded and self._alive()

    def stop(self) -> None:
        with self._lock:
            if self._alive():
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None
            self._loaded = False


_sidecar = _Sidecar()


def warm_up() -> None:
    """Nạp model sẵn ở luồng nền để lần nghe thử đầu tiên không phải chờ lâu."""
    def run():
        try:
            _sidecar.call("warm", timeout=900)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="voice-clone-warm").start()


def engine_status() -> Dict:
    return {"installed": engine_installed(), "loaded": _sidecar.model_loaded}


# ─────────────────────────────────────────────────────────────────────────────
# KHO GIỌNG
# ─────────────────────────────────────────────────────────────────────────────

def _load_library() -> Dict:
    try:
        with open(LIB_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("voices"), list):
            return data
    except Exception:
        pass
    return {"voices": []}


def _save_library(data: Dict) -> bool:
    os.makedirs(VOICE_DIR, exist_ok=True)
    return config_store.write_json(LIB_FILE, data)


def _public(voice: Dict) -> Dict:
    """Bản ghi gửi ra giao diện: đường dẫn trên đĩa đổi thành đường nghe được."""
    vid = voice["id"]
    out = {k: voice.get(k) for k in
           ("id", "name", "note", "source_kind", "source_url", "source_name",
            "created_at", "ref_seconds", "used_count", "last_used")}
    out["ref_url"] = f"/api/voice_clone/audio/{vid}/reference.wav"
    if voice.get("has_preview"):
        out["preview_url"] = f"/api/voice_clone/audio/{vid}/preview.wav"
    return out


def list_voices() -> List[Dict]:
    voices = _load_library()["voices"]
    voices.sort(key=lambda v: v.get("created_at") or 0, reverse=True)
    return [_public(v) for v in voices]


def get_voice(voice_id: str) -> Optional[Dict]:
    for voice in _load_library()["voices"]:
        if voice.get("id") == voice_id:
            return voice
    return None


def voice_dir(voice_id: str) -> str:
    return os.path.join(VOICE_DIR, voice_id)


def _unique_name(name: str, skip_id: str = "") -> str:
    """Tên trùng thì thêm số đếm: hai giọng cùng tên thì chọn nhầm là chắc chắn."""
    taken = {v["name"].strip().lower() for v in _load_library()["voices"]
             if v.get("id") != skip_id and v.get("name")}
    name = (name or "").strip() or "Giọng chưa đặt tên"
    if name.lower() not in taken:
        return name
    for i in range(2, 100):
        thu = f"{name} {i}"
        if thu.lower() not in taken:
            return thu
    return f"{name} {uuid.uuid4().hex[:4]}"


def rename_voice(voice_id: str, name: str, note: str = None) -> Dict:
    data = _load_library()
    for voice in data["voices"]:
        if voice.get("id") == voice_id:
            if name is not None and name.strip():
                voice["name"] = _unique_name(name, skip_id=voice_id)
            if note is not None:
                voice["note"] = note.strip()[:300]
            _save_library(data)
            return _public(voice)
    raise RuntimeError("Không tìm thấy giọng này trong kho.")


def delete_voice(voice_id: str) -> bool:
    data = _load_library()
    keep = [v for v in data["voices"] if v.get("id") != voice_id]
    if len(keep) == len(data["voices"]):
        return False
    folder = voice_dir(voice_id)
    ref = os.path.join(folder, "reference.wav")
    if os.path.isfile(ref):
        try:
            _sidecar.call("forget", timeout=30, ref=ref)
        except Exception:
            pass
    shutil.rmtree(folder, ignore_errors=True)
    data["voices"] = keep
    _save_library(data)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# LẤY NGUYÊN LIỆU VÀ CHỌN ĐOẠN MẪU
# ─────────────────────────────────────────────────────────────────────────────

def _run_ffmpeg(args: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([_ffmpeg(), "-hide_banner", "-nostdin", "-y"] + args,
                          capture_output=True, text=True, timeout=timeout)


def to_wav(src: str, dest: str, start: float = 0.0, seconds: float = 0.0) -> str:
    """Đổi bất cứ file tiếng hay video nào thành wav một kênh 44.1 kHz."""
    args = []
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", src]
    if seconds > 0:
        args += ["-t", f"{seconds:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", "44100", "-acodec", "pcm_s16le", dest]
    run = _run_ffmpeg(args)
    if run.returncode or not os.path.isfile(dest) or os.path.getsize(dest) < 1000:
        raise RuntimeError(f"Không đọc được phần tiếng của file: {(run.stderr or '')[-300:]}")
    return dest


def wav_seconds(path: str) -> float:
    try:
        with wave.open(path, "rb") as audio:
            return audio.getnframes() / float(audio.getframerate() or 1)
    except Exception:
        return 0.0


_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def _speech_spans(path: str) -> List[List[float]]:
    """
    Các khoảng đang có tiếng nói, tìm bằng cách dò ngược khoảng lặng.

    Cắt bừa tám giây đầu file thì rất hay dính nhạc hiệu, tiếng ồn hoặc đúng
    một quãng im lặng, và giọng nhân bản từ mẫu như thế nghe rè. Dò khoảng
    lặng rẻ hơn nhiều so với chạy một model dò giọng nói riêng.
    """
    total = wav_seconds(path)
    if total <= 0:
        return []
    run = _run_ffmpeg(["-i", path, "-af", "silencedetect=noise=-32dB:d=0.35",
                       "-f", "null", "-"], timeout=300)
    log = (run.stderr or "") + (run.stdout or "")
    marks = []
    for line in log.splitlines():
        got = _SIL_START.search(line)
        if got:
            marks.append(("start", float(got.group(1))))
        got = _SIL_END.search(line)
        if got:
            marks.append(("end", float(got.group(1))))
    spans, cursor = [], 0.0
    for kind, at in marks:
        if kind == "start":
            if at - cursor > 0.4:
                spans.append([cursor, min(at, total)])
        else:
            cursor = at
    if total - cursor > 0.4:
        spans.append([cursor, total])
    return [s for s in spans if s[1] > s[0]]


def pick_reference_span(path: str, want: float = REF_WANT) -> List[float]:
    """
    Chọn đoạn mẫu: khoảng nói liền mạch đầu tiên đủ dài, không có thì lấy dài nhất.

    Ưu tiên đoạn đầu tiên đủ dài thay vì đoạn dài nhất: đoạn dài nhất thường
    nằm giữa video, mà phần giữa hay có nhạc nền chồng lên lời nói.
    """
    total = wav_seconds(path)
    spans = _speech_spans(path)
    for span in spans:
        if span[1] - span[0] >= want:
            # Lùi vào một chút cho khỏi dính tiếng gió lúc mới mở miệng.
            start = min(span[0] + 0.15, max(0.0, span[1] - want))
            return [start, want]
    if spans:
        best = max(spans, key=lambda s: s[1] - s[0])
        dur = min(want, best[1] - best[0])
        if dur >= REF_MIN:
            return [best[0], dur]
    # Cả file không tách được khoảng lặng nào thì bỏ một giây đầu rồi lấy tiếp.
    start = 1.0 if total > want + 1.0 else 0.0
    return [start, min(want, max(REF_MIN, total - start))]


def fetch_youtube_audio(url: str, dest: str, start: float = 0.0,
                        progress: Optional[Callable[[str], None]] = None) -> Dict:
    """
    Tải riêng phần tiếng của một video YouTube.

    Video dài thì chỉ tải đúng một cửa sổ quanh mốc người dùng chọn. Một video
    hai tiếng tải trọn phần tiếng mất vài phút, trong khi làm mẫu giọng chỉ
    cần tám giây, nên tải trọn là phí thời gian chờ của người dùng.
    """
    import yt_dlp

    from youtube_ingest import PLAYER_CLIENTS, _client_opts

    folder = os.path.dirname(dest)
    raw = os.path.join(folder, "source_raw.%(ext)s")
    window = 90.0
    ranges = None
    if start > 0:
        head = max(0.0, start - 2.0)
        ranges = [{"start_time": head, "end_time": head + window}]

    base = {
        "quiet": True, "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": raw,
        "ffmpeg_location": _ffmpeg(),
        "noplaylist": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
    }
    if ranges:
        base["download_ranges"] = lambda _info, _ydl: [
            {"start_time": r["start_time"], "end_time": r["end_time"]} for r in ranges]
        base["force_keyframes_at_cuts"] = True

    title, last_error = "", ""
    for client in PLAYER_CLIENTS:
        try:
            if progress:
                progress("Đang tải phần tiếng của video")
            with yt_dlp.YoutubeDL({**base, **_client_opts(client)}) as ydl:
                info = ydl.extract_info(url, download=True)
            title = (info or {}).get("title") or ""
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:200]
            continue

    got = [f for f in os.listdir(folder) if f.startswith("source_raw")]
    if not got:
        raise RuntimeError(f"Không tải được phần tiếng của video. {last_error}")
    src = os.path.join(folder, got[0])
    # Đã cắt cửa sổ lúc tải thì mốc bắt đầu tính lại từ đầu cửa sổ.
    to_wav(src, dest, start=0.0 if ranges else 0.0)
    try:
        os.remove(src)
    except OSError:
        pass
    return {"title": title, "trimmed": bool(ranges)}


# ─────────────────────────────────────────────────────────────────────────────
# VIỆC CHẠY NỀN
# ─────────────────────────────────────────────────────────────────────────────

_JOBS: Dict[str, Dict] = {}
_JOBS_LOCK = threading.Lock()
MAX_JOBS_KEPT = 10


def _prune_jobs() -> None:
    with _JOBS_LOCK:
        done = sorted((j for j in _JOBS.values() if j["status"] in ("done", "error")),
                      key=lambda j: j["updated_at"])
        while len(_JOBS) > MAX_JOBS_KEPT and done:
            _JOBS.pop(done.pop(0)["id"], None)


def get_job(job_id: str) -> Optional[Dict]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _start_job(kind: str, worker: Callable[[Dict], None]) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "status": "running", "progress": 0,
           "step": "Đang bắt đầu", "result": None, "error": "",
           "created_at": time.time(), "updated_at": time.time()}
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def run():
        try:
            worker(job)
            if job["status"] == "running":
                job["status"] = "done"
                job["progress"] = 100
                job["step"] = "Xong"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)[:400]
        finally:
            job["updated_at"] = time.time()
            _prune_jobs()

    threading.Thread(target=run, daemon=True, name=f"voice-{kind}-{job_id}").start()
    return job_id


def _step(job: Dict, progress: int, step: str) -> None:
    job["progress"] = max(0, min(100, progress))
    job["step"] = step
    job["updated_at"] = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# NHÂN BẢN MỘT GIỌNG
# ─────────────────────────────────────────────────────────────────────────────

def start_clone(name: str, source_kind: str, upload_path: str = "", url: str = "",
                start_at: float = 0.0, sample_text: str = "",
                note: str = "", source_name: str = "") -> str:
    """
    Nhận nguyên liệu, dựng đoạn mẫu, đọc thử một câu, rồi cất vào kho.

    Chạy nền vì bước tải video và bước nạp model đều tính bằng chục giây, dài
    quá mức một lượt gọi web nên bắt người dùng ngồi nhìn màn hình trắng.
    """
    sample_text = (sample_text or "").strip() or DEFAULT_SAMPLE
    name = (name or "").strip()

    def worker(job: Dict) -> None:
        voice_id = uuid.uuid4().hex[:10]
        folder = voice_dir(voice_id)
        os.makedirs(folder, exist_ok=True)
        source_wav = os.path.join(folder, "source.wav")
        ref_wav = os.path.join(folder, "reference.wav")
        raw_ref = os.path.join(folder, "reference_raw.wav")
        preview_wav = os.path.join(folder, "preview.wav")
        got_title = ""

        try:
            if source_kind == "youtube":
                if not url.strip():
                    raise RuntimeError("Chưa dán link YouTube.")
                _step(job, 8, "Đang tải phần tiếng của video")
                got = fetch_youtube_audio(url.strip(), source_wav, start=start_at,
                                          progress=lambda m: _step(job, 12, m))
                got_title = got.get("title") or ""
                # Đã cắt cửa sổ lúc tải nên mốc người dùng chọn nằm ở đầu file.
                head = 2.0 if got.get("trimmed") and start_at > 0 else start_at
            elif source_kind == "link":
                # TikTok, Facebook, Instagram...: tải cả video (clip ngắn) rồi tách tiếng. Mốc bắt đầu tính trên cả video.
                import tai_link
                _step(job, 8, f"Đang tải video từ {tai_link.ten_nen(url)}")
                got = tai_link.tai_video(url.strip(), folder)
                got_title = " ".join(x for x in (got.get("tac_gia"), got.get("ten")) if x)[:60]
                _step(job, 20, "Đang tách phần tiếng")
                to_wav(got["file"], source_wav)
                try:
                    os.remove(got["file"])
                except OSError:
                    pass
                head = start_at
            else:
                if not upload_path or not os.path.isfile(upload_path):
                    raise RuntimeError("Không tìm thấy file bạn vừa tải lên.")
                _step(job, 10, "Đang đọc file tiếng")
                to_wav(upload_path, source_wav)
                head = start_at

            total = wav_seconds(source_wav)
            if total < REF_MIN:
                raise RuntimeError(
                    f"Đoạn tiếng chỉ dài {total:.1f} giây, cần ít nhất {REF_MIN:.0f} giây.")
            if total > MAX_SOURCE_SECONDS:
                raise RuntimeError("File tiếng dài quá một tiếng, hãy cắt ngắn bớt.")

            _step(job, 30, "Đang tìm đoạn giọng sạch nhất")
            if head > 0:
                span = [min(head, max(0.0, total - REF_MIN)), REF_WANT]
                span[1] = min(span[1], total - span[0])
            else:
                span = pick_reference_span(source_wav)
            to_wav(source_wav, raw_ref, start=span[0], seconds=span[1])

            _step(job, 45, "Đang lọc tạp âm khỏi giọng mẫu")
            try:
                _sidecar.call("denoise", timeout=900, src=raw_ref, out=ref_wav,
                              max_seconds=REF_WANT)
            except Exception as exc:  # noqa: BLE001
                # Lọc tạp âm hỏng thì vẫn nhân bản được từ đoạn thô, chỉ kém
                # hơn chút. Dừng hẳn ở đây thì người dùng mất trắng công tải.
                job["warning"] = f"Không lọc được tạp âm ({exc}), dùng đoạn gốc."
                shutil.copyfile(raw_ref, ref_wav)

            _step(job, 62, "Đang đọc thử một câu bằng giọng vừa nhân bản")
            spoken = _sidecar.call("speak", timeout=900, ref=ref_wav,
                                   out=preview_wav, text=sample_text[:400])

            _step(job, 92, "Đang cất giọng vào kho")
            record = {
                "id": voice_id,
                "name": _unique_name(name or got_title or "Giọng mới"),
                "note": (note or "").strip()[:300],
                "source_kind": source_kind,
                "source_url": url.strip() if source_kind in ("youtube", "link") else "",
                "source_name": got_title or source_name or "",
                "ref_seconds": round(wav_seconds(ref_wav), 2),
                "sample_text": sample_text[:400],
                "has_preview": os.path.isfile(preview_wav),
                "preview_seconds": spoken.get("seconds", 0),
                "created_at": time.time(),
                "used_count": 0,
                "last_used": 0,
            }
            data = _load_library()
            data["voices"].append(record)
            _save_library(data)

            job["result"] = _public(record)
            if job.get("warning"):
                job["result"]["warning"] = job["warning"]
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            # File nguyên liệu nặng hàng chục MB mà chỉ dùng một lần để cắt ra
            # tám giây. Dọn cả khi nhân bản hỏng: hỏng mới là lúc người dùng
            # thử đi thử lại, tức là lúc rác chồng lên nhanh nhất.
            for tam in (source_wav, raw_ref, upload_path):
                if not tam:
                    continue
                try:
                    os.remove(tam)
                except OSError:
                    pass

    return _start_job("clone", worker)


def start_preview(voice_id: str, text: str) -> str:
    """Đọc thử một câu bất kỳ bằng giọng đã có trong kho."""
    text = (text or "").strip() or DEFAULT_SAMPLE

    def worker(job: Dict) -> None:
        voice = get_voice(voice_id)
        if not voice:
            raise RuntimeError("Không tìm thấy giọng này trong kho.")
        ref = os.path.join(voice_dir(voice_id), "reference.wav")
        if not os.path.isfile(ref):
            raise RuntimeError("Giọng này mất file mẫu, hãy nhân bản lại.")
        out = os.path.join(voice_dir(voice_id), "preview.wav")
        _step(job, 20, "Đang đọc bằng giọng của bạn")
        spoken = _sidecar.call("speak", timeout=900, ref=ref, out=out, text=text[:2000])

        data = _load_library()
        for record in data["voices"]:
            if record.get("id") == voice_id:
                record["has_preview"] = True
                record["sample_text"] = text[:400]
                record["preview_seconds"] = spoken.get("seconds", 0)
                record["used_count"] = int(record.get("used_count") or 0) + 1
                record["last_used"] = time.time()
                _save_library(data)
                job["result"] = _public(record)
                break
        if job.get("result"):
            job["result"]["cache_bust"] = int(time.time())

    return _start_job("preview", worker)


def speak_to_file(voice_id: str, text: str, out_path: str) -> Dict:
    """
    Đọc một đoạn chữ ra file, dùng cho các xưởng làm video gọi sang.

    Gọi thẳng chứ không chạy nền: bên gọi đã ở trong một việc nền của nó rồi.
    """
    voice = get_voice(voice_id)
    if not voice:
        raise RuntimeError("Không tìm thấy giọng này trong kho.")
    ref = os.path.join(voice_dir(voice_id), "reference.wav")
    if not os.path.isfile(ref):
        raise RuntimeError("Giọng này mất file mẫu, hãy nhân bản lại.")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    spoken = _sidecar.call("speak", timeout=1800, ref=ref, out=out_path, text=text)

    data = _load_library()
    for record in data["voices"]:
        if record.get("id") == voice_id:
            record["used_count"] = int(record.get("used_count") or 0) + 1
            record["last_used"] = time.time()
            _save_library(data)
            break
    return {"success": True, "path": out_path, "seconds": spoken.get("seconds", 0)}
