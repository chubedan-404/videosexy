"""
TTS Engine — Xử lý Text-to-Speech đa ngôn ngữ với Microsoft Edge TTS.
Hỗ trợ nhiều giọng đọc, nhiều ngôn ngữ, chất lượng cao hoàn toàn miễn phí.
"""
import asyncio
import os
import subprocess
import time
import edge_tts
from typing import List, Dict, Optional

# Danh sách giọng đọc phong phú theo ngôn ngữ (kèm giọng đa ngôn ngữ tự nhiên)
VOICE_PRESETS: Dict[str, List[Dict]] = {
    "vi": [
        {"name": "vi-VN-HoaiMyNeural", "label": "🇻🇳 Hoài My (Nữ - Tự Nhiên, Nhẹ Nhàng)", "gender": "Female"},
        {"name": "vi-VN-NamMinhNeural", "label": "🇻🇳 Nam Minh (Nam - Chuyên Nghiệp, Truyền Cảm)", "gender": "Male"},
        {"name": "en-US-AvaMultilingualNeural", "label": "🌐 Ava (Nữ - Đa Ngôn Ngữ, Trẻ Trung)", "gender": "Female"},
        {"name": "en-US-AndrewMultilingualNeural", "label": "🌐 Andrew (Nam - Đa Ngôn Ngữ, Ấm Áp)", "gender": "Male"},
        {"name": "en-US-EmmaMultilingualNeural", "label": "🌐 Emma (Nữ - Đa Ngôn Ngữ, Vui Tươi)", "gender": "Female"},
        {"name": "en-US-BrianMultilingualNeural", "label": "🌐 Brian (Nam - Đa Ngôn Ngữ, Thân Thiện)", "gender": "Male"},
        {"name": "en-AU-WilliamMultilingualNeural", "label": "🌐 William (Nam - Đa Ngôn Ngữ, Ấm Trầm)", "gender": "Male"},
        {"name": "fr-FR-RemyMultilingualNeural", "label": "🌐 Rémy (Nam - Đa Ngôn Ngữ, Trầm Êm)", "gender": "Male"},
        {"name": "it-IT-GiuseppeMultilingualNeural", "label": "🌐 Giuseppe (Nam - Đa Ngôn Ngữ, Trầm Dày)", "gender": "Male"},
        {"name": "ko-KR-HyunsuMultilingualNeural", "label": "🌐 Hyunsu (Nam - Đa Ngôn Ngữ, Ấm Nhẹ)", "gender": "Male"},
        {"name": "de-DE-FlorianMultilingualNeural", "label": "🌐 Florian (Nam - Đa Ngôn Ngữ, Điềm Đạm)", "gender": "Male"},
        {"name": "fr-FR-VivienneMultilingualNeural", "label": "🌐 Vivienne (Nữ - Đa Ngôn Ngữ, Êm Dịu)", "gender": "Female"},
        {"name": "de-DE-SeraphinaMultilingualNeural", "label": "🌐 Seraphina (Nữ - Đa Ngôn Ngữ, Dịu Dàng)", "gender": "Female"},
        {"name": "pt-BR-ThalitaMultilingualNeural", "label": "🌐 Thalita (Nữ - Đa Ngôn Ngữ, Mềm Mại)", "gender": "Female"},
    ],
    "en": [
        {"name": "en-US-JennyNeural", "label": "🇺🇸 Jenny (Nữ - Thân Thiện)", "gender": "Female"},
        {"name": "en-US-GuyNeural", "label": "🇺🇸 Guy (Nam - Chuyên Nghiệp)", "gender": "Male"},
        {"name": "en-US-AvaMultilingualNeural", "label": "🇺🇸 Ava (Nữ - Tự Nhiên)", "gender": "Female"},
        {"name": "en-US-AndrewMultilingualNeural", "label": "🇺🇸 Andrew (Nam - Ấm Áp)", "gender": "Male"},
        {"name": "en-GB-SoniaNeural", "label": "🇬🇧 Sonia (Nữ - Anh)", "gender": "Female"},
    ],
    "zh": [
        {"name": "zh-CN-XiaoxiaoNeural", "label": "🇨🇳 Xiaoxiao (Nữ - Tươi Vui)", "gender": "Female"},
        {"name": "zh-CN-XiaoyiNeural", "label": "🇨🇳 Xiaoyi (Nữ - Hoạt Hình / Đáng Yêu)", "gender": "Female"},
        {"name": "zh-CN-YunxiNeural", "label": "🇨🇳 Yunxi (Nam - Tươi Sáng)", "gender": "Male"},
        {"name": "zh-CN-YunxiaNeural", "label": "🇨🇳 Yunxia (Nam - Hoạt Hình Cute)", "gender": "Male"},
        {"name": "zh-CN-YunjianNeural", "label": "🇨🇳 Yunjian (Nam - Trầm Ấm)", "gender": "Male"},
    ],
    "ko": [
        {"name": "ko-KR-SunHiNeural", "label": "🇰🇷 SunHi (Nữ)", "gender": "Female"},
        {"name": "ko-KR-InJoonNeural", "label": "🇰🇷 InJoon (Nam)", "gender": "Male"},
        {"name": "ko-KR-HyunsuMultilingualNeural", "label": "🇰🇷 Hyunsu (Nam - Tự Nhiên)", "gender": "Male"},
    ],
    "ja": [
        {"name": "ja-JP-NanamiNeural", "label": "🇯🇵 Nanami (Nữ)", "gender": "Female"},
        {"name": "ja-JP-KeitaNeural", "label": "🇯🇵 Keita (Nam)", "gender": "Male"},
    ],
    "th": [
        {"name": "th-TH-PremwadeeNeural", "label": "🇹🇭 Premwadee (Nữ)", "gender": "Female"},
        {"name": "th-TH-NiwatNeural", "label": "🇹🇭 Niwat (Nam)", "gender": "Male"},
    ],
    "id": [
        {"name": "id-ID-GadisNeural", "label": "🇮🇩 Gadis (Nữ)", "gender": "Female"},
        {"name": "id-ID-ArdiNeural", "label": "🇮🇩 Ardi (Nam)", "gender": "Male"},
    ],
    "fr": [
        {"name": "fr-FR-DeniseNeural", "label": "🇫🇷 Denise (Nữ)", "gender": "Female"},
        {"name": "fr-FR-HenriNeural", "label": "🇫🇷 Henri (Nam)", "gender": "Male"},
    ],
    "de": [
        {"name": "de-DE-KatjaNeural", "label": "🇩🇪 Katja (Nữ)", "gender": "Female"},
        {"name": "de-DE-ConradNeural", "label": "🇩🇪 Conrad (Nam)", "gender": "Male"},
    ],
    "es": [
        {"name": "es-ES-ElviraNeural", "label": "🇪🇸 Elvira (Nữ)", "gender": "Female"},
        {"name": "es-ES-AlvaroNeural", "label": "🇪🇸 Alvaro (Nam)", "gender": "Male"},
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# KHO GIỌNG CHO AI SHORTS STUDIO
# ─────────────────────────────────────────────────────────────────────────────
# Một "giọng" ở đây không chỉ là tên giọng của Microsoft, mà là bộ ba: giọng
# gốc, tốc độ đọc và cao độ. Cùng một giọng Hoài My, hạ tốc độ và hạ cao độ
# xuống là ra chất thủ thỉ kể chuyện, đẩy lên là ra chất tươi tắn. Nhờ vậy hai
# giọng tiếng Việt của Microsoft đủ sức phủ nhiều sắc thái khác nhau.
#
# Nhóm "vi" là giọng tiếng Việt bản xứ, phát âm chuẩn nhất.
# Nhóm "ml" là giọng đa ngôn ngữ: đọc được tiếng Việt và có chất trầm ấm mà hai
# giọng bản xứ không có, đổi lại đôi chỗ hơi lơ lớ. Đã thử đọc thật một câu
# tiếng Việt bằng cả 14 giọng rồi cho máy nghe lại, giọng nào cũng ra chữ.

SHORTS_VOICE_GROUPS = [
    {"id": "vi_warm", "label": "Tiếng Việt bản xứ, trầm ấm và nhẹ nhàng"},
    {"id": "vi_bright", "label": "Tiếng Việt bản xứ, tươi tắn và dẫn nhanh"},
    {"id": "ml_warm", "label": "Giọng đa ngôn ngữ, trầm ấm (đôi chỗ hơi lơ lớ)"},
    {"id": "native_global", "label": "Giọng bản xứ theo thị trường quốc tế"},
]

SHORTS_VOICE_PRESETS: List[Dict] = [
    # ── Tiếng Việt bản xứ, trầm ấm nhẹ nhàng ────────────────────────────────
    {"id": "hoaimy_diu", "group": "vi_warm", "label": "Hoài My dịu dàng",
     "desc": "Nữ, chậm vừa, hợp mọi chủ đề nhẹ nhàng",
     "voice": "vi-VN-HoaiMyNeural", "rate": "-6%", "pitch": "-3Hz"},
    {"id": "hoaimy_thuthi", "group": "vi_warm", "label": "Hoài My thủ thỉ",
     "desc": "Nữ, chậm và gần, như đang nói riêng với người xem",
     "voice": "vi-VN-HoaiMyNeural", "rate": "-14%", "pitch": "-6Hz"},
    {"id": "hoaimy_tram", "group": "vi_warm", "label": "Hoài My trầm ấm",
     "desc": "Nữ, hạ cao độ nhiều, giọng dày và ấm",
     "voice": "vi-VN-HoaiMyNeural", "rate": "-8%", "pitch": "-12Hz"},
    {"id": "hoaimy_kechuyen", "group": "vi_warm", "label": "Hoài My kể chuyện đêm khuya",
     "desc": "Nữ, chậm nhất, hợp chuyện tâm sự và chữa lành",
     "voice": "vi-VN-HoaiMyNeural", "rate": "-18%", "pitch": "-9Hz"},
    {"id": "namminh_tram", "group": "vi_warm", "label": "Nam Minh trầm ấm",
     "desc": "Nam, ấm và điềm đạm, hợp kiến thức và tài chính",
     "voice": "vi-VN-NamMinhNeural", "rate": "-8%", "pitch": "-10Hz"},
    {"id": "namminh_daoly", "group": "vi_warm", "label": "Nam Minh đạo lý",
     "desc": "Nam, trầm ấm điềm đạm, chất giọng chiêm nghiệm sâu sắc, chuyên lời dẫn đạo lý",
     "voice": "vi-VN-NamMinhNeural", "rate": "-8%", "pitch": "-10Hz"},
    {"id": "namminh_thuthi", "group": "vi_warm", "label": "Nam Minh thủ thỉ",
     "desc": "Nam, chậm và gần, hợp chuyện đời và tâm sự",
     "voice": "vi-VN-NamMinhNeural", "rate": "-14%", "pitch": "-5Hz"},
    {"id": "namminh_khan", "group": "vi_warm", "label": "Nam Minh trầm khàn",
     "desc": "Nam, trầm sâu nhất, hợp chuyện nghiêm túc",
     "voice": "vi-VN-NamMinhNeural", "rate": "-10%", "pitch": "-20Hz"},
    # Thêm 15/09/2026 cho video kể chuyện dài. Trong kho cũ, giọng trầm nhất
    # (namminh_khan, -20Hz) lại đọc khá nhanh, còn giọng chậm nhất (thủ thỉ,
    # -14%) thì chưa đủ trầm. Video ba mươi phút cần cả hai cùng lúc: trầm để
    # nghe lâu không mệt, chậm để người xem kịp theo câu chuyện.
    {"id": "namminh_kechuyen", "group": "vi_warm", "label": "Nam Minh kể chuyện đêm khuya",
     "desc": "Nam, vừa trầm vừa chậm, hợp video kể chuyện dài nghe trước khi ngủ",
     "voice": "vi-VN-NamMinhNeural", "rate": "-18%", "pitch": "-18Hz"},
    {"id": "namminh_chinchan", "group": "vi_warm", "label": "Nam Minh chững chạc",
     "desc": "Nam, chậm nhẹ, giọng người dẫn tin",
     "voice": "vi-VN-NamMinhNeural", "rate": "-4%", "pitch": "-6Hz"},

    # ── Tiếng Việt bản xứ, tươi tắn ─────────────────────────────────────────
    {"id": "hoaimy_chuan", "group": "vi_bright", "label": "Hoài My chuẩn",
     "desc": "Nữ, tốc độ gốc của tool từ trước tới nay",
     "voice": "vi-VN-HoaiMyNeural", "rate": "+5%", "pitch": "+0Hz"},
    {"id": "hoaimy_tuoi", "group": "vi_bright", "label": "Hoài My tươi tắn",
     "desc": "Nữ, nhanh và sáng, hợp mẹo vặt và tin nhanh",
     "voice": "vi-VN-HoaiMyNeural", "rate": "+8%", "pitch": "+5Hz"},
    {"id": "namminh_chuan", "group": "vi_bright", "label": "Nam Minh chuẩn",
     "desc": "Nam, tốc độ gốc của tool từ trước tới nay",
     "voice": "vi-VN-NamMinhNeural", "rate": "+5%", "pitch": "+0Hz"},
    {"id": "namminh_nhanh", "group": "vi_bright", "label": "Nam Minh dẫn nhanh",
     "desc": "Nam, nhịp gấp, hợp video nhiều thông tin",
     "voice": "vi-VN-NamMinhNeural", "rate": "+14%", "pitch": "+0Hz"},

    # ── Giọng đa ngôn ngữ, trầm ấm ──────────────────────────────────────────
    {"id": "andrew_tram", "group": "ml_warm", "label": "Andrew trầm ấm",
     "desc": "Nam, chất giọng dày và chắc, ấm nhất nhóm này",
     "voice": "en-US-AndrewMultilingualNeural", "rate": "-8%", "pitch": "-12Hz"},
    {"id": "andrew_am", "group": "ml_warm", "label": "Andrew ấm nhẹ",
     "desc": "Nam, ấm vừa phải, dễ nghe cho video dài",
     "voice": "en-US-AndrewMultilingualNeural", "rate": "-5%", "pitch": "-5Hz"},
    {"id": "brian_thuthi", "group": "ml_warm", "label": "Brian thủ thỉ",
     "desc": "Nam, mộc và gần, như đang trò chuyện",
     "voice": "en-US-BrianMultilingualNeural", "rate": "-10%", "pitch": "-8Hz"},
    {"id": "william_am", "group": "ml_warm", "label": "William ấm trầm",
     "desc": "Nam, chậm rãi, hợp kể chuyện",
     "voice": "en-AU-WilliamMultilingualNeural", "rate": "-6%", "pitch": "-8Hz"},
    {"id": "remy_tram", "group": "ml_warm", "label": "Rémy trầm êm",
     "desc": "Nam, êm và mượt",
     "voice": "fr-FR-RemyMultilingualNeural", "rate": "-8%", "pitch": "-10Hz"},
    {"id": "giuseppe_tram", "group": "ml_warm", "label": "Giuseppe trầm dày",
     "desc": "Nam, trầm dày, hợp nội dung nghiêm túc",
     "voice": "it-IT-GiuseppeMultilingualNeural", "rate": "-8%", "pitch": "-12Hz"},
    {"id": "florian_diemdam", "group": "ml_warm", "label": "Florian điềm đạm",
     "desc": "Nam, chậm và bình tĩnh",
     "voice": "de-DE-FlorianMultilingualNeural", "rate": "-8%", "pitch": "-8Hz"},
    {"id": "hyunsu_am", "group": "ml_warm", "label": "Hyunsu ấm nhẹ",
     "desc": "Nam, nhẹ và trong",
     "voice": "ko-KR-HyunsuMultilingualNeural", "rate": "-6%", "pitch": "-6Hz"},
    {"id": "ava_diu", "group": "ml_warm", "label": "Ava dịu dàng",
     "desc": "Nữ, mềm và biểu cảm",
     "voice": "en-US-AvaMultilingualNeural", "rate": "-6%", "pitch": "-4Hz"},
    {"id": "emma_nhe", "group": "ml_warm", "label": "Emma nhẹ nhàng",
     "desc": "Nữ, trong và rõ chữ",
     "voice": "en-US-EmmaMultilingualNeural", "rate": "-6%", "pitch": "-2Hz"},
    {"id": "seraphina_diu", "group": "ml_warm", "label": "Seraphina dịu",
     "desc": "Nữ, êm và đều",
     "voice": "de-DE-SeraphinaMultilingualNeural", "rate": "-6%", "pitch": "-4Hz"},
    {"id": "vivienne_em", "group": "ml_warm", "label": "Vivienne êm",
     "desc": "Nữ, êm và hơi thầm",
     "voice": "fr-FR-VivienneMultilingualNeural", "rate": "-7%", "pitch": "-4Hz"},
    {"id": "thalita_mem", "group": "ml_warm", "label": "Thalita mềm mại",
     "desc": "Nữ, mềm và tròn tiếng",
     "voice": "pt-BR-ThalitaMultilingualNeural", "rate": "-6%", "pitch": "-4Hz"},

    # ── Giọng bản xứ: dùng khi AI Shorts viết đúng ngôn ngữ thị trường ─────
    {"id": "jenny_us", "group": "native_global", "label": "🇺🇸 Jenny — English US",
     "desc": "Nữ Mỹ, rõ và thân thiện", "voice": "en-US-JennyNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "sonia_uk", "group": "native_global", "label": "🇬🇧 Sonia — English UK",
     "desc": "Nữ Anh, tự nhiên và điềm đạm", "voice": "en-GB-SoniaNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "elvira_es", "group": "native_global", "label": "🇪🇸 Elvira — Español",
     "desc": "Nữ Tây Ban Nha, tự nhiên", "voice": "es-ES-ElviraNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "francisca_br", "group": "native_global", "label": "🇧🇷 Francisca — Português BR",
     "desc": "Nữ Brazil, ấm và rõ", "voice": "pt-BR-FranciscaNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "denise_fr", "group": "native_global", "label": "🇫🇷 Denise — Français",
     "desc": "Nữ Pháp, tự nhiên", "voice": "fr-FR-DeniseNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "katja_de", "group": "native_global", "label": "🇩🇪 Katja — Deutsch",
     "desc": "Nữ Đức, rõ ràng", "voice": "de-DE-KatjaNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "nanami_jp", "group": "native_global", "label": "🇯🇵 Nanami — 日本語",
     "desc": "Nữ Nhật, tự nhiên", "voice": "ja-JP-NanamiNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "sunhi_kr", "group": "native_global", "label": "🇰🇷 SunHi — 한국어",
     "desc": "Nữ Hàn, tự nhiên", "voice": "ko-KR-SunHiNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "premwadee_th", "group": "native_global", "label": "🇹🇭 Premwadee — ไทย",
     "desc": "Nữ Thái, tự nhiên", "voice": "th-TH-PremwadeeNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "gadis_id", "group": "native_global", "label": "🇮🇩 Gadis — Indonesia",
     "desc": "Nữ Indonesia, tự nhiên", "voice": "id-ID-GadisNeural", "rate": "+0%", "pitch": "+0Hz"},
    {"id": "xiaoxiao_cn", "group": "native_global", "label": "🇨🇳 Xiaoxiao — 中文",
     "desc": "Nữ Trung Quốc, tự nhiên", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "+0Hz"},
]

# Tốc độ đọc THẬT của từng giọng, đo bằng cách cho đọc ba câu tiếng Việt ở tốc
# độ +5% rồi cắt khoảng lặng hai đầu đúng như lúc render, sau đó lấy số từ chia
# cho số giây. Đây là nền của mọi con số thời lượng hiện trên giao diện.
# Đo lại bằng cách chạy lại phép đo nếu bạn thêm giọng mới; đừng đoán.
VOICE_WORDS_PER_SECOND: Dict[str, float] = {
    "vi-VN-HoaiMyNeural": 4.23,
    "vi-VN-NamMinhNeural": 4.19,
    "en-US-AndrewMultilingualNeural": 3.84,
    "en-US-AvaMultilingualNeural": 4.27,
    "en-US-BrianMultilingualNeural": 4.01,
    "en-US-EmmaMultilingualNeural": 4.16,
    "en-AU-WilliamMultilingualNeural": 4.36,
    "fr-FR-VivienneMultilingualNeural": 4.81,
    "fr-FR-RemyMultilingualNeural": 4.65,
    "de-DE-SeraphinaMultilingualNeural": 4.16,
    "de-DE-FlorianMultilingualNeural": 4.54,
    "it-IT-GiuseppeMultilingualNeural": 4.51,
    "ko-KR-HyunsuMultilingualNeural": 4.25,
    "pt-BR-ThalitaMultilingualNeural": 4.39,
}
MEASURED_AT_RATE = "+5%"
DEFAULT_WORDS_PER_SECOND = 4.2

SHORTS_VOICE_BY_ID = {v["id"]: v for v in SHORTS_VOICE_PRESETS}
DEFAULT_SHORTS_VOICE_ID = "hoaimy_diu"


def list_shorts_voices() -> List[Dict]:
    """Kho giọng của AI Shorts Studio, đã xếp theo nhóm để đổ vào ô chọn."""
    return [dict(v) for v in SHORTS_VOICE_PRESETS]


# Giọng người dùng tự nhân bản mang tiền tố này. Phải có tiền tố chứ không thể
# để mã trần: nhánh cuối của hàm dưới coi mọi mã lạ là tên giọng Microsoft, nên
# một mã giọng nhân bản lọt vào đó sẽ được gửi thẳng sang Edge TTS và cả video
# ra giọng máy mặc định mà không có gì báo.
CLONE_PREFIX = "clone:"


def la_giong_clone(value) -> bool:
    return str(value or "").strip().startswith(CLONE_PREFIX)


def ma_giong_clone(value) -> str:
    """Mã giọng trong kho, bóc khỏi tiền tố. Không phải giọng clone thì rỗng."""
    chu = str(value or "").strip()
    return chu[len(CLONE_PREFIX):].strip() if la_giong_clone(chu) else ""


def resolve_shorts_voice(value: str, rate: Optional[str] = None,
                         pitch: Optional[str] = None) -> Dict:
    """
    Đổi giá trị ô chọn giọng thành bộ ba giọng, tốc độ, cao độ.

    Nhận cả mã giọng mới (ví dụ "namminh_tram") lẫn tên giọng cũ của Microsoft
    (ví dụ "vi-VN-HoaiMyNeural"), để những lần render cũ và các phần khác của
    tool gọi sang vẫn chạy đúng như trước.

    Giọng nhân bản trả về kèm khoá "clone" để bên đọc lời rẽ sang bộ nhân bản.
    Tốc độ và cao độ để nguyên: model nhân bản không nhận hai tham số ấy, chất
    giọng đã nằm trong đoạn mẫu rồi.
    """
    key = (value or "").strip()
    if la_giong_clone(key):
        return {"id": key, "clone": ma_giong_clone(key), "label": key,
                "voice": key, "rate": rate or "+0%", "pitch": pitch or "+0Hz"}
    preset = SHORTS_VOICE_BY_ID.get(key)
    if preset:
        return {
            "id": preset["id"],
            "label": preset["label"],
            "voice": preset["voice"],
            "rate": rate or preset["rate"],
            "pitch": pitch or preset["pitch"],
        }
    return {
        "id": "",
        "label": key or "vi-VN-HoaiMyNeural",
        "voice": key or "vi-VN-HoaiMyNeural",
        "rate": rate or "+5%",
        "pitch": pitch or "+0Hz",
    }


# Các preset cao độ giọng (Pitch presets)
VOICE_PITCH_PRESETS = [
    {"value": "+0Hz", "label": "Bình Thường (Chuẩn)"},
    {"value": "+25Hz", "label": "👧 Bé Gái / Đáng Yêu (+25Hz)"},
    {"value": "+20Hz", "label": "👦 Bé Trai / Hoạt Hình (+20Hz)"},
    {"value": "+10Hz", "label": "Tươi Vui / Trẻ Trung (+10Hz)"},
    {"value": "-10Hz", "label": "Trầm Ấm (-10Hz)"},
    {"value": "-15Hz", "label": "👵 Bác Gái / Lớn Tuổi (-15Hz)"},
    {"value": "-20Hz", "label": "👴 Bác Trai / Trầm Khàn (-20Hz)"},
]

# Ngôn ngữ được hỗ trợ (để hiển thị trên UI)
SUPPORTED_LANGUAGES = [
    {"code": "vi", "label": "🇻🇳 Tiếng Việt"},
    {"code": "en", "label": "🇺🇸 Tiếng Anh"},
    {"code": "zh-CN", "label": "🇨🇳 Tiếng Trung (Giản Thể)"},
    {"code": "zh-TW", "label": "🇹🇼 Tiếng Trung (Phồn Thể)"},
    {"code": "ko", "label": "🇰🇷 Tiếng Hàn"},
    {"code": "ja", "label": "🇯🇵 Tiếng Nhật"},
    {"code": "th", "label": "🇹🇭 Tiếng Thái"},
    {"code": "id", "label": "🇮🇩 Tiếng Indonesia"},
    {"code": "fr", "label": "🇫🇷 Tiếng Pháp"},
    {"code": "de", "label": "🇩🇪 Tiếng Đức"},
    {"code": "es", "label": "🇪🇸 Tiếng Tây Ban Nha"},
]


def get_voices_for_lang(lang_code: str) -> List[Dict]:
    """
    Trả về danh sách giọng đọc cho một ngôn ngữ.
    """
    base_lang = lang_code.split("-")[0].lower()
    return VOICE_PRESETS.get(base_lang, VOICE_PRESETS.get("vi", []))


def get_default_voice(lang_code: str) -> str:
    """
    Trả về giọng đọc mặc định cho ngôn ngữ.
    """
    voices = get_voices_for_lang(lang_code)
    if voices:
        return voices[0]["name"]
    return "vi-VN-HoaiMyNeural"


async def _generate_single_audio(
    text: str,
    voice: str,
    output_path: str,
    rate: str = "+0%",
    pitch: str = "+0Hz"
) -> bool:
    """
    Tạo file audio từ text dùng edge-tts (async).
    """
    from text_normalizer import normalize_for_speech
    spoken = normalize_for_speech(text, lang=(voice or "")[:2])
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text=spoken, voice=voice, rate=rate, pitch=pitch)
            await communicate.save(output_path)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                return True
        except Exception as e:
            print(f"[⚠️ Edge TTS lần {attempt + 1}/3] {e}")
        if attempt < 2:
            await asyncio.sleep(0.8 * (2 ** attempt))
    return False


def generate_audio_sync(
    text: str,
    voice: str,
    output_path: str,
    rate: str = "+0%",
    pitch: str = "+0Hz"
) -> bool:
    """
    Wrapper đồng bộ để chạy edge-tts từ non-async code.
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_generate_single_audio(text, voice, output_path, rate, pitch))
        loop.close()
        return result
    except Exception as e:
        print(f"[⚠️ TTS Sync Error] {e}")
        return False


class TTSEngine:
    """
    Engine tạo voice-over đa ngôn ngữ với Microsoft Edge TTS.
    Tự động ghép các đoạn audio theo mốc thời gian phụ đề.
    """

    def __init__(self, download_dir: str, ffmpeg_cmd: str = "ffmpeg"):
        self.download_dir = download_dir
        self.ffmpeg_cmd = ffmpeg_cmd
        self.last_preview_provider = "edge"

    def _get_audio_duration_ms(self, file_path: str) -> int:
        """Đo độ dài audio tính theo milliseconds."""
        try:
            cmd = [self.ffmpeg_cmd, '-i', file_path]
            res = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
            for line in res.stderr.split('\n'):
                if 'Duration:' in line:
                    dur_str = line.split('Duration:')[1].split(',')[0].strip()
                    h, m, s = dur_str.split(':')
                    return int((float(h) * 3600 + float(m) * 60 + float(s)) * 1000)
        except Exception:
            pass
        return 2000

    def _parse_timestamp_ms(self, ts_val) -> int:
        """Chuyển đổi timestamp (float seconds, int ms, hoặc chuỗi SRT) sang milliseconds."""
        if ts_val is None:
            return 0
        if isinstance(ts_val, (int, float)):
            # Float thường là số giây từ Whisper/SRT (ví dụ 122.799)
            return int(round(float(ts_val) * 1000))
        try:
            ts_str = str(ts_val).strip().replace(".", ",")
            if ":" in ts_str:
                parts = ts_str.split(":")
                if len(parts) == 3:
                    h = int(parts[0])
                    m = int(parts[1])
                    if "," in parts[2]:
                        s_part, ms_part = parts[2].split(",")
                        s = int(s_part)
                        ms = int(ms_part[:3].ljust(3, "0"))
                    else:
                        s = int(parts[2])
                        ms = 0
                    return (h * 3600 + m * 60 + s) * 1000 + ms
                elif len(parts) == 2:
                    m = int(parts[0])
                    if "," in parts[1]:
                        s_part, ms_part = parts[1].split(",")
                        s = int(s_part)
                        ms = int(ms_part[:3].ljust(3, "0"))
                    else:
                        s = int(parts[1])
                        ms = 0
                    return (m * 60 + s) * 1000 + ms
            elif "," in ts_str:
                s_part, ms_part = ts_str.split(",")
                s = int(s_part)
                ms = int(ms_part[:3].ljust(3, "0"))
                return s * 1000 + ms
            else:
                return int(round(float(ts_str) * 1000))
        except Exception:
            return 0

    def _format_timestamp_ms(self, ms: int) -> str:
        """Chuyển đổi milliseconds sang định dạng timestamp SRT HH:MM:SS,mmm."""
        ms = max(0, int(ms))
        total_sec = ms // 1000
        rem_ms = ms % 1000
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        s = total_sec % 60
        return f"{h:02d}:{m:02d}:{s:02d},{rem_ms:03d}"

    def _trim_audio_silence_inplace(self, audio_path: str) -> bool:
        """
        Cắt bỏ khoảng lặng đầu và đuôi (~1.05s dead silence padding) do Edge TTS sinh ra.
        Giúp giọng đọc khớp chính xác với khẩu hình nhân vật (khởi đầu tức thì, không bị trôi delay).
        """
        if not audio_path or not os.path.exists(audio_path) or os.path.getsize(audio_path) < 100:
            return False
        
        trimmed_path = audio_path + ".trim.mp3"
        cmd = [
            self.ffmpeg_cmd, '-y',
            '-i', audio_path,
            '-af', 'silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB:detection=peak,areverse,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB:detection=peak,areverse',
            '-c:a', 'libmp3lame',
            '-b:a', '192k',
            trimmed_path
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0 and os.path.exists(trimmed_path) and os.path.getsize(trimmed_path) > 100:
                os.replace(trimmed_path, audio_path)
                return True
        except Exception:
            pass
        finally:
            if os.path.exists(trimmed_path):
                try:
                    os.remove(trimmed_path)
                except Exception:
                    pass
        return False

    def preview_voice_audio(
        self,
        text: str,
        voice: str = "vi-VN-HoaiMyNeural",
        pitch: str = "+0Hz",
        rate: str = "+0%",
        filename: str = "preview.mp3"
    ) -> Optional[str]:
        """Tạo file nghe thử giọng ngắn trong thư mục download."""
        output_path = os.path.join(self.download_dir, filename)
        self.last_preview_provider = "edge"
        ok = generate_audio_sync(text=text, voice=voice, output_path=output_path, rate=rate, pitch=pitch)
        if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            self._trim_audio_silence_inplace(output_path)
            return output_path
        # Đây chỉ là fallback cho NGHE THỬ, không dùng khi render thành phẩm.
        # Nó giúp nút preview luôn có phản hồi trong lúc Edge TTS chập chờn,
        # nhưng API/UI sẽ công khai nhà cung cấp khác để người dùng không nhầm.
        fallback_aiff = output_path + ".linh.aiff"
        try:
            say = subprocess.run(
                ["say", "-v", "Linh", "-o", fallback_aiff, text],
                capture_output=True, text=True, timeout=30,
            )
            convert = subprocess.run(
                [self.ffmpeg_cmd, "-y", "-i", fallback_aiff, "-c:a", "libmp3lame", "-b:a", "128k", output_path],
                capture_output=True, text=True, timeout=30,
            )
            if say.returncode == 0 and convert.returncode == 0 and os.path.getsize(output_path) > 100:
                self.last_preview_provider = "macos_linh_fallback"
                return output_path
        except Exception as e:
            print(f"[⚠️ TTS Preview fallback] {e}")
        finally:
            if os.path.exists(fallback_aiff):
                os.remove(fallback_aiff)
        return None

    def generate_voiceover(
        self,
        video_id: str,
        subtitles: list,
        voice: str = "vi-VN-HoaiMyNeural",
        text_field: str = "translated_text",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        characters: Optional[object] = None,
        multi_voice: bool = True,
        sync_mode: str = "triple_sync",
        max_atempo: float = 1.35,
        return_sync_info: bool = False
    ):
        """
        Tạo file voice-over đa ngôn ngữ cho toàn bộ video (hỗ trợ nhiều giọng nhân vật Multi-Voice).
        Hỗ trợ Tầng 2: Dynamic Audio Speed (atempo) co giãn tốc độ từng câu thoại 
        và Tầng 3: Đồng bộ timestamps tránh dồn toa.

        :param video_id: ID video (để đặt tên file output)
        :param subtitles: Danh sách phụ đề [{"start", "end", "translated_text", "speaker_id", "voice", "pitch", ...}]
        :param voice: Tên giọng Edge TTS mặc định
        :param text_field: Trường chứa text cần đọc (mặc định: translated_text)
        :param rate: Tốc độ đọc cơ bản (+0%, +10%, -10%...)
        :param pitch: Cao độ giọng đọc mặc định (+0Hz, +25Hz, -15Hz...)
        :param characters: Dict hoặc List thông tin nhân vật {spk_id: {voice, pitch, rate}}
        :param multi_voice: True để đọc bằng giọng từng nhân vật, False để dùng 1 giọng chung
        :param sync_mode: "triple_sync" | "standard"
        :param max_atempo: Tốc độ ép tối đa cho phép bằng atempo (mặc định 1.35x)
        :param return_sync_info: Nếu True trả về tuple (output_path, synced_subtitles, cumulative_delay_ms)
        :return: Đường dẫn file audio output hoặc tuple nếu return_sync_info=True
        """
        output_path = os.path.join(self.download_dir, f"{video_id}_voice.mp3")
        # Theo dõi các câu phải dùng giọng dự phòng gTTS (giọng máy hơn Edge TTS)
        # để burn_hardsub báo rõ cho người dùng thay vì âm thầm hạ chất lượng.
        self.gtts_fallback_indices = []
        valid_subs = [s for s in subtitles if s.get(text_field, "").strip()]

        if not valid_subs:
            print("[⚠️ TTS] Không có phụ đề hợp lệ để tạo voice-over.")
            return (None, [], 0) if return_sync_info else None

        valid_subs = sorted(valid_subs, key=lambda x: self._parse_timestamp_ms(x.get("start", "00:00:00,000")))

        # Chuẩn hóa map nhân vật
        char_map = {}
        if characters:
            if isinstance(characters, list):
                char_map = {c.get("id"): c for c in characters if c.get("id")}
            elif isinstance(characters, dict):
                char_map = characters

        print(f"[🔊 TTS] Đang tạo voice-over ({sync_mode}) cho {len(valid_subs)} câu thoại (multi_voice={multi_voice}, nhân vật: {len(char_map)})...")

        # Chuẩn bị danh sách tải song song
        tasks_data = []
        for idx, sub in enumerate(valid_subs):
            text = sub.get(text_field, "").strip()
            if text:
                temp_mp3 = os.path.join(self.download_dir, f"{video_id}_tts_{idx}.mp3")
                tasks_data.append((idx, text, temp_mp3, sub))

        # Tải song song có kiểm soát luồng (Semaphore=2) + fallback tự động gTTS
        async def batch_download():
            sem = asyncio.Semaphore(2)

            async def download_one(idx, text, path, sub):
                # Xác định giọng đọc, cao độ, tốc độ cho từng câu
                spk_id = sub.get("speaker_id")
                char_info = char_map.get(spk_id, {}) if (multi_voice and spk_id) else {}

                cue_voice = (sub.get("voice") or char_info.get("suggested_voice") or char_info.get("voice") or voice) if multi_voice else voice
                cue_pitch = (sub.get("pitch") or char_info.get("suggested_pitch") or char_info.get("pitch") or pitch) if multi_voice else pitch
                cue_rate = (sub.get("rate") or char_info.get("suggested_rate") or char_info.get("rate") or rate) if multi_voice else rate

                # Chuẩn hoá số, đơn vị, phần trăm cho giọng đọc tiếng Việt; giọng
                # ngôn ngữ khác giữ nguyên để Edge đọc số bản ngữ.
                from text_normalizer import normalize_for_speech
                spoken = normalize_for_speech(text, lang=(cue_voice or "")[:2])

                async with sem:
                    # 1. Thử Microsoft Edge-TTS
                    for attempt in range(3):
                        try:
                            comm = edge_tts.Communicate(text=spoken, voice=cue_voice, rate=cue_rate, pitch=cue_pitch)
                            await comm.save(path)
                            if os.path.exists(path) and os.path.getsize(path) > 100:
                                self._trim_audio_silence_inplace(path)
                                return (idx, path, sub)
                        except Exception as e:
                            print(f"[⚠️ Edge TTS câu #{idx}, lần {attempt + 1}/3] {e}")
                        if attempt < 2:
                            await asyncio.sleep(0.8 * (2 ** attempt))

                    # 2. Fallback dự phòng sang gTTS nếu Edge-TTS bị timeout/rate-limit
                    try:
                        from gtts import gTTS
                        base_lang = cue_voice.split("-")[0].lower() if "-" in cue_voice else "vi"
                        tts_fallback = gTTS(text=spoken, lang=base_lang)
                        tts_fallback.save(path)
                        if os.path.exists(path) and os.path.getsize(path) > 100:
                            self._trim_audio_silence_inplace(path)
                            self.gtts_fallback_indices.append(idx)
                            print(f"[ℹ️ TTS Fallback] Dùng gTTS cho câu #{idx}")
                            return (idx, path, sub)
                    except Exception as ge:
                        print(f"[❌ TTS Error #{idx}] {ge}")

                    return (idx, None, sub)

            return await asyncio.gather(*[download_one(idx, text, path, sub) for idx, text, path, sub in tasks_data])

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            results = loop.run_until_complete(batch_download())
            loop.close()
        except Exception as e:
            print(f"[❌ Batch TTS Error] {e}")
            results = []

        temp_files = []
        temp_files_to_clean = []
        filter_inputs = []
        filter_complex = []
        synced_subtitles = []
        last_end_ms = 0
        cumulative_delay_ms = 0

        for idx, temp_mp3, sub in results:
            if not temp_mp3 or not os.path.exists(temp_mp3):
                continue
            if os.path.getsize(temp_mp3) < 100:
                continue

            temp_files_to_clean.append(temp_mp3)
            start_ms = self._parse_timestamp_ms(sub.get("start", "00:00:00,000"))
            end_ms = self._parse_timestamp_ms(sub.get("end", "00:00:01,000"))
            slot_ms = max(400, end_ms - start_ms)
            raw_dur_ms = self._get_audio_duration_ms(temp_mp3)

            cue_audio_path = temp_mp3
            dur_ms = raw_dur_ms

            # 🔹 TẦNG 2: DYNAMIC AUDIO SPEED (ATEMPO PER CUE)
            if sync_mode == "triple_sync" and raw_dur_ms > slot_ms:
                ratio = raw_dur_ms / slot_ms
                if ratio > 1.04:  # Dài hơn slot trên 4%
                    atempo_val = min(ratio, max_atempo)
                    sped_mp3 = os.path.join(self.download_dir, f"{video_id}_sync_{idx}.mp3")
                    cmd_atempo = [
                        self.ffmpeg_cmd, '-y',
                        '-i', temp_mp3,
                        '-filter:a', f"atempo={atempo_val:.3f}",
                        '-vn', sped_mp3
                    ]
                    try:
                        res = subprocess.run(cmd_atempo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if res.returncode == 0 and os.path.exists(sped_mp3) and os.path.getsize(sped_mp3) > 100:
                            cue_audio_path = sped_mp3
                            temp_files_to_clean.append(sped_mp3)
                            dur_ms = self._get_audio_duration_ms(sped_mp3)
                    except Exception as at_err:
                        print(f"[⚠️ atempo error #{idx}] {at_err}")

            # 🔹 TẦNG 3: TÍNH TOÁN LẠI MỐC THỜI GIAN CHỐNG DỒN TOA
            # Nếu mốc bắt đầu gốc của câu sau đã vượt qua thời điểm kết thúc câu trước (+ khoảng đệm 60ms)
            # -> Khoảng lặng tự nhiên trong video đã hấp thụ hoàn toàn độ trễ trước đó, reset delay về 0!
            if start_ms >= last_end_ms + 60:
                adjusted_start_ms = start_ms
                cumulative_delay_ms = 0
            else:
                # Câu trước đọc dài hơn đè vào câu sau -> dịch câu sau ra sau câu trước + đệm 60ms
                adjusted_start_ms = last_end_ms + 60
                cumulative_delay_ms = adjusted_start_ms - start_ms

            actual_end_ms = adjusted_start_ms + dur_ms
            last_end_ms = actual_end_ms

            # Cập nhật phụ đề đã đồng bộ
            synced_sub = dict(sub)
            synced_sub["start"] = self._format_timestamp_ms(adjusted_start_ms)
            synced_sub["end"] = self._format_timestamp_ms(actual_end_ms)
            synced_subtitles.append(synced_sub)

            temp_files.append(cue_audio_path)
            filter_inputs.extend(['-i', cue_audio_path])
            filter_idx = len(temp_files) - 1
            filter_complex.append((filter_idx, adjusted_start_ms))

        if not temp_files:
            print("[⚠️ TTS] Không tạo được file audio nào.")
            return (None, [], 0) if return_sync_info else None

        # Chiến lược merge: chia thành batch nhỏ (max 30 inputs/batch) rồi merge kết quả
        BATCH_SIZE = 30
        num_files = len(temp_files)

        if num_files <= BATCH_SIZE:
            # Merge trực tiếp nếu ít input
            fc_parts = []
            for i, (filter_idx, delay_ms) in enumerate(filter_complex):
                fc_parts.append(f"[{filter_idx}:a]adelay={delay_ms}|{delay_ms}[a{filter_idx}];")
            mix_inputs = "".join([f"[a{fc[0]}]" for fc in filter_complex])
            full_filter = "".join(fc_parts) + f"{mix_inputs}amix=inputs={num_files}:normalize=0:dropout_transition=0[aout]"

            cmd = [self.ffmpeg_cmd, '-y'] + filter_inputs + [
                '-filter_complex', full_filter,
                '-map', '[aout]',
                '-c:a', 'mp3',
                '-b:a', '192k',
                output_path
            ]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                print(f"[✅ TTS] Đã tạo voice-over: {output_path} (delay tích lũy: {cumulative_delay_ms}ms)")
                return (output_path, synced_subtitles, cumulative_delay_ms) if return_sync_info else output_path
            except Exception as e:
                print(f"[❌ TTS Mix Error] {e}")
                return (None, [], 0) if return_sync_info else None
            finally:
                for tf in temp_files_to_clean:
                    try:
                        if os.path.exists(tf):
                            os.remove(tf)
                    except Exception:
                        pass
        else:
            # Merge theo batch
            print(f"[🔊 TTS] Video có {num_files} đoạn thoại → merge theo batch ({BATCH_SIZE}/batch)...")
            batch_outputs = []

            for batch_idx in range(0, num_files, BATCH_SIZE):
                batch_slice = filter_complex[batch_idx:batch_idx + BATCH_SIZE]
                batch_files = temp_files[batch_idx:batch_idx + BATCH_SIZE]
                batch_out = os.path.join(self.download_dir, f"_batch_{batch_idx}.mp3")

                b_inputs = []
                fc_parts = []
                for local_idx, (orig_idx, delay_ms) in enumerate(batch_slice):
                    b_inputs.extend(['-i', batch_files[local_idx]])
                    fc_parts.append(f"[{local_idx}:a]adelay={delay_ms}|{delay_ms}[a{local_idx}];")

                mix_labels = "".join([f"[a{i}]" for i in range(len(batch_slice))])
                b_filter = "".join(fc_parts) + f"{mix_labels}amix=inputs={len(batch_slice)}:duration=longest:normalize=0:dropout_transition=0[aout]"

                cmd = [self.ffmpeg_cmd, '-y'] + b_inputs + [
                    '-filter_complex', b_filter,
                    '-map', '[aout]',
                    '-c:a', 'mp3', '-b:a', '192k',
                    batch_out
                ]

                try:
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    if os.path.exists(batch_out) and os.path.getsize(batch_out) > 100:
                        batch_outputs.append(batch_out)
                    else:
                        print(f"[⚠️ TTS Batch {batch_idx}] Output rỗng, bỏ qua.")
                except Exception as e:
                    print(f"[⚠️ TTS Batch Error #{batch_idx}] {e}")

            if not batch_outputs:
                print("[❌ TTS] Không merge được batch nào.")
                return (None, [], 0) if return_sync_info else None

            if len(batch_outputs) == 1:
                # Chỉ 1 batch → rename thành output
                import shutil
                shutil.move(batch_outputs[0], output_path)
            else:
                # Merge tất cả batch bằng amix với duration=longest để không bị cắt ngắn
                merge_inputs = []
                merge_labels = []
                for i, bo in enumerate(batch_outputs):
                    merge_inputs.extend(['-i', bo])
                    merge_labels.append(f"[{i}:a]")

                m_filter = f"{''.join(merge_labels)}amix=inputs={len(batch_outputs)}:duration=longest:normalize=0:dropout_transition=0[aout]"
                cmd_merge = [self.ffmpeg_cmd, '-y'] + merge_inputs + [
                    '-filter_complex', m_filter,
                    '-map', '[aout]',
                    '-c:a', 'mp3', '-b:a', '192k',
                    output_path
                ]

                try:
                    subprocess.run(cmd_merge, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except Exception as e:
                    print(f"[❌ TTS Final Merge Error] {e}")
                    return (None, [], 0) if return_sync_info else None

            # Cleanup batch files
            for bo in batch_outputs:
                try:
                    if os.path.exists(bo):
                        os.remove(bo)
                except Exception:
                    pass

            # Cleanup temp TTS files
            for tf in temp_files_to_clean:
                try:
                    if os.path.exists(tf):
                        os.remove(tf)
                except Exception:
                    pass

            if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                print(f"[✅ TTS] Đã tạo voice-over ({num_files} đoạn): {output_path} (delay tích lũy cuối: {cumulative_delay_ms}ms)")
                return (output_path, synced_subtitles, cumulative_delay_ms) if return_sync_info else output_path
            else:
                print("[❌ TTS] File output rỗng sau khi merge.")
                return (None, [], 0) if return_sync_info else None
