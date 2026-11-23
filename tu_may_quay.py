"""Từ điển máy quay và ánh sáng cho AI viết prompt (21/10/2026).

Tên gọi và câu mẫu tiếng Anh lấy từ Motion Prompt của Shohei Ohtaki (pack `camera` và `photo`):
https://github.com/showheyohtaki/motion-prompt , giấy phép MIT, bản giấy phép ở vendor/motion_prompt/LICENSE.
Copyright (c) 2026 Shohei Ohtaki. Phần giải thích tiếng Việt do tool viết.

Dùng ở hai chỗ của batch_gen.viet_prompt:
1. `bang_cho_ai(video)`: danh sách tên + câu mẫu đưa vào lời dặn, để AI gọi đúng tên và tả đúng cách.
2. `chon_cho_ai(ma, video)`: những kiểu người dùng bấm chọn, AI phải dùng.
Bỏ nhóm chuyển cảnh (Jump cut, J-cut...): mỗi prompt là một clip riêng, chuyển cảnh làm lúc ghép ở Edit Studio.
"""
from __future__ import annotations

from typing import Dict, List

TRANG_MAU = "https://showheyohtaki.github.io/motion-prompt/"

# nhóm: (tên hiển thị, chỉ dùng cho video?)
NHOM = {
    "may_quay": ("Máy quay di chuyển", True),
    "cach_quay": ("Cách quay và tốc độ", True),
    "anh_sang": ("Ánh sáng", False),
    "ong_kinh": ("Ống kính và màu", False),
}

# (mã, nhóm, tên, giải thích tiếng Việt, câu mẫu tiếng Anh)
_BANG = [
    ("025", "may_quay", "Pan", "Máy đứng yên, xoay ngang trái hoặc phải",
     "Slow pan from left to right across the skyline, camera fixed in place, over 5 seconds."),
    ("026", "may_quay", "Tilt", "Máy đứng yên, ngửa lên hoặc cúi xuống",
     "Slow tilt up from the person's feet to the top of the tower."),
    ("027", "may_quay", "Dolly in", "Máy tiến lại gần nhân vật (đẩy máy, không zoom)",
     "Slow dolly in toward her face; move the camera, do not zoom."),
    ("028", "may_quay", "Zoom in", "Máy đứng yên, ống kính phóng to dần",
     "The camera stays still; zoom in on the person at the window over 3 seconds."),
    ("029", "may_quay", "Tracking shot", "Máy chạy theo nhân vật đang di chuyển",
     "Tracking shot from the side, moving along with the walking person at the same speed."),
    ("030", "may_quay", "Orbit", "Máy đi vòng quanh nhân vật",
     "Half orbit around the standing person, keeping them in the center of the frame."),
    ("031", "may_quay", "Crane up", "Máy nâng dần lên cao, thấy toàn cảnh",
     "Crane up from eye level until the whole town comes into view."),
    ("032", "may_quay", "Dolly zoom", "Nhân vật giữ nguyên, phông nền kéo giãn ra (cảm giác choáng)",
     "Dolly zoom on his face at the moment he realizes: he stays the same size while the background stretches away."),
    ("033", "cach_quay", "Rack focus", "Chuyển nét từ vật gần sang vật xa",
     "Rack focus from the cup in the foreground to the person behind it, in 1 second."),
    ("034", "cach_quay", "Handheld", "Cầm tay, rung nhẹ như quay thật",
     "Handheld shot following behind the walking person, with only a slight shake."),
    ("035", "cach_quay", "FPV drone", "Flycam bay nhanh luồn qua cảnh vật",
     "FPV drone shot weaving between the trees and out over the river."),
    ("036", "cach_quay", "Dutch angle", "Khung hình nghiêng chéo, tạo cảm giác bất an",
     "Use a Dutch angle of about 15 degrees for this uneasy scene."),
    ("037", "cach_quay", "Slow motion", "Quay chậm",
     "Show the moment of the splash in slow motion."),
    ("038", "cach_quay", "Speed ramp", "Đang nhanh thì chậm lại rồi nhanh trở lại",
     "Speed ramp: normal speed, slowing at the top of the jump, then back to normal."),
    ("039", "cach_quay", "Time-lapse", "Tua nhanh, vài giờ gói trong vài giây",
     "Time-lapse of the city from dusk to night as the lights come on."),
    ("097", "anh_sang", "Backlighting", "Nắng hoặc đèn sau lưng nhân vật, tóc sáng viền",
     "Backlit portrait of a person in a meadow, the low sun right behind their head, glowing hair outline and soft haze."),
    ("098", "anh_sang", "Rim light", "Đèn sau tạo viền sáng quanh người trên nền tối",
     "Portrait on a black background with rim lights from both rear sides; the face stays dark and only the outline of the head and shoulders glows."),
    ("099", "anh_sang", "Silhouette", "Nhân vật thành bóng đen trên nền trời sáng",
     "Silhouette of a person jumping on a beach at sunset; expose for the sky so the person and the palm tree turn pure black."),
    ("100", "anh_sang", "Split lighting", "Đèn bên hông chia mặt nửa sáng nửa tối",
     "Portrait with split lighting: the light at 90° to the side, the face divided down the nose, the far half in shadow."),
    ("101", "anh_sang", "Rembrandt lighting", "Đèn chéo cao, còn một tam giác sáng trên má tối",
     "Portrait of an old man in Rembrandt lighting: light from 45° to the side and above, leaving a small triangle of light on the shadow-side cheek."),
    ("102", "anh_sang", "Butterfly lighting", "Đèn chính diện từ trên cao, kiểu ảnh làm đẹp",
     "Beauty portrait with butterfly lighting: light straight in front and 45° above, a small butterfly-shaped shadow under the nose."),
    ("103", "anh_sang", "Golden hour", "Nắng vàng ấm lúc sáng sớm hoặc chiều tà",
     "A person in a field at golden hour: the sun near the horizon, long shadows, everything warm orange."),
    ("104", "anh_sang", "Blue hour", "Trời xanh thẫm sau hoàng hôn, đèn phố vàng ấm",
     "Harbour city at blue hour: deep blue sky, warm windows and streetlights reflected in the water."),
    ("113", "anh_sang", "High-key", "Sáng trắng, gần như không có bóng",
     "High-key still life of a cup and a bottle on a white table: pure white background, very faint shadows, only light tones."),
    ("114", "anh_sang", "Low-key", "Nền tối, chỉ chỗ được chiếu đèn nổi lên",
     "Low-key shot of a whisky bottle on black, a single side light, only the lit edges showing and most of the frame in deep black."),
    ("105", "ong_kinh", "Bokeh", "Phông nền mờ, đèn thành đốm tròn",
     "Person in a city at night with big round bokeh from the street lights behind; f/1.4, focus on the person."),
    ("106", "ong_kinh", "Tilt-shift", "Mờ trên dưới, cảnh thật trông như mô hình tí hon",
     "High-angle view of a town with a tilt-shift look: a narrow sharp band across the middle, strong blur above and below, boosted colour, like a miniature model."),
    ("107", "ong_kinh", "Long exposure", "Phơi sáng lâu, đèn xe thành vệt sáng",
     "Night road as a long exposure, 8-second shutter: the cars disappear and only their headlights and tail lights remain as white and red trails."),
    ("108", "ong_kinh", "Panning shot", "Lia theo vật đang chạy, vật nét còn nền nhoè",
     "Panning shot of a red car driving past, 1/15 s shutter: the car sharp, the trees and buildings behind streaked sideways."),
    ("109", "ong_kinh", "Lens flare", "Vệt loé sáng khi mặt trời chiếu vào ống kính",
     "Mountain landscape with the sun in frame and lens flare: coloured ghosts lined up from the sun through the centre to the opposite side."),
    ("110", "ong_kinh", "Fisheye lens", "Ống mắt cá, góc siêu rộng, đường thẳng bị cong",
     "City street through a fisheye lens, 180° view, the building edges bowing outward."),
    ("112", "ong_kinh", "Vignette", "Góc hình tối dần, mắt dồn vào giữa",
     "Beach photo with a strong vignette: the corners darken gradually so the eye goes to the person in the middle."),
    ("115", "ong_kinh", "Black and white", "Đen trắng",
     "Beach snapshot in black and white, contrast slightly raised, keeping the difference in brightness between sky and sand."),
    ("117", "ong_kinh", "Teal and orange", "Màu điện ảnh: bóng ngả xanh, da và nắng ngả cam",
     "Person walking in the city with a cinematic teal and orange grade: shadows and sky toward teal, skin and sunlight toward orange."),
    ("118", "ong_kinh", "Film grain", "Hạt nhiễu như phim nhựa",
     "Add film grain to the beach photo: fine grain all over, blacks slightly lifted and warm."),
    ("119", "ong_kinh", "Light leak", "Vệt sáng cam đỏ rò vào như phim cũ",
     "Summer memory photo with a light leak coming in from the right edge, red and orange light bleeding in."),
    ("120", "ong_kinh", "Double exposure", "Hai hình chồng lên nhau trong một khung",
     "Double exposure of a profile silhouette and a forest-and-mountain landscape on a white background, the landscape visible only inside the head."),
]

KIEU: Dict[str, Dict] = {m: {"ma": m, "nhom": n, "ten": t, "viet": v, "mau": e} for m, n, t, v, e in _BANG}


def _hop(ma: str, video: bool) -> bool:
    return video or not NHOM[KIEU[ma]["nhom"]][1]


def danh_sach(video: bool = True) -> List[Dict]:
    """Cho trang: nhóm theo thứ tự NHOM, máy ảnh thì bỏ nhóm chỉ dành cho video."""
    return [{"ma": n, "ten": ten, "chi_video": chi, "kieu": [{"ma": k["ma"], "ten": k["ten"], "viet": k["viet"]}
                                                          for k in KIEU.values() if k["nhom"] == n]}
            for n, (ten, chi) in NHOM.items() if video or not chi]


def bang_cho_ai(video: bool) -> str:
    """Danh sách tên + câu mẫu để AI chọn đúng tên và tả đúng cách."""
    return "\n".join(f"- {k['ten']}: {k['mau']}" for k in KIEU.values() if _hop(k["ma"], video))


def chon_hop_le(ma: List[str], video: bool) -> List[str]:
    """Bỏ mã lạ, mã trùng, mã không hợp máy (máy ảnh không có máy quay di chuyển). Tối đa 6."""
    ra: List[str] = []
    for m in ma or []:
        m = str(m)
        if m in KIEU and m not in ra and _hop(m, video):
            ra.append(m)
    return ra[:6]


def chon_cho_ai(ma: List[str], video: bool) -> str:
    return "\n".join(f"- {KIEU[m]['ten']}: {KIEU[m]['mau']}" for m in chon_hop_le(ma, video))
