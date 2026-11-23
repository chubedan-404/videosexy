"""
Đường vào máy chủ của xưởng Tiên hiệp (MuseAI Studio): /api/muse/xianxia/*

Xưởng làm phim donghua nhiều series, nhiều tập trên tài khoản Muse.ai của người
dùng. Mã lõi ở muse-controller/video/xianxia/. File này chỉ nối lõi đó với
Flask, với bộ hỏi AI dùng chung (lf.ask_json), bộ vẽ tranh
(lf.generate_image_router) và việc chạy nền (lf.start_job).

Mỗi lúc xưởng chỉ chạy một việc: Muse dùng một hồ sơ trình duyệt duy nhất, hai
việc cùng mở là giẫm lên nhau.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory

_MUSE_DIR = Path(__file__).resolve().parent / "muse-controller"
if str(_MUSE_DIR) not in sys.path:
    sys.path.insert(0, str(_MUSE_DIR))

from video.xianxia import continuity, director, prompt_engine, store  # noqa: E402
from video.xianxia.shot_manager import EpisodeShotManager  # noqa: E402

bp = Blueprint("muse_xianxia", __name__, url_prefix="/api/muse/xianxia")

_KHOA = threading.Lock()
_AI_SETTINGS = lambda data=None: {}  # noqa: E731 - app.py đặt lại lúc đăng ký


def register(app, ai_settings) -> None:
    global _AI_SETTINGS
    _AI_SETTINGS = ai_settings
    app.register_blueprint(bp)


# ------------------------------------------------------------------------------
# Việc chạy nền
# ------------------------------------------------------------------------------

def _viec_dang_chay() -> str:
    import longform_studio as lf
    with lf._JOBS_LOCK:
        for job in lf._JOBS.values():
            if job["status"] == "running" and str(job.get("kind") or "").startswith("mx_"):
                return job["id"]
    return ""


# Trình duyệt viết truyện đang mở cho từng việc, để đóng lại khi việc xong.
_WRITERS = {}


def _bat_dau(kind, worker):
    """Kiểm bận và bắt đầu việc dưới cùng một khoá, để bấm đúp không chạy hai việc."""
    import longform_studio as lf

    def run(job):
        try:
            worker(job)
        finally:
            writer = _WRITERS.pop(job["id"], None)
            if writer:
                writer.close()
    with _KHOA:
        ma = _viec_dang_chay()
        if ma:
            return jsonify({"ok": False, "job_id": ma,
                            "error": "Xưởng Tiên hiệp đang chạy một việc. Đợi xong hoặc bấm Dừng."}), 409
        return jsonify({"ok": True, "job_id": lf.start_job(kind, run)})


def _ask(job):
    """
    Bộ hỏi AI cho Đạo diễn. Nguồn do người dùng chọn: AI viết chữ của tool
    (9Router, hỏng thì Gemini API) hoặc Gemini/ChatGPT trên trình duyệt bằng
    tài khoản đã đăng nhập. Muse không bao giờ được hỏi viết.
    """
    import chat_writer
    import longform_studio as lf
    source = chat_writer.get_source()
    settings = _AI_SETTINGS({})

    def ask(system, user, max_tokens=4000):
        if job.get("cancel"):
            raise RuntimeError("Đã dừng theo yêu cầu.")
        if source != "router":
            if job["id"] not in _WRITERS:
                _WRITERS[job["id"]] = chat_writer.WriterSession(source)
            return _WRITERS[job["id"]].ask(system, user, max_tokens)
        got = lf.ask_json(system, user, settings, max_tokens=max_tokens)
        if not got.get("success") or not isinstance(got.get("data"), dict):
            raise RuntimeError("AI không trả lời được: " + (got.get("error") or "không rõ lý do"))
        return got["data"]
    return ask


def _step(job, pct, text):
    import longform_studio as lf
    lf.update_job(job, progress=pct, step=text, log=text)


def _tien(job, tu, den):
    """Đạo diễn hỏi AI nhiều lượt nhỏ; mỗi lượt đẩy thanh tiến trình thêm một nấc trong khoảng tu..den."""
    dem = {"n": 0}

    def tell(text):
        dem["n"] += 1
        _step(job, tu + round((den - tu) * (1 - 0.7 ** dem["n"])), text)
    return tell


def _body():
    return request.get_json(force=True, silent=True) or {}


def _loi(exc, code=400):
    return jsonify({"ok": False, "error": str(exc)}), code


@bp.errorhandler(ValueError)
def _sai_du_lieu(exc):
    return _loi(exc)


@bp.route("/phien", methods=["GET"])
def phien():
    return jsonify({"ok": True, "job_id": _viec_dang_chay()})


@bp.route("/job/<job_id>", methods=["GET"])
def job_get(job_id):
    import longform_studio as lf
    job = lf.get_job(job_id)
    if not job:
        return _loi("Không tìm thấy việc này (tool vừa khởi động lại?).", 404)
    return jsonify({"ok": True, "job": job})


@bp.route("/job/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    import longform_studio as lf
    return jsonify({"ok": lf.cancel_job(job_id)})


# ------------------------------------------------------------------------------
# Máy làm clip: Muse (mặc định) hoặc Google Flow. Chọn một lần ở trang đầu của xưởng, áp cho mọi series.
# ------------------------------------------------------------------------------

MAY_QUAY = {"muse": "Muse.ai", "flow": "Google Flow"}
_MAY_QUAY_FILE = Path(__file__).resolve().parent / "muse-controller" / "may_quay.json"


def _may_dang_chon() -> str:
    try:
        may = json.loads(_MAY_QUAY_FILE.read_text(encoding="utf-8")).get("may")
    except Exception:  # noqa: BLE001
        may = ""
    return may if may in MAY_QUAY else "muse"


def _may_quay(uid, sid):
    """Máy làm clip cho một lượt quay. Cả hai có cùng các hàm mà EpisodeShotManager gọi."""
    if _may_dang_chon() == "flow":
        import flow_may
        return flow_may.FlowStudio(khoa=f"tien_hiep/{uid}/{sid}", ten_du_an=str((store.get_series(uid, sid) or {}).get("title") or sid))
    from muse import MuseAIStudio
    return MuseAIStudio()


def _may_quay_view(kiem=False):
    import flow_may
    try:
        flow = flow_may.kiem() if kiem else flow_may.trang_thai()
    except Exception:  # noqa: BLE001
        flow = {"open": False, "logged_in": False}
    return {"ok": True, "machine": _may_dang_chon(), "machines": [{"id": k, "name": v} for k, v in MAY_QUAY.items()],
            "flow": {"open": bool(flow.get("open")), "logged_in": bool(flow.get("logged_in")), "hidden": bool(flow.get("hidden"))}}


@bp.route("/clip_machine", methods=["GET", "POST"])
def clip_machine():
    if request.method == "POST":
        may = str(_body().get("machine") or "")
        if may not in MAY_QUAY:
            return _loi("Không có máy làm clip này.")
        if _viec_dang_chay():
            return _loi("Đang có việc chạy. Đợi việc xong rồi đổi máy làm clip.")
        _MAY_QUAY_FILE.write_text(json.dumps({"may": may}, ensure_ascii=False), encoding="utf-8")
    # ?check=1: mở Flow ở chế độ ẩn để xem đã đăng nhập chưa. Chỉ máy chạy tool, và không làm lúc đang có việc chạy.
    kiem = request.args.get("check") == "1" and request.remote_addr in ("127.0.0.1", "::1") and _may_dang_chon() == "flow"
    return jsonify(_may_quay_view(kiem))


@bp.route("/clip_machine/open", methods=["POST"])
def clip_machine_open():
    """Mở cửa sổ Google Flow để người dùng tự đăng nhập Google. Chỉ máy chạy tool mở được."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return _loi("Chỉ máy chạy tool mở được cửa sổ Google Flow.", 403)
    import flow_may
    try:
        flow_may.mo()
    except Exception as exc:  # noqa: BLE001
        return _loi("Chưa mở được cửa sổ Flow: " + str(exc)[:200])
    return jsonify(_may_quay_view())


# ------------------------------------------------------------------------------
# Nguồn viết truyện
# ------------------------------------------------------------------------------

def _writer_view():
    import chat_writer
    sites = []
    for sid in sorted(chat_writer.SITES, key=lambda x: x != chat_writer.DEFAULT_SOURCE):
        cfg = chat_writer.SITES[sid]
        folder = chat_writer.profile_dir(sid)
        sites.append({"id": sid, "name": cfg["name"], "browser": True,
                      "has_profile": folder.exists() and any(folder.iterdir()),
                      "window_open": chat_writer.profile_busy(sid),
                      "login": chat_writer.LOGIN_STATE.get(sid, "")})
    sites.append({"id": "router", "name": "AI viết chữ của tool (9Router, chạy trên máy)", "browser": False})
    return {"ok": True, "source": chat_writer.get_source(), "sites": sites, "checked": _WRITER_CHECK}


@bp.route("/writer", methods=["GET", "POST"])
def writer():
    import chat_writer
    if request.method == "POST":
        chat_writer.set_source(str(_body().get("source") or ""))
    return jsonify(_writer_view())


# Kết quả lần kiểm phiên gần nhất của từng nguồn, để trang hiện lại.
_WRITER_CHECK = {}


@bp.route("/writer/check", methods=["POST"])
def writer_check():
    """Kiểm phiên như Muse: mở ẩn, xem đã đăng nhập chưa, rồi hỏi thử một câu thật."""
    import chat_writer
    site = str(_body().get("site") or "")
    if site not in chat_writer.SITES:
        return _loi("Nguồn này không kiểm bằng trình duyệt.")

    def worker(job):
        import time
        _step(job, 30, f"Đang mở {chat_writer.SITES[site]['name']} và hỏi thử một câu...")
        got = chat_writer.check(site)
        got["at"] = time.strftime("%H:%M")
        _WRITER_CHECK[site] = got
        job["result"] = got
    return _bat_dau("mx_check", worker)


@bp.route("/writer/login", methods=["POST"])
def writer_login():
    """Mở cửa sổ trình duyệt thật để người dùng đăng nhập. Phiên được lưu trong hồ sơ riêng của nguồn đó."""
    import chat_writer
    site = str(_body().get("site") or "")
    if site not in chat_writer.SITES:
        return _loi("Nguồn này không cần đăng nhập.")
    if _viec_dang_chay():
        return _loi("Đang có việc chạy, hồ sơ trình duyệt đang bận. Đợi việc xong rồi đăng nhập.")
    if chat_writer.profile_busy(site):
        return _loi("Cửa sổ đăng nhập đang mở sẵn rồi. Đăng nhập trong cửa sổ đó; nó tự đóng khi xong.")
    threading.Thread(target=chat_writer.open_login, args=(site,), daemon=True).start()
    return jsonify({"ok": True})


# ------------------------------------------------------------------------------
# Thư viện phong cách
# ------------------------------------------------------------------------------

@bp.route("/styles", methods=["GET"])
def styles():
    return jsonify({"ok": True, "styles": store.list_packs(), "formats": store.list_story_formats()})


@bp.route("/styles/custom", methods=["POST"])
def styles_custom():
    data = _body()
    mo_ta = str(data.get("description") or "").strip()
    if not mo_ta:
        return _loi("Hãy tả phong cách bạn muốn.")
    template = store.get_pack(data.get("base") or "xianxia") or store.list_packs()[0]
    template = {k: v for k, v in template.items() if k != "custom"}

    def worker(job):
        _step(job, 20, "AI đang dựng bộ phong cách riêng...")
        pack = director.make_custom_pack(_ask(job), mo_ta, str(data.get("direction") or ""), template)
        job["result"] = {"style_id": store.save_custom_pack(pack)}
    return _bat_dau("mx_style", worker)


@bp.route("/styles/save", methods=["POST"])
def styles_save():
    pack = _body().get("pack")
    if not isinstance(pack, dict) or not pack.get("name"):
        return _loi("Thiếu nội dung bộ phong cách.")
    return jsonify({"ok": True, "style_id": store.save_custom_pack(pack)})


# ------------------------------------------------------------------------------
# Vũ trụ và series
# ------------------------------------------------------------------------------

@bp.route("/tree", methods=["GET"])
def tree():
    return jsonify({"ok": True, "universes": store.list_universes(), "job_id": _viec_dang_chay()})


NARRATED_USE = 0.85       # phần của mỗi clip còn lại sau khi rời cảnh lúc hết lời (đo 10/10/2026: 0,79 đến 0,88)
DEFAULT_MINUTES = 8       # tập 30 phút mất hai ngày mới xong và khó soát: người dùng chọn tập ngắn


def _series_settings(data):
    ratio = data.get("aspect_ratio") if data.get("aspect_ratio") in ("16:9", "9:16") else "16:9"
    seconds = max(4, min(10, int(data.get("shot_seconds") or 10)))
    minutes = max(0, min(90, int(float(data.get("episode_minutes") or 0))))
    # Có số phút thì số cảnh quay tính ra từ đó: tập 30 phút, cảnh 10 giây là 180 cảnh.
    # Một giọng kể hết thì phim rời cảnh ngay khi hết lời, mỗi cảnh chỉ dùng khoảng 85% clip:
    # phải quay nhiều cảnh hơn một chút thì tập mới đủ số phút người dùng chọn.
    narrated = bool(data.get("narrated"))
    per_shot = seconds * (NARRATED_USE if narrated else 1)
    shots = round(minutes * 60 / per_shot) if minutes else int(data.get("shots_per_episode") or 12)
    out = {"narrated": narrated, "aspect_ratio": ratio, "episode_minutes": minutes, "shot_seconds": seconds,
           "shots_per_episode": max(4, min(540, shots)),
           "story_format": store.get_story_format(str(data.get("story_format") or "")).get("format_id")}
    if "channel_name" in data:
        out["channel_name"] = str(data.get("channel_name") or "").strip()
    return out


IDEA_TIMEOUT = 45
IDEA_TRIES = 2


def _hoi_nhanh(system, user, settings, max_tokens=1500):
    """
    Một lượt hỏi ngắn tới AI viết chữ của tool, không chờ lâu. `lf.ask_json`
    chờ tới 240 giây mỗi lượt và thử ba lượt (đo 08/10/2026: bình thường 7
    giây, gặp nhà cung cấp chậm trong vòng xoay của 9Router thì 188 giây). Gợi
    ý ý tưởng hiện ngay trên màn tạo series nên thà báo chưa có còn hơn treo.
    """
    import requests
    import longform_studio as lf
    base = str(settings.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("chưa cài địa chỉ AI viết chữ")
    last = ""
    for _ in range(IDEA_TRIES):
        try:
            res = requests.post(base + "/chat/completions",
                                headers={"Authorization": f"Bearer {settings.get('api_key') or '9router-key'}"},
                                json={"model": settings.get("model") or "combo1", "temperature": 0.9,
                                      "max_tokens": max_tokens, "stream": False,
                                      "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}]},
                                timeout=IDEA_TIMEOUT)
            if res.status_code != 200:
                raise ValueError(f"HTTP {res.status_code}")
            content = ((res.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            data = lf._extract_json(content)
            if isinstance(data, dict):
                return data
            raise ValueError("AI không trả về đúng khuôn")
        except Exception as exc:  # noqa: BLE001
            last = "quá 45 giây chưa trả lời" if "timed out" in str(exc).lower() else str(exc)[:120]
    raise RuntimeError(last or "không rõ lý do")


@bp.route("/ideas", methods=["POST"])
def ideas():
    """
    Gợi ý ý tưởng theo phong cách và kiểu truyện đã chọn, cho người chưa có ý
    tưởng nào. Hỏi thẳng AI viết chữ của tool vì chỉ là một lượt ngắn: không
    mở trình duyệt, không chiếm chỗ "mỗi lúc một việc" của xưởng.
    """
    data = _body()
    uid = str(data.get("universe_id") or "")
    uni = store.get_universe(uid) if uid else None
    if uid and not uni:
        return _loi("Không có vũ trụ này.", 404)
    pack = store.get_universe_pack(uid) if uni else store.get_pack(str(data.get("style_id") or ""))
    if not pack:
        return _loi("Hãy chọn một phong cách.")
    settings = _AI_SETTINGS({})

    def ask(system, user, max_tokens=1500):
        return _hoi_nhanh(system, user, settings, max_tokens)

    taken = [str(x)[:120] for x in (data.get("taken") or []) if x][:12]
    if uni:
        taken += [s["title"] for s in store.list_series(uid)]
    try:
        got = director.suggest_ideas(ask, pack, store.get_story_format(data.get("story_format")), uni, taken)
    except Exception as exc:  # noqa: BLE001
        return _loi("AI chưa gợi ý được: " + str(exc)[:200] + ". Bạn vẫn để trống ô ý tưởng được, AI sẽ tự nghĩ lúc dựng.", 502)
    if not got:
        return _loi("AI trả lời nhưng không có ý tưởng nào dùng được. Bấm gợi ý lại.", 502)
    return jsonify({"ok": True, "ideas": got})


@bp.route("/create", methods=["POST"])
def create():
    """
    Tạo series mới. Có `universe_id` thì series vào vũ trụ đó; không có thì AI
    dựng luôn vũ trụ mới theo bộ phong cách đã chọn rồi mới dựng series.
    """
    data = _body()
    uid = str(data.get("universe_id") or "")
    pack = None
    if uid:
        if not store.get_universe(uid):
            return _loi("Không có vũ trụ này.", 404)
    else:
        pack = store.get_pack(str(data.get("style_id") or ""))
        if not pack:
            return _loi("Hãy chọn một phong cách.")
        pack = {k: v for k, v in pack.items() if k != "custom"}
    episodes = max(3, min(100, int(data.get("episodes") or 10)))
    # Series mới: một giọng kể hết, tập ngắn. Series cũ giữ cách kể của nó cho tới khi được đổi.
    data = {"narrated": True, **data}
    if not data.get("episode_minutes") and not data.get("shots_per_episode"):
        data["episode_minutes"] = DEFAULT_MINUTES
    settings = _series_settings(data)

    def worker(job):
        ask = _ask(job)
        vu_tru = uid
        if not vu_tru:
            pack_dict = pack or {}
            uni = director.make_universe(ask, pack_dict, str(data.get("universe_idea") or data.get("idea") or ""),
                                         tell=_tien(job, 5, 35))
            vu_tru = store.create_universe(uni, pack_dict)
            job["result"] = {"universe_id": vu_tru}
        if data.get("universe_only"):
            return
        draft = director.make_series(ask, vu_tru, str(data.get("idea") or ""), episodes, tell=_tien(job, 40, 95),
                                     story_format=store.get_story_format(settings["story_format"]))
        sid = director.save_series_draft(vu_tru, draft, settings)
        job["result"] = {"universe_id": vu_tru, "series_id": sid}
    return _bat_dau("mx_create", worker)


@bp.route("/universe/<uid>", methods=["GET", "POST"])
def universe(uid):
    if not store.get_universe(uid):
        return _loi("Không có vũ trụ này.", 404)
    if request.method == "POST":
        data = _body()
        if isinstance(data.get("universe"), dict):
            store.save_universe(uid, data["universe"])
        if isinstance(data.get("pack"), dict):
            store.save_universe_pack(uid, data["pack"])
    return jsonify({"ok": True, "universe": store.get_universe(uid), "pack": store.get_universe_pack(uid),
                    "lore": store.get_lore(uid), "series": store.list_series(uid)})


def _series_view(uid, sid):
    seasons = []
    for s in store.list_seasons(uid, sid):
        n = int(s.get("number") or 0)
        eps = {}
        for e in store.list_episode_numbers(uid, sid, n):
            ep = store.get_episode(uid, sid, n, e) or {}
            shots = store.get_shots(uid, sid, n, e)
            eps[e] = {"title": ep.get("title"), "status": ep.get("status"), "approved": ep.get("approved"),
                      "total": len(shots), "completed": sum(1 for x in shots if x.get("status") == "completed"),
                      "conflicts": len(continuity.blocking(ep.get("conflicts") or [])),
                      "closed": bool(store.get_state(uid, sid, n, e)), "final_video": ep.get("final_video")}
        seasons.append(dict(s, episodes=eps))
    return {"ok": True, "universe": store.get_universe(uid), "pack": store.get_universe_pack(uid),
            "series": store.get_series(uid, sid), "bible": {k: store.list_entities(uid, sid, k)
                                                           for k in store.ENTITY_KINDS},
            "seasons": seasons, "memory": store.get_memory(uid, sid), "formats": store.list_story_formats(),
            "dashboard": continuity.dashboard(uid, sid), "job_id": _viec_dang_chay()}


@bp.route("/series/<uid>/<sid>", methods=["GET", "POST"])
def series(uid, sid):
    if not store.get_series(uid, sid):
        return _loi("Không có series này.", 404)
    if request.method == "POST":
        data = _body()
        if isinstance(data.get("series"), dict):
            old = store.get_series(uid, sid) or {}
            merged = {**old, **data["series"]}
            store.save_series(uid, sid, {**merged, **_series_settings(merged)})
    return jsonify(_series_view(uid, sid))


@bp.route("/series/<uid>/<sid>/entity/<kind>", methods=["POST"])
def entity_save(uid, sid, kind):
    data = _body().get("data")
    if not isinstance(data, dict) or not str(data.get("name") or "").strip():
        return _loi("Thiếu tên.")
    eid = store.save_entity(uid, sid, kind, data)
    store.rebuild_lore(uid)
    return jsonify({"ok": True, "id": eid, "data": store.get_entity(uid, sid, kind, eid)})


@bp.route("/series/<uid>/<sid>/character/<cid>/lock", methods=["POST"])
def character_lock(uid, sid, cid):
    char = store.lock_character(uid, sid, cid, bool(_body().get("locked", True)))
    return jsonify({"ok": True, "data": char})


@bp.route("/series/<uid>/<sid>/character/<cid>/state", methods=["POST"])
def character_state(uid, sid, cid):
    state = _body().get("state")
    if not isinstance(state, dict):
        return _loi("Thiếu trạng thái.")
    # Ghi mốc: trạng thái thêm tay có hiệu lực từ sau tập đã chốt gần nhất.
    da_chot = store.closed_tags(uid, sid)
    state = dict(state, from_episode=None, after_episode=da_chot[-1] if da_chot else None)
    return jsonify({"ok": True, "data": store.add_character_state(uid, sid, cid, state)})


# Tên file của chân dung nhiều góc do tool vẽ kết thúc bằng đuôi này. Tấm kiểu cũ (hai góc) không có
# đuôi này và sẽ được vẽ lại; ảnh người dùng tự tải lên (tên không bắt đầu bằng "ref_v") thì không đụng.
SHEET_SUFFIX = "_xoay.png"


def _portrait_prompt(uid, char):
    """
    Chân dung mẫu nhiều góc trong MỘT tấm: bốn góc toàn thân và bốn góc cận mặt của cùng một người.
    Người dùng yêu cầu 08/10/2026 ("khuôn mặt đa chiều trái phải, trước, sau lưng để nó không làm
    sai"). Mọi góc nằm chung một tấm vì dịch vụ vẽ bỏ qua ảnh mẫu đưa vào (đo cùng ngày): vẽ hai
    tấm riêng là ra hai khuôn mặt khác nhau.
    """
    pack = store.get_universe_pack(uid)
    state = continuity._state_of(char)
    return (f"{prompt_engine.MEDIUM} {pack.get('identity')}. "
            "Character turnaround model sheet for an animation production, 3D animated character model render. "
            f"{prompt_engine.identity_block(char)} Wardrobe: {state.get('costume')}. "
            "ONE single character shown from several angles, always the same person with the same face, hair and costume. "
            "Top row, four full-body views standing straight in a neutral pose, evenly spaced: front view, left side view, "
            "right side view, back view. Bottom row, four head-and-shoulders close-ups with a neutral expression: front, "
            "three-quarter view, left profile, right profile. Plain light grey studio background, even soft lighting, "
            "no scenery, no props in hand, no labels, no text. " + prompt_engine.NOT_LIVE)


def _can_ve_chan_dung(char):
    """Nhân vật cần tool vẽ chân dung: chưa có ảnh nào, hoặc chỉ có tấm kiểu cũ do tool vẽ."""
    refs = char.get("refs") or []
    return not refs or all(r.startswith("ref_v") and not r.endswith(SHEET_SUFFIX) for r in refs)


# Chân dung mẫu vẽ bằng Gemini (người dùng yêu cầu 08/10/2026, lúc Codex trong 9Router hết hạn mức
# rồi mất đăng nhập). Thử lần lượt các model dưới đây; model nào khoá không dùng được thì qua cái kế.
GEMINI_IMAGE_MODELS = ("gemini-3.1-flash-image", "gemini-2.5-flash-image", "gemini-3-pro-image")
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


# Đường vẽ chính: model ảnh của Gemini đi qua tài khoản Antigravity trong 9Router (người dùng đưa
# cấu hình này 08/10/2026; đo thật: 10 đến 13 giây một ảnh, xin 1536x1024 thì ra ảnh ngang 1264x848,
# dữ liệu trả về là JPEG dù xin PNG). Khoá Gemini riêng trong config.json là gói miễn phí, không vẽ được.
ROUTER_GEMINI_IMAGE_MODEL = "ag/gemini-3.1-flash-image"


# Đường ag/ có hạn lượt vẽ: hết thì dịch vụ báo "exhausted your capacity ... reset after 4h18m14s"
# (đo 08/10/2026: khoảng mười tấm là hết). Nhớ giờ có lại để không gọi vô ích cho từng nhân vật ở
# mỗi lượt quay; chỉ nhớ khi phải chờ lâu, còn "reset after 12s" là nghẽn tạm thì bỏ qua.
_HET_LUOT_VE = {"toi": 0.0}
HET_LUOT_TOI_THIEU = 300


def _giay_cho_co_lai(text):
    """Số giây phải chờ đọc từ câu "reset after 4h18m14s"; không có thì 0."""
    import re
    m = re.search(r"reset after\s+((?:\d+h)?(?:\d+m)?(?:\d+s)?)", str(text or ""))
    if not m or not m.group(1):
        return 0
    return sum(int(so) * he for so, he in ((x, {"h": 3600, "m": 60, "s": 1}[d]) for x, d in re.findall(r"(\d+)([hms])", m.group(1))))


def _cau_het_luot_ve():
    import time
    return ("đường vẽ của Antigravity đã hết lượt, có lại khoảng "
            + time.strftime("%H:%M", time.localtime(_HET_LUOT_VE["toi"])) + ". Tới lúc đó tool tự vẽ tiếp")


def _ve_bang_9router_gemini(prompt, out_path, settings=None, post=None):
    """Vẽ một ảnh ngang bằng model ảnh của Gemini qua 9Router, ghi ra file PNG thật. Hỏng thì ném lỗi."""
    import base64
    import io
    import time
    import requests
    if time.time() < _HET_LUOT_VE["toi"]:
        raise RuntimeError(_cau_het_luot_ve())
    settings = settings or _AI_SETTINGS({})
    base = str(settings.get("base_url") or "http://localhost:20128/v1").rstrip("/")
    res = (post or requests.post)(base + "/images/generations",
                                  headers={"Authorization": f"Bearer {settings.get('api_key') or '9router-key'}"},
                                  json={"model": ROUTER_GEMINI_IMAGE_MODEL, "prompt": prompt, "n": 1, "size": "1536x1024",
                                        "quality": "auto", "background": "auto", "image_detail": "high", "output_format": "png"},
                                  timeout=240)
    data = res.json()
    if res.status_code != 200:
        loi = str((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data)
        cho = _giay_cho_co_lai(loi)
        if ("exhausted your capacity" in loi or "QUOTA_EXHAUSTED" in loi) and cho >= HET_LUOT_TOI_THIEU:
            _HET_LUOT_VE["toi"] = time.time() + cho
            raise RuntimeError(_cau_het_luot_ve())
        raise RuntimeError(f"9Router {ROUTER_GEMINI_IMAGE_MODEL}: HTTP {res.status_code}: {loi[:160]}")
    blob = ((data.get("data") or [{}])[0]).get("b64_json")
    if not blob:
        raise RuntimeError(f"9Router {ROUTER_GEMINI_IMAGE_MODEL} trả lời nhưng không kèm ảnh")
    raw = base64.b64decode(blob)
    try:
        # File mang đuôi .png thì ruột cũng phải là PNG: Muse nhận ảnh đính kèm theo đuôi file.
        from PIL import Image
        Image.open(io.BytesIO(raw)).convert("RGB").save(out_path, "PNG")
    except Exception:  # noqa: BLE001 - thiếu PIL hay ảnh lạ thì ghi nguyên, vẫn hơn là không có ảnh
        with open(out_path, "wb") as f:
            f.write(raw)
    return ROUTER_GEMINI_IMAGE_MODEL


def _khoa_gemini():
    """Khoá Gemini riêng của người dùng trong config.json (ô api_key), hoặc biến môi trường."""
    import json
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8") as f:
            key = str(json.load(f).get("api_key") or "").strip()
    except Exception:  # noqa: BLE001
        key = ""
    return key or os.environ.get("GEMINI_API_KEY", "").strip()


def _ve_bang_gemini(prompt, out_path, key=None, post=None):
    """Vẽ một ảnh ngang bằng Gemini và ghi ra file. Trả về tên model đã vẽ; hỏng hết thì ném lỗi kèm lý do."""
    import base64
    import requests
    key = key or _khoa_gemini()
    if not key:
        raise RuntimeError("chưa có khoá Gemini trong cấu hình")
    post = post or requests.post
    last = ""
    for model in GEMINI_IMAGE_MODELS:
        try:
            res = post(GEMINI_API.format(model=model), headers={"x-goog-api-key": key},
                       json={"contents": [{"parts": [{"text": prompt}]}],
                             "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}}},
                       timeout=180)
            data = res.json()
            if res.status_code != 200:
                raise ValueError(f"HTTP {res.status_code}: {str((data.get('error') or {}).get('message') or data)[:160]}")
            for cand in data.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    blob = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
                    if blob:
                        with open(out_path, "wb") as f:
                            f.write(base64.b64decode(blob))
                        return model
            raise ValueError("Gemini trả lời nhưng không kèm ảnh: " + str(data.get("promptFeedback") or "")[:120])
        except Exception as exc:  # noqa: BLE001
            last = f"{model}: {str(exc)[:200]}"
    if "429" in last or "quota" in last.lower():
        # Đo 08/10/2026: khoá gói miễn phí trả 429 "exceeded your current quota" cho MỌI model vẽ, ngay lập tức.
        raise RuntimeError("khoá Gemini trong cấu hình không có hạn mức vẽ tranh (gói miễn phí không vẽ được). "
                           "Bật thanh toán cho khoá đó hoặc đổi sang khoá có trả phí")
    raise RuntimeError("Gemini không vẽ được (" + last + ")")


def _ve_chan_dung(uid, sid, cid):
    """
    Vẽ một chân dung mẫu cho nhân vật, ghi vào sổ, trả về tên file. Thứ tự: model ảnh của Gemini
    qua 9Router (đường ag/), rồi khoá Gemini riêng, rồi Codex qua 9Router. Hỏng hết thì ném lỗi
    ghi rõ từng bên hỏng vì gì.
    """
    import longform_studio as lf
    char = store.get_entity(uid, sid, "characters", cid) or {}
    folder = store.character_refs_dir(uid, sid, cid)
    folder.mkdir(parents=True, exist_ok=True)
    n = len(list(folder.glob("*.png"))) + 1
    name = f"ref_v{(char.get('identity') or {}).get('version') or 1}_{n:02d}{SHEET_SUFFIX}"
    prompt = _portrait_prompt(uid, char)
    loi = []
    for ve in (_ve_bang_9router_gemini, _ve_bang_gemini):
        try:
            ve(prompt, str(folder / name))
            break
        except Exception as exc:  # noqa: BLE001
            loi.append(str(exc)[:200])
    else:
        got = lf.generate_image_router(prompt, str(folder / name), _AI_SETTINGS({}),
                                       fmt={"image_width": 1792, "image_height": 1024})
        if not got.get("success"):
            raise RuntimeError("; ".join(loi) + f"; Codex: {str(got.get('error') or 'vẽ không được')[:160]}")
    store.add_character_ref(uid, sid, cid, name)
    # Tấm mới là ảnh mẫu chính (Muse nhận ảnh đầu danh sách). Các tấm cũ do tool vẽ thôi không dùng nữa
    # vì khác khuôn mặt; file vẫn còn trên đĩa. Ảnh người dùng tự tải lên được giữ, xếp sau.
    char = store.get_entity(uid, sid, "characters", cid) or {}
    char["refs"] = [name] + [r for r in char.get("refs") or [] if r != name and not r.startswith("ref_v")]
    store.save_entity(uid, sid, "characters", char)
    return name


def _chan_dung_truoc_khi_quay(job, uid, sid, season, ep, shot_ids=None):
    """
    Trước khi quay, nhân vật nào sắp lên hình mà chưa có chân dung mẫu thì tool tự vẽ và khoá
    ngoại hình. Ngày 08/10/2026 cả tập 1 quay không kèm ảnh nào (chưa ai bấm vẽ), và mỗi cảnh
    Muse vẽ một khuôn mặt khác. Vẽ hỏng thì ghi lại rồi quay tiếp bằng chữ, không dừng cả lượt.
    """
    shots = [s for s in store.get_shots(uid, sid, season, ep)
             if (s.get("shot_id") in shot_ids if shot_ids else s.get("status") != "completed")]
    bib = store.bible(uid, sid)
    can = []
    for s in shots:
        for cid in s.get("characters") or []:
            char = bib["characters"].get(cid)
            if char and _can_ve_chan_dung(char) and cid not in can:
                can.append(cid)
    hong = []
    het_luot = ""
    for i, cid in enumerate(can):
        ten = bib["characters"][cid].get("name") or cid
        if het_luot:
            # Người trước đã hết lượt vẽ thì người sau cũng vậy: ghi cùng lý do, không gọi thêm lượt nào.
            hong.append(ten)
            _LOI_CHAN_DUNG[(uid, sid, cid)] = het_luot
            continue
        _step(job, 1, f"Đang vẽ chân dung mẫu của {ten} ({i + 1}/{len(can)}) để Muse giữ đúng mặt...")
        try:
            _ve_chan_dung(uid, sid, cid)
            store.lock_character(uid, sid, cid, True)
            _LOI_CHAN_DUNG.pop((uid, sid, cid), None)
        except Exception as exc:  # noqa: BLE001
            hong.append(ten)
            _LOI_CHAN_DUNG[(uid, sid, cid)] = str(exc)[:240]
            if "hết lượt" in str(exc):
                het_luot = _cau_het_luot_ve() if _HET_LUOT_VE["toi"] else str(exc)[:240]
                _LOI_CHAN_DUNG[(uid, sid, cid)] = het_luot
            _step(job, 1, f"Chưa vẽ được chân dung của {ten}: {str(exc)[:140]}. Cảnh có {ten} sẽ quay bằng chữ tả.")
    return {"ve": [c for c in can if bib['characters'][c].get('name') not in hong], "hong": hong}


@bp.route("/series/<uid>/<sid>/character/<cid>/portrait", methods=["POST"])
def character_portrait(uid, sid, cid):
    char = store.get_entity(uid, sid, "characters", cid)
    if not char:
        return _loi("Không có nhân vật này.", 404)

    def worker(job):
        _step(job, 20, f"Đang vẽ chân dung mẫu của {char.get('name')}...")
        job["result"] = {"ref": _ve_chan_dung(uid, sid, cid)}
    return _bat_dau("mx_portrait", worker)


@bp.route("/series/<uid>/<sid>/character/<cid>/ref", methods=["POST"])
def character_ref_upload(uid, sid, cid):
    f = request.files.get("file")
    if not f or not f.filename:
        return _loi("Không thấy file ảnh.")
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return _loi("Chỉ nhận ảnh png, jpg, webp.")
    folder = store.character_refs_dir(uid, sid, cid)
    name = f"upload_{len(list(folder.iterdir())) + 1:02d}{ext}"
    f.save(str(folder / name))
    store.add_character_ref(uid, sid, cid, name)
    return jsonify({"ok": True, "ref": name})


@bp.route("/series/<uid>/<sid>/character/<cid>/ref/<name>", methods=["GET", "DELETE"])
def character_ref(uid, sid, cid, name):
    folder = store.character_refs_dir(uid, sid, cid)
    if request.method == "DELETE":
        # Bỏ khỏi danh sách dùng làm mẫu; file vẫn nằm trên đĩa.
        char = store.get_entity(uid, sid, "characters", cid) or {}
        char["refs"] = [r for r in char.get("refs") or [] if r != name]
        store.save_entity(uid, sid, "characters", char)
        return jsonify({"ok": True})
    return send_from_directory(folder, os.path.basename(name))


@bp.route("/series/<uid>/<sid>/memory", methods=["POST"])
def memory_save(uid, sid):
    mem = _body().get("memory")
    if not isinstance(mem, dict):
        return _loi("Thiếu nội dung.")
    store.save_memory(uid, sid, mem)
    return jsonify({"ok": True, "memory": store.get_memory(uid, sid)})


@bp.route("/series/<uid>/<sid>/season", methods=["POST"])
def season_new(uid, sid):
    """Mùa mới do AI lên kế hoạch từ chỗ truyện đang đứng."""
    data = _body()
    so = max([int(s.get("number") or 0) for s in store.list_seasons(uid, sid)] or [0]) + 1
    episodes = max(3, min(100, int(data.get("episodes") or 10)))

    def worker(job):
        season = director.make_season(_ask(job), uid, sid, so, episodes, str(data.get("idea") or ""),
                                      tell=_tien(job, 10, 95))
        store.save_season(uid, sid, so, season)
        job["result"] = {"season": so}
    return _bat_dau("mx_season", worker)


@bp.route("/series/<uid>/<sid>/season/<int:season>", methods=["POST"])
def season_save(uid, sid, season):
    data = _body().get("season")
    old = store.get_season(uid, sid, season)
    if not isinstance(data, dict) or not old:
        return _loi("Không có mùa này.", 404)
    store.save_season(uid, sid, season, dict(old, **data))
    return jsonify({"ok": True, "season": store.get_season(uid, sid, season)})


# ------------------------------------------------------------------------------
# Tập phim
# ------------------------------------------------------------------------------

EP = "/episode/<uid>/<sid>/<int:season>/<int:ep>"
SHOT_FIELDS = ("action", "camera", "lighting", "vfx", "line", "characters", "props", "scene",
               "duration", "state_overrides", "emotion")


# Lỗi vẽ chân dung gần nhất của từng nhân vật, để trang hiện ngay trên thẻ của người đó.
_LOI_CHAN_DUNG = {}


def _dan_nhan_vat(uid, sid, season, ep):
    """
    Nhân vật của tập (tập chưa viết thì cả series) kèm chân dung mẫu đang dùng, để trang hiện cho
    người dùng xem ngay ở đầu trang tập mà không phải bấm vào đâu.
    """
    bib = store.bible(uid, sid)
    shots = store.get_shots(uid, sid, season, ep)
    ids = []
    for s in shots:
        for cid in s.get("characters") or []:
            if cid in bib["characters"] and cid not in ids:
                ids.append(cid)
    ids = ids or list(bib["characters"])
    out = []
    for cid in ids:
        char = bib["characters"][cid]
        refs = char.get("refs") or []
        kieu = "chua_co" if not refs else "kieu_cu" if _can_ve_chan_dung(char) else "nhieu_goc" if refs[0].endswith(SHEET_SUFFIX) else "tu_tai"
        out.append({"id": cid, "name": char.get("name") or cid, "ref": refs[0] if refs else "", "kind": kieu,
                    "locked": bool((char.get("identity") or {}).get("locked")),
                    "shots": sum(1 for s in shots if cid in (s.get("characters") or [])),
                    "error": _LOI_CHAN_DUNG.get((uid, sid, cid), "")})
    return out


def _episode_view(uid, sid, season, ep):
    episode = store.get_episode(uid, sid, season, ep)
    manager = EpisodeShotManager(uid, sid, season, ep)
    return {"ok": True, "episode": episode, "shots": store.get_shots(uid, sid, season, ep),
            "cast": _dan_nhan_vat(uid, sid, season, ep),
            "can_resume": (store.episode_dir(uid, sid, season, ep) / "writing.json").exists(),
            "state_before": continuity.state_before(uid, sid, season, ep),
            "state_after": store.get_state(uid, sid, season, ep),
            "progress": manager.status(), "job_id": _viec_dang_chay(),
            "plan": next((p for p in (store.get_season(uid, sid, season) or {}).get("plan") or []
                          if int(p.get("number") or 0) == ep), None)}


@bp.route(EP, methods=["GET"])
def episode_get(uid, sid, season, ep):
    return jsonify(_episode_view(uid, sid, season, ep))


@bp.route(EP + "/write", methods=["POST"])
def episode_write(uid, sid, season, ep):
    data = _body()
    if not store.get_season(uid, sid, season):
        return _loi("Không có mùa này.", 404)

    def worker(job):
        _step(job, 5, "Đang nạp sổ series, trạng thái tập trước và trí nhớ truyện...")
        director.write_episode(_ask(job), uid, sid, season, ep, note=str(data.get("note") or ""),
                               shots=int(data["shots"]) if data.get("shots") else None, tell=_tien(job, 10, 92),
                               fresh=bool(data.get("fresh")))
        _step(job, 95, "Đã soát liên tục và ghép câu lệnh cho từng cảnh.")
    return _bat_dau("mx_write", worker)


@bp.route(EP + "/save", methods=["POST"])
def episode_save(uid, sid, season, ep):
    """Người dùng sửa tay kịch bản, cảnh quay hoặc câu lệnh. Sửa xong là soát lại."""
    data = _body()
    episode = store.get_episode(uid, sid, season, ep)
    if not episode:
        return _loi("Tập này chưa có kịch bản.", 404)
    if isinstance(data.get("episode"), dict):
        for key in ("title", "summary", "objective", "beats", "scenes", "ending"):
            if key in data["episode"]:
                episode[key] = data["episode"][key]
        # Kịch bản đổi thì lần duyệt cũ không còn giá trị.
        episode["approved"] = False
        store.save_episode(uid, sid, season, ep, episode)
    shots = store.get_shots(uid, sid, season, ep)
    by_id = {s["shot_id"]: s for s in shots}
    for change in data.get("shots") or []:
        shot = by_id.get(str(change.get("shot_id") or ""))
        if not shot:
            continue
        if shot.get("status") == "completed" and any(k in change for k in SHOT_FIELDS):
            # Cảnh đã quay giữ nguyên nội dung; muốn đổi thì bấm quay lại cảnh đó.
            shot["status"] = "pending"
        for key in SHOT_FIELDS:
            if key in change:
                shot[key] = change[key]
        if "prompt" in change:
            shot["prompt"] = str(change["prompt"])
            shot["prompt_edited"] = bool(str(change["prompt"]).strip())
        if change.get("reset_prompt"):
            shot["prompt_edited"] = False
    store.save_shots(uid, sid, season, ep, shots)
    director.recheck(uid, sid, season, ep)
    return jsonify(_episode_view(uid, sid, season, ep))


@bp.route(EP + "/check", methods=["POST"])
def episode_check(uid, sid, season, ep):
    director.recheck(uid, sid, season, ep)
    return jsonify(_episode_view(uid, sid, season, ep))


@bp.route(EP + "/conflict", methods=["POST"])
def episode_conflict(uid, sid, season, ep):
    data = _body()
    cid, decision = str(data.get("id") or ""), str(data.get("decision") or "")
    if decision == "change_scene":
        def worker(job):
            _step(job, 30, "Đạo diễn AI đang sửa cảnh cho đúng luật đã định...")
            director.fix_conflict(_ask(job), uid, sid, season, ep, cid)
        return _bat_dau("mx_fix", worker)
    director.settle_conflict(uid, sid, season, ep, cid, decision, str(data.get("note") or ""))
    director.recheck(uid, sid, season, ep)
    return jsonify(_episode_view(uid, sid, season, ep))


@bp.route(EP + "/approve", methods=["POST"])
def episode_approve(uid, sid, season, ep):
    episode = director.recheck(uid, sid, season, ep)
    want = bool(_body().get("approved", True))
    if want and continuity.blocking(episode.get("conflicts") or []):
        return _loi("Còn xung đột liên tục chưa xử lý, chưa duyệt được.")
    store.save_episode(uid, sid, season, ep, dict(episode, approved=want))
    return jsonify(_episode_view(uid, sid, season, ep))


def _viet_lai_canh(uid, sid):
    """
    Bộ viết lại cảnh cho lúc Muse từ chối. Dùng AI viết chữ của tool chứ không qua trình duyệt:
    lượt quay chạy lâu và không có ai ngồi canh, mở thêm cửa sổ ChatGPT giữa chừng dễ hỏng.
    """
    import longform_studio as lf
    settings = _AI_SETTINGS({})

    def ask(system, user, max_tokens=900):
        # Đường nhanh trước (45 giây mỗi lượt): lượt quay đang đứng chờ câu trả lời này.
        try:
            return _hoi_nhanh(system, user, settings, max_tokens)
        except Exception:  # noqa: BLE001 - chậm hay hỏng thì qua đường kiên nhẫn
            pass
        got = lf.ask_json(system, user, settings, max_tokens=max_tokens)
        if not got.get("success") or not isinstance(got.get("data"), dict):
            raise RuntimeError("AI không viết lại được: " + (got.get("error") or "không rõ lý do"))
        return got["data"]
    bib = store.bible(uid, sid)
    names: dict[str, str] = {str(i): str(e.get("name") or "") for kind in ("characters", "props", "locations") for i, e in (bib.get(kind) or {}).items()}
    return lambda shot, scene, reason, level: director.soften_shot(ask, shot, scene, reason, level, names)


def _muse_job(kind, uid, sid, season, ep, run, shot_ids=None):
    def worker(job):
        _chan_dung_truoc_khi_quay(job, uid, sid, season, ep, shot_ids)

        async def go():
            studio = _may_quay(uid, sid)
            try:
                manager = EpisodeShotManager(uid, sid, season, ep, studio=studio, rewrite=_viet_lai_canh(uid, sid))
                await run(manager, job)
            finally:
                await studio.close()
        asyncio.run(go())
    return _bat_dau(kind, worker)


@bp.route(EP + "/generate", methods=["POST"])
def episode_generate(uid, sid, season, ep):
    """Quay mọi cảnh chưa xong. Cảnh xong rồi không quay lại, nên đây cũng là nút chạy tiếp."""
    EpisodeShotManager(uid, sid, season, ep).ready()

    async def run(manager, job):
        manager.audit()
        total = max(1, manager.status()["total"])

        def say(shot_id, text):
            _step(job, round(100 * manager.status()["completed"] / total), f"{shot_id}: {text}")
        await manager.generate_all(progress=say, cancelled=lambda: bool(job.get("cancel")))
        job["result"] = manager.status()
    return _muse_job("mx_muse", uid, sid, season, ep, run)


AUTO_FIX_ROUNDS = 3
# Sau lượt quay đầu, cảnh nào còn thiếu thì quay bù thêm ngần này lượt rồi mới ghép. Lời từ chối
# của Muse có lúc thất thường: ngày 08/10/2026 cảnh 017 bị từ chối lần đầu, lần sau qua ngay.
AUTO_RETRY_PASSES = 1


def _tu_xu_ly_mau_thuan(job, ask, uid, sid, season, ep):
    """
    Tự xử lý mọi mâu thuẫn đang chặn, không hỏi người dùng. Trước hết cho Đạo diễn AI sửa cảnh
    cho đúng luật đã định (luật của vũ trụ thắng). Sửa mấy vòng mà mâu thuẫn về luật vẫn còn
    thì nhận tập như đã viết. Thiếu dữ liệu trong sổ thì không bỏ qua được: trả về số còn lại.
    """
    for vong in range(AUTO_FIX_ROUNDS):
        episode = director.recheck(uid, sid, season, ep)
        chan = continuity.blocking(episode.get("conflicts") or [])
        if not chan:
            return 0
        for c in chan:
            _step(job, 22, f"Tự sửa mâu thuẫn ({vong + 1}/{AUTO_FIX_ROUNDS}): {str(c.get('new') or c.get('where') or '')[:90]}")
            try:
                director.fix_conflict(ask, uid, sid, season, ep, c["id"])
            except Exception as exc:  # noqa: BLE001 - sửa hỏng một cái thì thử cái khác, vòng sau soát lại
                _step(job, 22, f"Chưa sửa được: {str(exc)[:120]}")
    episode = director.recheck(uid, sid, season, ep)
    for c in continuity.blocking(episode.get("conflicts") or []):
        if c.get("kind") == "lore":
            director.settle_conflict(uid, sid, season, ep, c["id"], "keep")
    return len(continuity.blocking(director.recheck(uid, sid, season, ep).get("conflicts") or []))


@bp.route(EP + "/auto", methods=["POST"])
def episode_auto(uid, sid, season, ep):
    """
    Làm một lèo cả tập, không dừng lại hỏi: viết, tự xử lý mâu thuẫn, tự duyệt, tự vẽ chân dung
    và khoá mặt, quay, ghép, lồng tiếng, dựng bản có phụ đề, chốt tập. Bước nào xong rồi thì bỏ
    qua, nên bấm lại là chạy tiếp từ chỗ dở. Người dùng yêu cầu 08/10/2026: "mình không muốn
    đụng tay vào", "phải duyệt quá nhiều, trông không như tự động".
    """
    from video.xianxia import dub
    if not store.get_season(uid, sid, season):
        return _loi("Không có mùa này.", 404)
    data = _body()
    key = (uid, sid, season, ep)

    def dung(job):
        if job.get("cancel"):
            raise RuntimeError("Đã dừng theo yêu cầu. Bấm lại nút tự động để chạy tiếp từ chỗ này.")

    def worker(job):
        ghi_chu = []
        # 1. Viết
        if not store.get_episode(*key) or (store.episode_dir(*key) / "writing.json").exists():
            _step(job, 3, "Bước 1/7: AI viết tập...")
            director.write_episode(_ask(job), uid, sid, season, ep, note=str(data.get("note") or ""), tell=_tien(job, 4, 20))
        dung(job)
        # 2. Soát và tự xử lý mâu thuẫn, 3. tự duyệt
        episode = store.get_episode(*key) or {}
        if not episode.get("approved"):
            _step(job, 21, "Bước 2/7: soát liên tục và tự xử lý mâu thuẫn...")
            con = _tu_xu_ly_mau_thuan(job, _ask(job), uid, sid, season, ep)
            if con:
                raise RuntimeError(f"Còn {con} chỗ thiếu dữ liệu trong sổ mà tool chưa tự sửa được. Mở thẻ Liên tục để xem, "
                                   "xử lý xong bấm lại nút tự động.")
            store.save_episode(*key, dict(store.get_episode(*key) or {}, approved=True))
        dung(job)
        # 4. Chân dung, khoá mặt, 5. quay
        manager = EpisodeShotManager(*key)
        # Soát các clip đã có trước khi quyết định còn phải quay gì: clip mất, sai khổ, sai độ dài
        # bị đưa về "chưa quay".
        sai = manager.audit()
        if sai:
            ghi_chu.append(f"Soát thấy {len(sai)} clip sai ({', '.join(x['shot_id'] for x in sai[:6])}) và đã quay lại.")
        if manager.status()["completed"] < manager.status()["total"]:
            _step(job, 25, "Bước 3/7: vẽ chân dung mẫu và khoá mặt nhân vật...")
            ve = _chan_dung_truoc_khi_quay(job, uid, sid, season, ep)
            if ve["hong"]:
                ghi_chu.append("Chưa vẽ được chân dung của " + ", ".join(ve["hong"]) + " nên các cảnh có họ quay bằng chữ tả.")
            dung(job)

            async def quay():
                studio = _may_quay(uid, sid)
                try:
                    m = EpisodeShotManager(*key, studio=studio, rewrite=_viet_lai_canh(uid, sid))
                    tong = max(1, m.status()["total"])
                    noi = lambda s, text: _step(job, 28 + round(47 * m.status()["completed"] / tong),  # noqa: E731
                                                f"Bước 4/7, quay {m.status()['completed']}/{tong}. {s}: {text}")
                    huy = lambda: bool(job.get("cancel"))  # noqa: E731
                    await m.generate_all(progress=noi, cancelled=huy)
                    # Soát clip vừa tải rồi quay bù những cảnh còn thiếu. Lượt bù hỏng thì thôi, vẫn
                    # dựng từ các cảnh đã có: không để vài cảnh Muse không chịu làm chặn cả tập.
                    for luot in range(AUTO_RETRY_PASSES):
                        m.audit()
                        tinh_hinh = m.status()
                        if huy() or tinh_hinh["completed"] >= tinh_hinh["total"]:
                            break
                        _step(job, 70, f"Còn {tinh_hinh['total'] - tinh_hinh['completed']} cảnh thiếu. Quay bù lượt {luot + 1}...")
                        try:
                            await m.generate_all(progress=noi, cancelled=huy)
                        except RuntimeError as exc:
                            ghi_chu.append(f"Lượt quay bù dừng sớm: {str(exc)[:160]}")
                            break
                    m.audit()
                finally:
                    await studio.close()
            asyncio.run(quay())
        dung(job)
        tinh = EpisodeShotManager(*key).status()
        if not tinh["completed"]:
            raise RuntimeError("Chưa quay được cảnh nào nên chưa ghép được. Xem lỗi của từng cảnh ở thẻ Cảnh quay.")
        if tinh["completed"] < tinh["total"]:
            ghi_chu.append(f"Còn {tinh['total'] - tinh['completed']} cảnh Muse không làm được; phim được dựng từ {tinh['completed']} cảnh đã xong.")
        # 6. Ghép
        _step(job, 76, "Bước 5/7: ghép các cảnh...")
        EpisodeShotManager(*key).assemble()
        dung(job)
        # 7. Lồng tiếng và dựng
        _step(job, 78, "Bước 6/7: chọn giọng và đọc lời...")
        if not dub.get_cast(uid, sid):
            dub.auto_cast(uid, sid, _voice_catalog())
        dub.synth(key, _voice_catalog(), _speak, _probe, progress=lambda pct, text: _step(job, 78 + round(pct * 0.1), text),
                  cancelled=lambda: bool(job.get("cancel")))
        vol = float(store.get_series(uid, sid).get("muse_volume", 0.25)) if store.get_series(uid, sid) else 0.25
        out = dub.render(key, muse_volume=vol, burn=True, progress=lambda pct, text: _step(job, 88 + round(pct * 0.1), text),
                         cancelled=lambda: bool(job.get("cancel")))
        # 8. Chốt tập
        _step(job, 99, "Bước 7/7: chốt tập để tập sau nối tiếp...")
        continuity.close_episode(uid, sid, season, ep)
        job["result"] = {"video": out.name, "notes": ghi_chu, **tinh}
        _tu_cat(*key)
        _step(job, 100, "Xong: " + out.name + ("" if not ghi_chu else ". Lưu ý: " + " ".join(ghi_chu)))
    return _bat_dau("mx_auto", worker)


@bp.route(EP + "/shot/<shot_id>/regenerate", methods=["POST"])
def shot_regenerate(uid, sid, season, ep, shot_id):
    EpisodeShotManager(uid, sid, season, ep).ready()

    async def run(manager, job):
        shot = await manager.generate_shot(shot_id, force=True,
                                           progress=lambda s, text: _step(job, 50, f"{s}: {text}"),
                                           cancelled=lambda: bool(job.get("cancel")))
        if shot.get("status") != "completed":
            raise RuntimeError(shot.get("error") or "Quay lại không thành công.")
    return _muse_job("mx_muse_one", uid, sid, season, ep, run, shot_ids=[shot_id])


@bp.route(EP + "/review", methods=["POST"])
def shot_review(uid, sid, season, ep):
    data = _body()
    verdict = str(data.get("verdict") or "")
    if verdict not in ("ok", "face", "costume", "other", ""):
        return _loi("Đánh giá không hợp lệ.")
    store.update_shot(uid, sid, season, ep, str(data.get("shot_id") or ""),
                      review={"verdict": verdict, "note": str(data.get("note") or "")} if verdict else {})
    return jsonify(_episode_view(uid, sid, season, ep))


@bp.route(EP + "/assemble", methods=["POST"])
def episode_assemble(uid, sid, season, ep):
    try:
        out = EpisodeShotManager(uid, sid, season, ep).assemble()
    except FileNotFoundError as exc:
        return _loi(exc)
    return jsonify(dict(_episode_view(uid, sid, season, ep), final_video=out.name))


@bp.route(EP + "/close", methods=["POST"])
def episode_close(uid, sid, season, ep):
    """Chốt tập: lưu episode_state.json làm điểm xuất phát cho tập sau."""
    continuity.close_episode(uid, sid, season, ep)
    return jsonify(_episode_view(uid, sid, season, ep))


# ------------------------------------------------------------------------------
# Xưởng dựng: dàn giọng, lời, phụ đề SRT, bản lồng tiếng
# ------------------------------------------------------------------------------

def _voice_catalog():
    """Giọng dùng được: giọng Edge đọc được tiếng Việt, rồi giọng trong Kho giọng nhân bản của người dùng."""
    import tts_engine
    out = []
    groups = {g["id"]: g["label"] for g in tts_engine.SHORTS_VOICE_GROUPS}
    for v in tts_engine.SHORTS_VOICE_PRESETS:
        if v["group"] == "native_global":
            continue
        desc = str(v.get("desc") or "")
        try:
            deep = -int(str(v.get("pitch") or "0").replace("Hz", ""))
        except ValueError:
            deep = 0
        out.append({"id": v["id"], "label": v["label"], "desc": desc, "group": groups.get(v["group"], ""),
                    "engine": "edge", "voice": v["voice"], "rate": v["rate"], "pitch": v["pitch"],
                    "gender": "m" if desc.startswith("Nam") else "f" if desc.startswith("Nữ") else "", "deep": deep})
    try:
        import voice_clone
        for v in voice_clone.list_voices():
            out.append({"id": f"clone:{v['id']}", "label": v.get("name") or v["id"], "desc": "Giọng nhân bản trong kho",
                        "group": "Kho giọng của bạn (đọc bằng máy này, chậm hơn)", "engine": "clone",
                        "voice": v["id"], "rate": "", "pitch": "", "gender": "", "deep": 0})
    except Exception as exc:  # noqa: BLE001 - kho giọng hỏng thì vẫn còn giọng Edge
        logging.getLogger(__name__).warning("Không đọc được kho giọng: %s", exc)
    return out


def _speak(text, voice, out_path):
    """Đọc một câu ra file bằng đúng bộ đọc của giọng đó."""
    if voice["engine"] == "clone":
        import voice_clone
        voice_clone.speak_to_file(voice["voice"], text, out_path)
        return
    import tts_engine
    if not tts_engine.generate_audio_sync(text, voice["voice"], out_path, rate=voice["rate"], pitch=voice["pitch"]):
        raise RuntimeError("Edge TTS không trả tiếng.")


def _probe(path):
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=30)
    return float(out.stdout.strip() or 0)


def _dub_view(uid, sid, season, ep):
    from video.xianxia import dub
    key = (uid, sid, season, ep)
    episode = store.get_episode(*key) or {}
    tl = dub.timeline(key)
    return {"ok": True, "cast": dub.get_cast(uid, sid), "speakers": dub.speakers(uid, sid),
            "catalog": _voice_catalog(), "timeline": tl, "job_id": _viec_dang_chay(),
            "dub_video": episode.get("dub_video"), "dub_shots": episode.get("dub_shots"),
            "dub_seconds": episode.get("dub_seconds"), "dub_burned": episode.get("dub_burned"),
            "publish": _publish_view(*key), "progress": EpisodeShotManager(*key).status()}


@bp.route(EP + "/dub", methods=["GET"])
def dub_get(uid, sid, season, ep):
    return jsonify(_dub_view(uid, sid, season, ep))


@bp.route("/series/<uid>/<sid>/cast", methods=["POST"])
def cast_save(uid, sid):
    """Lưu dàn giọng của series. `auto` thì tool tự dò giọng cho ai chưa có (hoặc dò lại hết nếu `reset`)."""
    from video.xianxia import dub
    data = _body()
    if isinstance(data.get("cast"), dict):
        dub.save_cast(uid, sid, data["cast"])
    if data.get("auto"):
        dub.auto_cast(uid, sid, _voice_catalog(), keep=not data.get("reset"))
    return jsonify({"ok": True, "cast": dub.get_cast(uid, sid)})


@bp.route("/voices/preview", methods=["POST"])
def voice_preview():
    """Nghe thử một giọng với tốc độ, cao độ đang chọn."""
    import hashlib
    data = _body()
    voice = next((v for v in _voice_catalog() if v["id"] == data.get("voice_id")), None)
    if not voice:
        return _loi("Không có giọng này.", 404)
    cfg = {"engine": voice["engine"], "voice": voice["voice"], "voice_id": voice["id"],
           "rate": str(data.get("rate") or voice["rate"] or "+0%"), "pitch": str(data.get("pitch") or voice["pitch"] or "+0Hz")}
    text = (str(data.get("text") or "").strip() or "Sư thúc, rốt cuộc người xong chưa vậy? Ngày mai là đại hội rồi.")[:300]
    folder = store.ROOT / "_nghe_thu"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.md5(f"{text}|{cfg}".encode("utf-8")).hexdigest()[:12]
    name = f"{digest}.{'wav' if cfg['engine'] == 'clone' else 'mp3'}"
    if not (folder / name).exists():
        try:
            _speak(text, cfg, str(folder / name))
        except Exception as exc:  # noqa: BLE001
            return _loi(f"Không đọc thử được: {exc}", 502)
    return jsonify({"ok": True, "url": f"/api/muse/xianxia/voices/preview/{name}"})


@bp.route("/voices/preview/<name>", methods=["GET"])
def voice_preview_file(name):
    return send_from_directory(store.ROOT / "_nghe_thu", os.path.basename(name))


@bp.route(EP + "/dub/lines", methods=["POST"])
def dub_lines(uid, sid, season, ep):
    from video.xianxia import dub
    dub.save_lines((uid, sid, season, ep), _body().get("changes") or [])
    return jsonify(_dub_view(uid, sid, season, ep))


@bp.route(EP + "/dub/synth", methods=["POST"])
def dub_synth(uid, sid, season, ep):
    from video.xianxia import dub
    key = (uid, sid, season, ep)
    if not dub.get_cast(uid, sid):
        dub.auto_cast(uid, sid, _voice_catalog())

    def worker(job):
        got = dub.synth(key, _voice_catalog(), _speak, _probe, progress=lambda pct, text: _step(job, pct, text),
                        cancelled=lambda: bool(job.get("cancel")))
        job["result"] = got
    return _bat_dau("mx_voice", worker)


@bp.route(EP + "/dub/srt", methods=["GET"])
def dub_srt(uid, sid, season, ep):
    from video.xianxia import dub
    path = dub.write_srt((uid, sid, season, ep), only_completed=request.args.get("ban_dung") == "1")
    return send_from_directory(path.parent, path.name, as_attachment=True)


@bp.route(EP + "/dub/audio/<name>", methods=["GET"])
def dub_audio(uid, sid, season, ep, name):
    from video.xianxia import dub
    return send_from_directory(dub.dub_dir((uid, sid, season, ep)) / "lines", os.path.basename(name))


@bp.route(EP + "/dub/render", methods=["POST"])
def dub_render(uid, sid, season, ep):
    from video.xianxia import dub
    data = _body()
    key = (uid, sid, season, ep)
    volume = max(0.0, min(1.0, float(data.get("muse_volume") or 0)))
    watermark = str(data.get("watermark") or "").strip()

    def worker(job):
        # Đọc trước những câu chưa có tiếng (câu đã đọc thì giữ nguyên), để phim không có câu câm.
        if not dub.get_cast(uid, sid):
            dub.auto_cast(uid, sid, _voice_catalog())
        dub.synth(key, _voice_catalog(), _speak, _probe, progress=lambda pct, text: _step(job, round(pct * 0.2), text),
                  cancelled=lambda: bool(job.get("cancel")))
        out = dub.render(key, muse_volume=volume, burn=bool(data.get("burn")),
                         watermark=watermark,
                         progress=lambda pct, text: _step(job, 20 + round(pct * 0.8), text),
                         cancelled=lambda: bool(job.get("cancel")))
        job["result"] = {"video": out.name}
        _tu_cat(*key)
    return _bat_dau("mx_render", worker)


# ------------------------------------------------------------------------------
# Đăng YouTube
# ------------------------------------------------------------------------------

# Lần đăng gần nhất của từng tập. Việc đăng không mang tiền tố mx_: nó không đụng tới Muse, nên đăng tập cũ được
# trong lúc tập mới đang quay.
_DANG = {}
_GIU_GIO = {}
CACH_DANG = [{"id": "private", "label": "Riêng tư, tự bấm công khai sau"},
             {"id": "schedule", "label": "Hẹn 18h, mỗi ngày một tập"},
             {"id": "unlisted", "label": "Không công khai, ai có link thì xem được"},
             {"id": "public", "label": "Công khai ngay"}]


def _ten_kenh(cid, hoi_google=False):
    """Tên kênh. Hỏi thẳng Google khi sắp đăng, vì tên lưu trên đĩa là tên của ngày nối kênh."""
    import youtube_upload as yu
    ten = next((k["name"] for k in yu.danh_sach_kenh() if k["id"] == cid), "")
    if hoi_google:
        try:
            got = yu._dich_vu(cid).channels().list(part="snippet", mine=True).execute()
            ten = got["items"][0]["snippet"]["title"] or ten
        except Exception as exc:  # noqa: BLE001 - mất mạng thì dùng tên đã lưu
            logging.warning("Không hỏi được tên kênh %s: %s", cid, exc)
    return ten


def _goi_dang(uid, sid, season, ep, hoi_google=False, danh_sach=""):
    from video.xianxia import publish
    series = store.get_series(uid, sid) or {}
    episode = store.get_episode(uid, sid, season, ep) or {}
    cid = str(series.get("youtube_channel") or "")
    ds = {"id": danh_sach, "channel": cid} if danh_sach else (series.get("youtube_playlist") or {})
    truoc = next(((store.get_episode(uid, sid, s, e) or {}).get("youtube") or {}
                  for s, e in store.episodes_before(uid, sid, season, ep)), {})
    return publish.package(series, episode, season, ep, channel=_ten_kenh(cid, hoi_google) if cid else "",
                           playlist_url=f"https://www.youtube.com/playlist?list={ds['id']}" if ds.get("id") and ds.get("channel") == cid else "",
                           previous_url=truoc.get("url") if truoc.get("channel") == cid else "")


def _anh_bia(uid, sid, season, ep):
    """Ảnh bìa mới nhất trong thư mục final: làm lại ảnh dưới tên khác thì bản mới được dùng, không phải bản cũ."""
    final = store.episode_dir(uid, sid, season, ep) / "final"
    return max(list(final.glob("thumbnail*.jpg")) + list(final.glob("thumbnail*.png")),
               key=lambda f: f.stat().st_mtime, default=None)


def _publish_view(uid, sid, season, ep):
    import longform_studio as lf
    import youtube_upload as yu
    series = store.get_series(uid, sid) or {}
    episode = store.get_episode(uid, sid, season, ep) or {}
    job = lf.get_job(_DANG.get((uid, sid, season, ep)) or "") or {}
    bia = _anh_bia(uid, sid, season, ep)
    return {"ok": True, "channels": [{"id": k["id"], "name": k["name"]} for k in yu.danh_sach_kenh()],
            "channel": str(series.get("youtube_channel") or ""), "modes": CACH_DANG,
            "package": _goi_dang(uid, sid, season, ep), "video": episode.get("dub_video") or "",
            "thumbnail": bia.name if bia else "", "posted": episode.get("youtube"),
            "job": {k: job.get(k) for k in ("status", "progress", "step", "error")} if job else None}


def _gio_hen(uid, sid):
    """Mốc 18h kế tiếp chưa tập nào giữ, tính cả những tập đang tải lên dở."""
    from datetime import datetime, timezone
    from video.xianxia import publish
    da_hen = [((store.get_episode(uid, sid, s, e) or {}).get("youtube") or {}).get("publish_at")
              for s, e in store.episodes_before(uid, sid, 99, 1)]
    da_hen += [gio for (u, x, _, _), gio in _GIU_GIO.items() if (u, x) == (uid, sid)]
    return publish.next_slot(datetime.now(timezone.utc), da_hen)


def _danh_sach_phat(uid, sid, cid, ghi_chu):
    """Mã danh sách phát của series trên kênh này; chưa có thì lập. Hỏng thì chỉ ghi chú, không chặn lần đăng."""
    import youtube_upload as yu
    series = store.get_series(uid, sid) or {}
    ds = series.get("youtube_playlist") or {}
    if ds.get("id") and ds.get("channel") == cid:
        return ds["id"]
    try:
        got = yu._dich_vu(cid).playlists().insert(part="snippet,status", body={
            "snippet": {"title": series.get("title") or "Phim bộ",
                        "description": f"{series.get('title') or ''}: xem lần lượt từ Tập 1."},
            "status": {"privacyStatus": "public"}}).execute()
        store.save_series(uid, sid, {**(store.get_series(uid, sid) or {}), "youtube_playlist": {"id": got["id"], "channel": cid}})
        return got["id"]
    except Exception as exc:  # noqa: BLE001
        ghi_chu.append(f"Chưa lập được danh sách phát: {yu._doc_loi(exc)}")
        return ""


def _xep_vao_danh_sach(cid, ds, video_id, ghi_chu):
    import youtube_upload as yu
    try:
        yu._dich_vu(cid).playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": ds, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
    except Exception as exc:  # noqa: BLE001
        ghi_chu.append(f"Chưa xếp được vào danh sách phát: {yu._doc_loi(exc)}")


@bp.route(EP + "/publish", methods=["GET", "POST"])
def publish_episode(uid, sid, season, ep):
    """
    Đăng bản lồng tiếng của một tập lên YouTube, tiêu đề, mô tả và thẻ theo khuôn của xưởng (video/xianxia/publish.py).

    Kênh đăng ghi vào series và KHÔNG bao giờ lấy kênh đang chọn của tool: kênh đang chọn thường là kênh lịch sử.
    """
    import longform_studio as lf
    import youtube_upload as yu
    from video.xianxia import publish
    key = (uid, sid, season, ep)
    if request.method == "GET":
        return jsonify(_publish_view(*key))
    data = _body()
    series = store.get_series(uid, sid) or {}
    episode = store.get_episode(*key) or {}
    cid = str(data.get("channel") or series.get("youtube_channel") or "")
    if cid not in {k["id"] for k in yu.danh_sach_kenh()}:
        return _loi("Chọn kênh YouTube cho series này trước. Kênh chưa nối thì nối ở mục Kênh của mình.")
    video = store.episode_dir(*key) / "final" / os.path.basename(str(episode.get("dub_video") or "-"))
    if not video.is_file():
        return _loi("Tập này chưa có bản lồng tiếng. Dựng phim trước rồi mới đăng.")
    if episode.get("youtube") and not data.get("again"):
        return _loi("Tập này đã đăng rồi. Đăng lần nữa sẽ ra hai video trùng nhau trên kênh.")
    dang = lf.get_job(_DANG.get(key) or "") or {}
    if dang.get("status") == "running":
        return _loi("Tập này đang được tải lên, đợi xong đã.", 409)
    if series.get("youtube_channel") != cid:
        store.save_series(uid, sid, {**series, "youtube_channel": cid})
    che_do = str(data.get("mode") or "private")
    gio = _gio_hen(uid, sid) if che_do == "schedule" else ""
    if data.get("publish_at"):
        # Giờ hẹn tự chọn (UTC, dạng YouTube nhận), ví dụ đăng liền mấy tập cách nhau một tiếng.
        from datetime import datetime, timezone
        gio = datetime.strptime(str(data["publish_at"]), publish.STAMP).strftime(publish.STAMP)
        if datetime.strptime(gio, publish.STAMP).replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
            return _loi("Giờ hẹn đã trôi qua.")
        che_do = "schedule"
    bia = _anh_bia(*key)

    if gio:
        _GIU_GIO[key] = gio

    def worker(job):
        truoc = []
        ds = _danh_sach_phat(uid, sid, cid, truoc)
        goi = _goi_dang(*key, hoi_google=True, danh_sach=ds)
        res = yu.dang_video(str(video), title=goi["title"], description=goi["description"], tags=goi["tags"],
                            che_do=che_do if che_do != "schedule" else yu.RIENG_TU, dang_luc=gio,
                            thumb_path=str(bia) if bia else "", channel_id=cid, danh_muc=publish.CATEGORY,
                            bao=lambda pct, text: lf.update_job(job, pct, text))
        if not res.get("success"):
            _GIU_GIO.pop(key, None)
            raise RuntimeError(res.get("error") or "Không đăng được lên YouTube.")
        res["ghi_chu"] = truoc + res["ghi_chu"]
        if ds:
            _xep_vao_danh_sach(cid, ds, res["video_id"], res["ghi_chu"])
        store.save_episode(*key, {**(store.get_episode(*key) or {}), "youtube": {
            "video_id": res["video_id"], "url": res["url"], "studio_url": res["studio_url"], "channel": cid,
            "privacy": res["privacy"], "publish_at": gio, "title": goi["title"], "file": video.name,
            "thumbnail": bool(res.get("thumbnail")), "notes": res["ghi_chu"], "at": store.now()}})
        _GIU_GIO.pop(key, None)
        job["result"] = res
        _tu_cat(*key)
    with _KHOA:
        _DANG[key] = lf.start_job("youtube", worker)
    return jsonify(_publish_view(*key))


# ------------------------------------------------------------------------------
# Cất lên Google Drive
# ------------------------------------------------------------------------------

_CAT = {}
_VAO_DRIVE = {"job": ""}
# Thư mục trên Drive đi đúng như Kho phim trong tool: Kho phim / Donghua / <series> / Tập NN - <tên tập> / <loại file>.
GOC_DRIVE = "Kho phim"
LOAI_DRIVE = "Donghua"
NGAN = {"phim": "Phim", "bia": "Ảnh bìa", "short": "Short", "clip": "Clip gốc", "du_lieu": "Dữ liệu", "cu": "Bản cũ"}


def _giay_drive(uid="", sid=""):
    """File giấy phép Google dùng để đăng nhập Drive: giấy phép riêng của Drive nếu có, không thì dự án của kênh mà series đăng lên."""
    import luu_drive as ld
    import youtube_upload as yu
    if os.path.isfile(ld.FILE_GIAY):
        return ld.FILE_GIAY
    cid = str((store.get_series(uid, sid) or {}).get("youtube_channel") or "") if uid else ""
    ma = yu._doc_json(yu.duong_token(cid)).get("client_id") if cid else ""
    return next((g["duong"] for g in yu.danh_sach_giay_phep() if ma and yu._ma_khach(g["duong"]) == ma), yu.duong_giay_phep())


def _tep_cat(uid, sid, season, ep):
    """
    Những file của một tập được cất: bản phim đã đăng hoặc bản lồng tiếng mới nhất kèm phụ đề, ảnh bìa, short, clip Muse
    gốc và hai file dữ liệu. Mọi phim khác trong final (bản dựng cũ, bản chưa lồng tiếng, phim thử) và các lượt quay bị
    thay (shots/takes) vào ngăn "Bản cũ": Drive rộng, và nhờ vậy dọn máy lấy lại được hết chỗ mà không mất gì.
    Trả về danh sách (file, ngăn trên Drive): mỗi loại file một ngăn riêng trong thư mục của tập (xem NGAN).
    """
    base = store.episode_dir(uid, sid, season, ep)
    episode = store.get_episode(uid, sid, season, ep) or {}
    final = base / "final"
    ten = {os.path.basename(str(x)) for x in (episode.get("dub_video"), (episode.get("youtube") or {}).get("file"),
                                              (episode.get("youtube_short") or {}).get("file")) if x}
    short = sorted(final.glob("short_tap*.mp4"))
    chon = [(final / t, "phim") for t in sorted(ten) if final / t not in short]
    chon += [((final / t).with_suffix(".srt"), "phim") for t in sorted(ten) if final / t not in short]
    chon += [(f, "bia") for f in sorted(final.glob("thumbnail*.jpg")) + sorted(final.glob("thumbnail*.png"))]
    chon += [(f, "bia") for f in sorted(final.glob("short_tap*_bia*.jpg")) if not f.stem.endswith("_xem")]
    chon += [(f, "short") for f in short]
    chon += [(f, "clip") for f in sorted((base / "shots").glob("shot_*.mp4"))]
    chon += [(base / "episode.json", "du_lieu"), (base / "shots.json", "du_lieu")]
    chon += [(f, "cu") for f in sorted(final.glob("*.mp4")) + sorted((base / "shots" / "takes").rglob("*.mp4"))]
    ra, da = [], set()
    for f, ngan in chon:
        if f.is_file() and f not in da:
            da.add(f)
            ra.append((f, NGAN[ngan]))
    return ra


def _drive_view(uid, sid, season, ep):
    import longform_studio as lf
    tep = _tep_cat(uid, sid, season, ep)
    job = lf.get_job(_CAT.get((uid, sid, season, ep)) or "") or {}
    return {"ok": True, "files": len(tep), "bytes": sum(f.stat().st_size for f, _ in tep),
            # Phần nặng còn trong máy mà đã thuộc diện cất: đây là chỗ nút dọn máy lấy lại được.
            "heavy": sum(f.stat().st_size for f, _ in tep if f.suffix.lower() in NANG),
            "saved": (store.get_episode(uid, sid, season, ep) or {}).get("drive"),
            "job": {k: job.get(k) for k in ("status", "progress", "step", "error")} if job else None}


@bp.route("/drive", methods=["GET", "POST"])
def drive_account():
    """
    Tình hình Google Drive: tài khoản đang dùng và mọi tài khoản đã đăng nhập. POST mở trình duyệt trên máy chạy tool để
    chủ máy đăng nhập THÊM một tài khoản; POST action=use/remove đổi tài khoản nhận phim hoặc bỏ một tài khoản.
    """
    import longform_studio as lf
    import luu_drive as ld
    if request.method == "POST":
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return _loi("Chỉ đăng nhập Google Drive được trên máy đang chạy tool.", 403)
        # Bấm lại thì mở một trang đăng nhập mới: trang trước có thể đã chết vì rớt mạng. Lượt nào xong sau thì thắng.
        data = _body()
        if data.get("action") in ("use", "remove"):
            # Chọn tài khoản nhận phim, hoặc bỏ một tài khoản khỏi tool (phim trên Drive của nó vẫn còn).
            lam = ld.chon if data["action"] == "use" else ld.bo
            if not lam(str(data.get("email") or "")):
                return _loi("Không có tài khoản Drive này.")
            return jsonify({"ok": True, **ld.tinh_hinh(), "project": ld.du_an_cua_giay(_giay_drive()), "job": None})
        giay = _giay_drive(str(data.get("uid") or ""), str(data.get("sid") or ""))
        if not giay:
            return _loi("Chưa có file giấy phép Google trong thư mục tool.")

        def worker(job):
            lf.update_job(job, 10, "Đang chờ bạn đăng nhập Google trên trình duyệt...")
            try:
                job["result"] = ld.dang_nhap(giay)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(ld.doc_loi(exc)) from exc
        with _KHOA:
            _VAO_DRIVE["job"] = lf.start_job("drive", worker)
    job = lf.get_job(_VAO_DRIVE["job"]) or {}
    return jsonify({"ok": True, **ld.tinh_hinh(), "project": ld.du_an_cua_giay(_giay_drive()),
                    "job": {k: job.get(k) for k in ("status", "step", "error")} if job else None})


def _cat_len_drive(uid, sid, season, ep):
    """Bắt đầu cất một tập lên Drive đang dùng. Trả về câu báo lỗi, hoặc chuỗi rỗng khi việc đã chạy."""
    import longform_studio as lf
    import luu_drive as ld
    key = (uid, sid, season, ep)
    noi = ld.tinh_hinh()
    if not noi.get("connected"):
        return "Chưa đăng nhập Google Drive."
    if (lf.get_job(_CAT.get(key) or "") or {}).get("status") == "running":
        return "Tập này đang được cất lên Drive, đợi xong đã."
    tep = _tep_cat(*key)
    if not tep:
        return "Tập này chưa có file nào để cất."
    series = store.get_series(uid, sid) or {}
    tap_nay = store.get_episode(*key) or {}
    cu, tua = tap_nay.get("drive") or {}, " ".join(str(tap_nay.get("title") or "").replace("/", " ").split())

    def worker(job):
        try:
            dv = ld.dich_vu(noi.get("email") or "")
            goc = ld.thu_muc(dv, series.get("title") or sid, ld.thu_muc(dv, LOAI_DRIVE, ld.thu_muc(dv, GOC_DRIVE)))
            ten = (f"Tập {ep:02d}" if season <= 1 else f"Phần {season} Tập {ep:02d}") + (f" - {tua}" if tua else "")
            # Tập đã cất rồi thì dùng lại đúng thư mục cũ (đổi tên nếu tên tập đã đổi), không lập thư mục thứ hai.
            tap = ld.dung_lai(dv, cu.get("folder") if (cu.get("account") or "") == (noi.get("email") or "") else "", ten) or ld.thu_muc(dv, ten, goc)
            con, tong, xong, moi, ma = {}, sum(f.stat().st_size for f, _ in tep) or 1, 0, 0, dict(cu.get("ids") or {})
            for i, (f, nhanh) in enumerate(tep, 1):
                cha = con[nhanh] if nhanh in con else con.setdefault(nhanh, ld.thu_muc(dv, nhanh, tap))
                co = f.stat().st_size
                got = ld.tai_len(dv, str(f), cha, bao=lambda p, xong=xong, co=co, i=i, f=f: lf.update_job(
                    job, int((xong + co * p) * 100 / tong), f"Đang cất {i}/{len(tep)}: {f.name}"))
                xong += co
                moi += 0 if got["da_co"] else 1
                if nhanh != NGAN["clip"]:
                    ma[f.name] = got["id"]  # để Kho phim mở được phim, short trên Drive khi bản trong máy đã dọn
                lf.update_job(job, int(xong * 100 / tong), f"Đã cất {i}/{len(tep)}: {f.name}")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(ld.doc_loi(exc)) from exc
        # Tập đã dọn máy rồi thì số file và dung lượng trên Drive là số của lần cất đầy đủ, không phải của mấy file còn lại.
        day = cu if cu.get("freed") else {}
        store.save_episode(*key, {**(store.get_episode(*key) or {}), "drive": {
            **({"freed": cu["freed"]} if cu.get("freed") else {}),
            "folder": tap, "url": ld.link_thu_muc(tap), "files": max(int(day.get("files") or 0), len(tep)),
            "bytes": max(int(day.get("bytes") or 0), xong), "at": store.now(), "account": noi.get("email") or "", "ids": ma}})
        job["result"] = {"url": ld.link_thu_muc(tap), "files": len(tep), "new": moi}
    with _KHOA:
        _CAT[key] = lf.start_job("drive", worker)
    return ""


def _tu_cat(uid, sid, season, ep):
    """
    Drive là nơi giữ phim: tập vừa dựng xong hoặc vừa đăng xong thì tự cất, không chờ ai bấm. Chưa nối Drive hay mất
    mạng thì bỏ qua trong im lặng: việc dựng, việc đăng không được hỏng vì bước cất.
    """
    try:
        loi = _cat_len_drive(uid, sid, season, ep)
        if loi:
            logging.info("Chưa tự cất %s/%s tập %s: %s", uid, sid, ep, loi)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Tự cất lên Drive hỏng: %s", exc)


NANG = (".mp4", ".mov", ".wav", ".mp3")


@bp.route(EP + "/drive/free", methods=["POST"])
def drive_free(uid, sid, season, ep):
    """
    Dọn máy sau khi đã cất: xoá bản trong máy của phim, short và clip Muse gốc. CHỈ xoá file nào vừa đối chiếu thấy đúng
    nội dung đó đang nằm trên Drive; file nào chưa có trên Drive thì để nguyên. Ảnh bìa, phụ đề và file dữ liệu luôn được
    giữ lại (nhẹ), nên Kho phim vẫn hiện đủ tập. Khi mọi file nặng đã lên Drive đủ thì dọn luôn clip tạm trong dub/shots.
    """
    import longform_studio as lf
    import luu_drive as ld
    key = (uid, sid, season, ep)
    episode = store.get_episode(*key) or {}
    cat = episode.get("drive") or {}
    noi = ld.tinh_hinh()
    if not cat.get("folder"):
        return _loi("Tập này chưa cất lên Drive nên chưa dọn máy được.")
    if not noi.get("connected") or (cat.get("account") or "") != (noi.get("email") or ""):
        return _loi(f"Tập này nằm ở Drive của {cat.get('account') or 'tài khoản khác'}. Chọn đúng tài khoản đó rồi mới dọn.")
    if (lf.get_job(_CAT.get(key) or "") or {}).get("status") == "running":
        return _loi("Tập này đang được cất lên Drive, đợi xong đã.", 409)
    if _viec_dang_chay():
        return _loi("Xưởng đang làm phim. Đợi việc đó xong rồi dọn máy.", 409)
    try:
        dv = ld.dich_vu(noi.get("email") or "")
        con = {f["name"]: f["id"] for f in dv.files().list(
            q=f"'{cat['folder']}' in parents and trashed = false and mimeType = '{ld.THU_MUC}'", fields="files(id,name)").execute().get("files") or []}
        xoa, giu = [], 0
        for f, ngan in _tep_cat(*key):
            if f.suffix.lower() not in NANG:
                continue
            if ngan in con and ld.co_tren_drive(dv, str(f), con[ngan]):
                xoa.append(f)
            else:
                giu += 1
    except Exception as exc:  # noqa: BLE001
        return _loi(ld.doc_loi(exc))
    # Clip đã cắt sẵn để dựng (dub/shots) là đồ tạm, làm lại được từ clip gốc và lời đọc: dọn luôn, không cần cất.
    xoa += [f for f in (store.episode_dir(*key) / "dub" / "shots").glob("*.mp4")] if not giu else []
    nhe = sum(f.stat().st_size for f in xoa)
    for f in xoa:
        f.unlink()
    truoc = cat.get("freed") or {}
    store.save_episode(*key, {**(store.get_episode(*key) or {}), "drive": {**cat, "freed": {
        "files": int(truoc.get("files") or 0) + len(xoa), "bytes": int(truoc.get("bytes") or 0) + nhe, "at": store.now()}}})
    return jsonify({**_drive_view(*key), "freed": {"files": len(xoa), "bytes": nhe, "kept": giu}})


@bp.route(EP + "/drive", methods=["GET", "POST"])
def drive_episode(uid, sid, season, ep):
    """
    Cất một tập lên Google Drive: <GOC_DRIVE>/<tên series>/Tập NN. Chỉ tải lên và đối chiếu, KHÔNG xoá gì trong máy.
    Cất lại thì chỉ những file mới hoặc đã đổi mới được tải.
    """
    if request.method == "POST":
        loi = _cat_len_drive(uid, sid, season, ep)
        if loi:
            return _loi(loi, 409 if "đang được cất" in loi else 400)
    return jsonify(_drive_view(uid, sid, season, ep))


# ------------------------------------------------------------------------------
# Kho phim: mọi tập đã dựng của một series, kèm ảnh bìa, short, nơi đã đăng và nơi đã cất
# ------------------------------------------------------------------------------

_LUOT_XEM = {}
LUOT_XEM_SONG_GIAY = 300


def _luot_xem(cid, ma):
    """Lượt xem và trạng thái công khai của các video, hỏi YouTube một lần cho cả mẻ rồi nhớ năm phút."""
    import youtube_upload as yu
    ma = sorted(m for m in ma if m)
    if not cid or not ma:
        return {}
    nho = _LUOT_XEM.get((cid, tuple(ma)))
    if nho and time.time() - nho[0] < LUOT_XEM_SONG_GIAY:
        return nho[1]
    ra = {}
    try:
        for i in range(0, len(ma), 50):
            got = yu._dich_vu(cid).videos().list(part="status,statistics", id=",".join(ma[i:i + 50])).execute()
            for v in got.get("items") or []:
                ra[v["id"]] = {"views": int((v.get("statistics") or {}).get("viewCount") or 0),
                               "privacy": (v.get("status") or {}).get("privacyStatus") or "",
                               "publish_at": (v.get("status") or {}).get("publishAt") or ""}
    except Exception as exc:  # noqa: BLE001 - mất mạng hay hết hạn mức thì kho vẫn mở được, chỉ thiếu lượt xem
        logging.warning("Không hỏi được lượt xem: %s", exc)
        return (nho or (0, {}))[1]
    _LUOT_XEM[(cid, tuple(ma))] = (time.time(), ra)
    return ra


def _vault_view(uid, sid):
    import luu_drive as ld
    series = store.get_series(uid, sid) or {}
    cid = str(series.get("youtube_channel") or "")
    tap = []
    for season, ep in sorted(store.episodes_before(uid, sid, 99, 1)):
        e = store.get_episode(uid, sid, season, ep) or {}
        base = store.episode_dir(uid, sid, season, ep)
        final = base / "final"
        phim = os.path.basename(str((e.get("youtube") or {}).get("file") or e.get("dub_video") or e.get("final_video") or ""))
        ma = (e.get("drive") or {}).get("ids") or {}
        tai_may = bool(phim) and (final / phim).is_file()
        if not tai_may and not e.get("drive"):
            continue
        bia = _anh_bia(uid, sid, season, ep)
        short = max(final.glob("short_tap*.mp4"), key=lambda f: f.stat().st_mtime, default=None)
        short_xa = next((ma[t] for t in sorted(ma, reverse=True) if t.startswith("short_tap") and t.endswith(".mp4")), "")
        bia_short = max((f for f in final.glob("short_tap*_bia*.jpg") if not f.stem.endswith("_xem")),
                        key=lambda f: f.stat().st_mtime, default=None)
        tap.append({"season": season, "ep": ep, "title": e.get("title") or "", "film": phim if tai_may else "",
                    "film_bytes": (final / phim).stat().st_size if tai_may else 0,
                    "film_drive": ld.link_file(ma[phim]) if ma.get(phim) else "",
                    "short_drive": ld.link_file(short_xa) if short_xa else "",
                    "thumbnail": bia.name if bia else "", "short": short.name if short else "",
                    "short_thumbnail": bia_short.name if bia_short else "",
                    "disk": sum(f.stat().st_size for f in base.rglob("*") if f.is_file()),
                    "youtube": e.get("youtube"), "youtube_short": e.get("youtube_short"), "facebook": e.get("facebook"),
                    "drive": _drive_view(uid, sid, season, ep)})
    so = _luot_xem(cid, [(t[k] or {}).get("video_id") for t in tap for k in ("youtube", "youtube_short")])
    for t in tap:
        for k in ("youtube", "youtube_short"):
            if t[k]:
                t[k] = {**t[k], **so.get(t[k].get("video_id"), {})}
    return {"ok": True, "title": series.get("title") or sid, "episodes": tap,
            "drive": {**ld.tinh_hinh(), "project": ld.du_an_cua_giay(_giay_drive(uid, sid))}}


def _vault_series(uid, sid):
    """Một dòng của kho: series có bao nhiêu tập đã dựng, bìa là ảnh bìa tập mới nhất. Không hỏi YouTube ở tầng này."""
    series = store.get_series(uid, sid) or {}
    tap, bia, nang, cat = 0, None, 0, 0
    for season, ep in sorted(store.episodes_before(uid, sid, 99, 1)):
        e = store.get_episode(uid, sid, season, ep) or {}
        base = store.episode_dir(uid, sid, season, ep)
        phim = os.path.basename(str((e.get("youtube") or {}).get("file") or e.get("dub_video") or e.get("final_video") or ""))
        if not (phim and (base / "final" / phim).is_file()) and not e.get("drive"):
            continue
        tap += 1
        cat += 1 if e.get("drive") else 0
        nang += sum(f.stat().st_size for f in base.rglob("*") if f.is_file())
        anh = _anh_bia(uid, sid, season, ep)
        if anh:
            bia = {"season": season, "ep": ep, "name": anh.name}
    return {"uid": uid, "sid": sid, "title": series.get("title") or sid, "logline": series.get("logline") or "",
            "episodes": tap, "saved": cat, "disk": nang, "cover": bia}


@bp.route("/vault", methods=["GET"])
def vault_home():
    """
    Tầng trên cùng của Kho phim: Kho phim → loại phim → series → tập. Hôm nay chỉ có một loại (Donghua, phim của
    xưởng Tiên hiệp); loại mới thêm vào danh sách này, trang không phải đổi.
    """
    ds = [_vault_series(u["universe_id"], s["series_id"]) for u in store.list_universes() for s in u.get("series") or []]
    ds = sorted((d for d in ds if d["episodes"]), key=lambda d: -d["episodes"])
    return jsonify({"ok": True, "categories": [{"id": "donghua", "name": "Donghua", "hint": "Phim tu tiên 3D của xưởng Tiên hiệp",
                                                "icon": "dragon", "series": ds}]})


@bp.route("/series/<uid>/<sid>/vault", methods=["GET"])
def vault(uid, sid):
    if not store.get_series(uid, sid):
        return _loi("Không có series này.", 404)
    return jsonify(_vault_view(uid, sid))


@bp.route(EP + "/video/<name>", methods=["GET"])
def episode_video(uid, sid, season, ep, name):
    name = os.path.basename(name)
    base = store.episode_dir(uid, sid, season, ep)
    folder = base / "shots" if name.startswith("shot_") else base / "final"
    return send_from_directory(folder, name)
