"""Muse còn bao nhiêu lượt (15/11/2026).

Ý lấy từ muse2api (www222fff/muse2api, MIT): hạn mức nằm ở nút Settings góc dưới (data-testid hatch-dock-more) ->
mục "Settings" -> phần Usage. Không chép code. Đo thật 15/11/2026, chữ trong hộp:
    Usage / Free plan / Weekly limit resets on Nov 21 / 57% used / Additional tokens / Never expires /
    28% used (4.3B tokens left) / Upgrade ...

Đọc bằng chính Chrome hồ sơ Muse của tool. Mở Chrome đó là tool TẮT Chrome Muse đang mở (cleanup_stale_profile_lock),
nên Chrome Muse đang mở (đang quay clip, hay cửa sổ đăng nhập) thì từ chối, không đọc. Số cất ở downloads/muse_han_muc.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from flask import Blueprint, jsonify, request

GOC = Path(__file__).resolve().parent
TEP = GOC / "downloads" / "muse_han_muc.json"
_MUSE_DIR = GOC / "muse-controller"
_KHOA = threading.Lock()
bp = Blueprint("muse_han_muc", __name__)

_THANG = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DA_DUNG = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*used", re.I)
_LAM_MOI = re.compile(r"(?:Weekly\s+)?limit\s+resets?\s+on\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})", re.I)
_CON = re.compile(r"\(\s*([\d.,]+)\s*([KMBT]?)\s*tokens?\s+left\s*\)", re.I)
_NHAN = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, "": 1}


def phan_tich(chu: str) -> Dict:
    """Chữ của hộp Settings -> số. `thay` chỉ True khi đọc được % đã dùng của tuần."""
    dong = [d.strip() for d in (chu or "").splitlines() if d.strip()]
    dau = next((i + 1 for i, d in enumerate(dong) if d.lower() == "usage"), 0)
    dong = dong[dau:]
    cuoi = next((i for i, d in enumerate(dong) if d.lower() in ("language", "upgrade")), len(dong))
    dong = dong[:cuoi]
    kq: Dict = {"goi": None, "tuan_da_dung": None, "lam_moi": None, "them_da_dung": None, "them_con": None}
    kq["goi"] = next((d for d in dong if re.fullmatch(r".{1,40}\splan", d, re.I)), None)
    m = _LAM_MOI.search("\n".join(dong))
    if m and m.group(1).lower()[:3] in _THANG:
        kq["lam_moi"] = f"{int(m.group(2)):02d}/{_THANG[m.group(1).lower()[:3]]:02d}"
    them = next((i for i, d in enumerate(dong) if re.match(r"(additional|extra)\s+tokens", d, re.I)), len(dong))
    for d in dong[:them]:
        if (m := _DA_DUNG.search(d)):
            kq["tuan_da_dung"] = float(m.group(1))
            break
    for d in dong[them:]:
        if (m := _DA_DUNG.search(d)):
            kq["them_da_dung"] = float(m.group(1))
            if (c := _CON.search(d)):
                kq["them_con"] = int(float(c.group(1).replace(",", "")) * _NHAN[c.group(2).upper()])
            break
    kq["thay"] = kq["tuan_da_dung"] is not None
    return kq


def _ho_so():
    if str(_MUSE_DIR) not in sys.path:
        sys.path.insert(0, str(_MUSE_DIR))
    from muse import MuseAIStudio
    s = MuseAIStudio()
    cs = [s.settings.browser_profile, getattr(s.settings, "legacy_profile", None)]
    return s, [Path(p) for p in cs if p and str(p) not in ("", ".")]


def _dang_mo(ho_so) -> bool:
    """Chrome Muse đang mở trên hồ sơ này? (khoá SingletonLock trỏ tới một tiến trình còn sống)"""
    for p in ho_so:
        k = p / "SingletonLock"
        if not (k.is_symlink() or k.exists()):
            continue
        try:
            pid = int(os.readlink(str(k)).rsplit("-", 1)[-1])
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            continue
    return False


_DOC_HOP = """(() => {
  const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')].find(e => e.getClientRects().length);
  return d ? (d.innerText || '') : '';
})()"""


async def _doc_tren_muse(s) -> str:
    try:
        p = await s.launch_browser()
        await p.goto(s.settings.muse_url, wait_until="domcontentloaded", timeout=45000)
        return await doc_tren_trang(p)
    finally:
        await s.close()


async def doc_tren_trang(p) -> str:
    """Mở Settings trên một trang Muse ĐANG MỞ, đọc chữ của hộp rồi đóng hộp."""
    await p.locator('[data-testid="hatch-dock-more"]').first.wait_for(state="visible", timeout=40000)
    await p.keyboard.press("Escape")
    try:
        await p.locator('[data-testid="hatch-dock-more"]').first.click(timeout=10000)
        await p.locator("[role=menuitem]").filter(has_text=re.compile(r"^\s*Settings", re.I)).first.click(timeout=10000)
        chu, het = "", time.time() + 15
        while time.time() < het:
            chu = await p.evaluate(_DOC_HOP) or ""
            if _DA_DUNG.search(chu):
                break
            await p.wait_for_timeout(300)
        return chu
    finally:
        await p.keyboard.press("Escape")
        await p.keyboard.press("Escape")


def _cat(chu: str) -> Optional[Dict]:
    kq = phan_tich(chu)
    if not kq["thay"]:
        return None
    kq["luc"] = int(time.time())
    TEP.parent.mkdir(parents=True, exist_ok=True)
    TEP.write_text(json.dumps(kq, ensure_ascii=False, indent=1), encoding="utf-8")
    return kq


async def cap_nhat_sau_khi_quay(p) -> Optional[Dict]:
    """Gọi trước khi đóng Chrome Muse của một lượt quay: đọc hạn mức trên cửa sổ đang mở, không bật thêm Chrome.
    Hỏng thì thôi (không được làm hỏng lượt quay)."""
    try:
        return _cat(await asyncio.wait_for(doc_tren_trang(p), 30))
    except Exception:  # noqa: BLE001
        return None


def doc_luu() -> Optional[Dict]:
    try:
        return json.loads(TEP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def doc_moi() -> Dict:
    if not _KHOA.acquire(blocking=False):
        raise RuntimeError("Đang đọc hạn mức Muse rồi, chờ chút.")
    try:
        s, ho_so = _ho_so()
        if _dang_mo(ho_so):
            raise RuntimeError("Chrome của Muse đang mở (đang quay clip hoặc cửa sổ đăng nhập). Chờ xong hoặc đóng cửa sổ đó rồi bấm lại.")
        try:
            chu = asyncio.run(_doc_tren_muse(s))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Không mở được phần Settings của Muse ({str(e)[:120]}). Có thể Muse đổi giao diện hoặc chưa đăng nhập.")
        kq = _cat(chu)
        if not kq:
            raise RuntimeError("Mở được Settings của Muse nhưng không thấy phần Usage. Có thể Muse đổi giao diện hoặc chưa đăng nhập.")
        return kq
    finally:
        _KHOA.release()


@bp.get("/api/muse/han_muc")
def api_doc():
    return jsonify({"ok": True, "han_muc": doc_luu()})


@bp.post("/api/muse/han_muc")
def api_doc_moi():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "Chỉ máy chạy tool xem được hạn mức Muse."}), 403
    try:
        return jsonify({"ok": True, "han_muc": doc_moi()})
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
