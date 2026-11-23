"""
voice_clone_worker.py
─────────────────────
Sidecar Python 3.12: chạy VieNeu-TTS để nhân bản giọng nói.

Vì sao chạy thường trú chứ không gọi một phát rồi thoát như worker nhận diện
người nói: nạp model mất khoảng bốn mươi giây, còn đọc một câu chỉ mất một
giây. Gọi kiểu một phát rồi thoát thì mỗi lần nghe thử phải chờ bốn mươi giây
nạp lại, tức là chờ nhiều gấp bốn mươi lần phần việc thật.

Trao đổi qua stdin và stdout, mỗi lượt một dòng JSON. Câu trả lời luôn có tiền
tố MARK ở đầu dòng vì thư viện bên dưới thỉnh thoảng in cảnh báo thẳng ra
stdout; không đánh dấu thì bên kia sẽ đọc nhầm cảnh báo thành kết quả.
"""
import hashlib
import json
import os
import sys
import traceback

MARK = "@@VC@@"

_engine = None
# Đường dẫn file mẫu đã nạp vào model, để đọc câu thứ hai trở đi khỏi phải rút
# lại hồ sơ giọng. Rút hồ sơ mất vài giây, đọc một câu chỉ mất một giây.
_enrolled = {}


def reply(obj):
    sys.stdout.write(MARK + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def engine():
    global _engine
    if _engine is None:
        from vieneu import Vieneu
        _engine = Vieneu()
    return _engine


def voice_of(ref_path, denoise=False):
    """Tên nội bộ của một giọng đã nạp; chưa nạp thì nạp ngay tại đây."""
    key = os.path.abspath(ref_path)
    if key in _enrolled:
        return _enrolled[key]
    if not os.path.isfile(key):
        raise RuntimeError("Không tìm thấy file giọng mẫu.")
    name = "vc_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    engine().add_voice(name, key, denoise=bool(denoise))
    _enrolled[key] = name
    return name


def do_ping(_job):
    return {"ready": True}


def do_warm(_job):
    engine()
    return {"ready": True}


def do_presets(_job):
    return {"voices": [{"id": vid, "label": label}
                       for label, vid in engine().list_preset_voices()]}


def do_denoise(job):
    """Lọc tạp âm và cắt file mẫu còn tối đa tám giây, rồi ghi ra file sạch."""
    _wav, rate = engine().denoise(job["src"], out_path=job["out"],
                                  max_seconds=job.get("max_seconds") or 8.0)
    return {"out": job["out"], "sample_rate": rate}


def do_speak(job):
    """Đọc một đoạn chữ bằng giọng đã nhân bản, hoặc bằng giọng dựng sẵn."""
    eng = engine()
    text = (job.get("text") or "").strip()
    if not text:
        raise RuntimeError("Không có chữ để đọc.")
    if job.get("preset"):
        audio = eng.infer(text, voice=job["preset"])
    else:
        audio = eng.infer(text, voice=voice_of(job["ref"], job.get("denoise", False)))
    eng.save(audio, job["out"])
    return {"out": job["out"], "seconds": round(len(audio) / float(eng.sample_rate), 2)}


def do_forget(job):
    """Quên một giọng đã nạp, dùng khi người dùng xoá giọng khỏi kho."""
    key = os.path.abspath(job.get("ref") or "")
    name = _enrolled.pop(key, None)
    if name:
        try:
            engine().remove_voice(name)
        except Exception:
            pass
    return {"forgotten": bool(name)}


HANDLERS = {"ping": do_ping, "warm": do_warm, "presets": do_presets,
            "denoise": do_denoise, "speak": do_speak, "forget": do_forget}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except ValueError:
            continue
        handler = HANDLERS.get(job.get("cmd"))
        if handler is None:
            reply({"id": job.get("id"), "success": False, "error": "Lệnh không hợp lệ."})
            continue
        try:
            reply({"id": job.get("id"), "success": True, **handler(job)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            reply({"id": job.get("id"), "success": False, "error": str(exc)[:400]})


if __name__ == "__main__":
    main()
