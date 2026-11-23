"""
quy_trinh.py
────────────
Trang "Quy trình làm video" (thêm 12/11/2026). Người dùng: "build 1 cái dạng workflow làm video cho playah từ đoạn lấy clip ->
scan clip -> .... tới cuối, cho nó lên playah, tao muốn thấy workflow đó".

Trang chỉ ĐỌC: vẽ các chặng của quy trình (static/quy_trinh.js) kèm số liệu thật của từng chặng. File này gom số liệu từ đĩa,
không ghi gì, không gọi AI hay máy làm clip. Mỗi chặng trên trang có nút mở đúng mục làm việc đó (Soi video mẫu, Kho ảnh, UGC...).
"""
from __future__ import annotations

import glob
import json
import os
import time
from collections import Counter
from typing import Dict

from flask import Blueprint, jsonify

bp = Blueprint("quy_trinh", __name__)
GOC = os.path.dirname(os.path.abspath(__file__))
DL = os.path.join(GOC, "downloads")


def _doc(duong: str, mac=None):
    try:
        with open(duong, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return mac


def so_lieu() -> Dict:
    # 1-2. Mẫu đã soi
    mau = [_doc(f, {}) or {} for f in glob.glob(os.path.join(DL, "soi_video", "sv_*", "mau.json"))]
    soi = {"tong": len(mau), "rea": sum(1 for m in mau if m.get("cach") == "rea"),
           "canh": sum(len(m.get("canh") or m.get("cac_canh") or []) for m in mau)}

    # 3. Kho ảnh
    kho = _doc(os.path.join(DL, "kho_anh", "kho.json"), {}) or {}
    anh = kho.get("anh") or []
    kho_tt = {"san_pham": len(kho.get("sp") or {}), "anh": len(anh),
              "chuan": sum(1 for a in anh if "bộ ảnh chuẩn" in (a.get("tags") or []))}

    # 4-6. Video và clip
    video, clip, ai_soan, co_phim = 0, Counter(), 0, 0
    may = Counter()
    for f in glob.glob(os.path.join(DL, "review_sp", "rv_*", "du_an.json")):
        d = _doc(f, {}) or {}
        video += 1
        if d.get("phim"):
            co_phim += 1
        canh = d.get("canh") or []
        if any(c.get("ai_vi_sao") for c in canh):
            ai_soan += 1
        for c in canh:
            clip[c.get("trang_thai") or "chua"] += 1
            may[c.get("may") or d.get("may_clip") or "?"] += 1

    # Nhật ký từng lượt quay
    luot = Counter()
    tuan = Counter()
    moc = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    try:
        with open(os.path.join(DL, "review_sp", "nhat_ky_quay.jsonl"), encoding="utf-8") as f:
            for dong in f:
                try:
                    j = json.loads(dong)
                except Exception:  # noqa: BLE001
                    continue
                kq = j.get("ket_qua") or "?"
                luot[kq] += 1
                if (j.get("luc") or "") >= moc:
                    tuan[kq] += 1
    except FileNotFoundError:
        pass
    tong_luot = sum(luot.values())

    # 7. Giao cho brand
    dv = _doc(os.path.join(DL, "luu_drive", "playah_demo.json"), {}) or {}
    giao = {
        "drive": f"https://drive.google.com/drive/folders/{dv['thu_muc_04']}" if dv.get("thu_muc_04") else "",
        "sheet": f"https://docs.google.com/spreadsheets/d/{dv['sheet_danh_gia']}" if dv.get("sheet_danh_gia") else "",
        "audit": f"https://docs.google.com/spreadsheets/d/{dv['sheet_audit']}" if dv.get("sheet_audit") else "",
    }

    return {
        "ok": True,
        "soi": soi,
        "kho": kho_tt,
        "video": {"tong": video, "ai_soan": ai_soan, "co_phim": co_phim},
        "clip": {"xong": clip.get("xong", 0), "loi": clip.get("loi", 0), "tong": sum(clip.values()),
                 "may": dict(may.most_common(4))},
        "luot": {"tong": tong_luot, "xong": luot.get("xong", 0), "tu_choi": luot.get("tu_choi", 0),
                 "loi": luot.get("loi", 0), "ty_le": round(100 * luot.get("xong", 0) / tong_luot) if tong_luot else None,
                 "tuan": sum(tuan.values()), "tuan_xong": tuan.get("xong", 0)},
        "giao": giao,
        "rea": os.path.isdir(os.path.join(GOC, "rea_venv")),
    }


@bp.route("/api/quy_trinh")
def api_quy_trinh():
    try:
        return jsonify(so_lieu())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
