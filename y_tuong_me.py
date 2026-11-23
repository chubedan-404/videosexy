"""
Lên ý tưởng mẻ (13/11/2026): lập trước một ĐỀ CƯƠNG khác biệt thật cho từng video của một mẻ Làm hàng loạt, rồi mới viết lời,
đọc giọng, quay.

Vì sao có module này. Trước đây mỗi video của mẻ được lập riêng: concept xoay vòng (gel chỉ hợp khoảng 5 concept, mỗi concept
một danh sách cảnh cố định), không có concept cảnh thì chép đúng các bước của mẫu đã soi. Một mẫu Rea cho 8 video là 8 lần cùng
chuỗi "nặn gel → thoa → cận hộp", chỉ khác câu hook.

Cách làm:
- Code giữ một BỘ TỪ VỰNG cố định: set studio, góc máy, cách mở, cách kể, mã cảnh. Mọi cảnh đều trong ngôn ngữ reel studio của
  PlayAh/Feelex (sản phẩm là chính, cắt nhanh, video nhấn vào chất gel có cảnh bóp; CLAUDE.md mục "Muse: ... studio kiểu reel PlayAh" và ghi nhớ
  "Quảng cáo PlayAh = studio"). Không có cảnh đời sống (bồn rửa, xe, bàn trang điểm), không xoay tuýp, không đẩy máy sát nhãn.
- MỘT lượt hỏi AI chữ cho cả số video của một sản phẩm. AI chỉ được chọn mã trong bộ từ vựng; câu lệnh tiếng Anh gửi máy làm
  clip do CODE ghép từ mã (`lenh_canh`), nên luật quay không phụ thuộc AI nhớ.
- Video mẫu chỉ cho mượn PHONG CÁCH (`soi_video.phong_cach`: góc máy, ánh sáng, nhịp), không gửi các bước của mẫu cho AI.
- CODE soát từng đề cương (đúng số cảnh, ý chất gel có cảnh bóp, không lặp mã trong một video, câu chữ qua luật nói) và so TRÙNG giữa
  các đề cương theo cấu trúc (`trung`). Trùng hay sai thì xin AI thêm, tối đa SO_LUOT_AI lượt; vẫn thiếu thì báo "đạt K/N",
  KHÔNG lặp cho đủ số.
- Ý chính chỉ được lấy từ THÔNG TIN SẢN PHẨM: thông tin trống thì đề cương chỉ nói về việc cầm, mở, nhìn sản phẩm.

`hoi` (hàm hỏi AI) truyền vào được để bài kiểm thay AI giả, không gọi AI thật.
"""
import re
import time
import unicodedata
from typing import Callable, Dict, List, Optional, Tuple

import xuong_chung as pc

SO_LUOT_AI = 3          # số lượt hỏi AI tối đa cho một sản phẩm (lượt đầu + 2 lượt xin thêm)
DU_THEM = 2             # mỗi lượt xin dư vài đề cương để còn chỗ loại bớt cái trùng
SO_VIDEO_TOI_DA_MOT_SP = 20

# ── Bộ từ vựng ───────────────────────────────────────────────────────────────

# Set studio: mỗi video MỘT set (ghi nhớ "một video = một nơi"), các video trong mẻ khác set nhau.
BO_SET: Dict[str, Tuple[str, str]] = {
    "sage_tham_long": ("Nền xanh sage, thảm lông trắng, hoa calla",
                       "a seamless soft sage-green studio backdrop, a fluffy white faux-fur rug as the surface and one white calla lily at the edge"),
    "hong_lua": ("Nền hồng phấn, vải lụa satin",
                 "a seamless blush-pink studio backdrop with draped champagne satin fabric as the surface"),
    "kem_da": ("Nền kem, bệ đá travertine",
               "a warm cream studio backdrop with two low travertine stone plinths as the surface"),
    "tim_khoi_kinh": ("Nền tím lavender, khối kính trong",
                      "a seamless pale-lavender studio backdrop with clear acrylic cubes and a glossy surface"),
    "be_hoa_kho": ("Nền be, vải lanh, hoa khô",
                   "a warm beige studio backdrop, crumpled natural linen as the surface and a few dried flowers softly blurred"),
    "trang_mat_nuoc": ("Nền trắng, mặt nước gợn sóng",
                       "a bright white studio set with a shallow tray of rippling clear water as the surface, soft caustic light"),
    "xanh_bac_ha_da": ("Nền xanh bạc hà, khay đá lạnh",
                       "a mint-green studio backdrop with a white tray of clear ice cubes as the surface"),
    "do_ruou_nhung": ("Nền đỏ rượu, nhung trầm",
                      "a deep wine-red studio backdrop with dark velvet fabric as the surface and one warm rim light"),
}

GOC_MAY: Dict[str, Tuple[str, str]] = {
    "ngang_mat": ("Ngang tầm sản phẩm", "eye-level, camera at product height, locked off"),
    "tu_tren": ("Nhìn từ trên xuống", "top-down overhead view, locked off"),
    "can_macro": ("Cận macro", "macro close-up on the content or a small detail, the product label kept out of the frame or soft"),
    "luot_ngang": ("Lướt ngang chậm", "slow sideways slide of the camera, the product stays in the same place"),
    "goc_thap": ("Góc thấp", "low angle looking slightly up at the product, locked off"),
}

CACH_MO: Dict[str, Tuple[str, str]] = {
    "cau_hoi_lo": ("Câu hỏi nỗi lo", "mở bằng một câu hỏi về một nỗi lo thường gặp mà THÔNG TIN SẢN PHẨM trả lời được"),
    "ket_qua_truoc": ("Kết quả trước", "mở bằng cảnh đẹp nhất (chất sản phẩm), lời nói kết quả trước rồi mới giải thích"),
    "mot_chu": ("Một chữ to", "mở bằng một từ hoặc cụm 1-3 chữ thật to tả đúng điều người xem đang thấy"),
    "dem_3": ("Đếm 3 điều", "mở bằng lời hứa sẽ kể đúng 3 điều, mỗi cảnh một điều"),
    "binh_luan": ("Trả lời bình luận", "mở bằng một câu hỏi của người xem (ô bình luận) rồi trả lời"),
    "lat_nguoc": ("Lật ngược hiểu lầm", "mở bằng một hiểu lầm phổ biến rồi cho xem điều ngược lại, chỉ khi THÔNG TIN SẢN PHẨM chứng minh được"),
    "bi_an_hop": ("Bí ẩn cái hộp", "mở bằng hộp đóng kín và câu hỏi bên trong là gì"),
    "am_thanh": ("Âm thanh đầu tiên", "mở bằng một âm thanh nhỏ nghe rõ (bật nắp, bóp, mở hộp), lời rất ít ở 2 giây đầu"),
    "chi_tiet": ("Một chi tiết nhỏ", "mở bằng cận một chi tiết của bao bì hoặc chất, rồi mới lùi ra cho thấy cả sản phẩm"),
    "loi_moi": ("Lời rủ thử", "mở bằng lời rủ người xem cùng thử ngay bây giờ"),
}

CACH_KE: Dict[str, Tuple[str, str]] = {
    "liet_ke": ("Liệt kê", "mỗi câu một điểm, đúng thứ tự cảnh"),
    "huong_dan": ("Hướng dẫn từng bước", "kể như đang chỉ người xem dùng: bước một, bước hai..."),
    "tra_loi": ("Hỏi đáp", "lời là câu trả lời thẳng cho câu hỏi ở đầu video"),
    "cam_nhan": ("Cảm nhận cá nhân", "giọng người dùng thật kể cảm nhận khi cầm, mở, chạm; không hứa công dụng ngoài THÔNG TIN SẢN PHẨM"),
    "it_loi": ("Ít lời, để hình nói", "lời rất ngắn, nhiều khoảng lặng để nghe tiếng thật của clip"),
    "giai_thich": ("Giải thích vì sao", "mỗi cảnh trả lời một chữ 'vì sao' về sản phẩm, dựa trên THÔNG TIN SẢN PHẨM"),
    "so_sanh": ("Trước - sau", "đặt một tình huống trước khi có sản phẩm cạnh lúc có sản phẩm, không chê sản phẩm khác"),
    "kham_pha": ("Khám phá bao bì", "đi lần lượt từ ngoài vào trong: hộp, nắp, bên trong, chất"),
}

# Ý chính của một video (13/11/2026). Người dùng chê mẻ "quá ít concept, na ná nhau, bóp gel, xoa gel lên tay hết": đề cương khác
# cách mở, cách kể nhưng ý chính vẫn chỉ là chất gel. Mỗi video trong mẻ lấy MỘT ý chính khác nhau, và chỉ ý nào THÔNG TIN SẢN PHẨM
# có nhắc (`co`: cụm đã bỏ dấu tìm trong thông tin) mới được dùng, để không bịa công dụng.
Y_CHINH: Dict[str, Tuple[str, str, str, Tuple[str, ...]]] = {
    "chat": ("Chất gel", "cho thấy chất sản phẩm: trong, sánh, mượt, không bết", r"sanh|min|trong suot|muot|bet dinh|ket cau", ("gel",)),
    "rua_sach": ("Rửa là sạch", "gốc nước, xả nước là trôi, không để lại vết", r"goc nuoc|xa nhe|rua|khong de lai vet|o tren vai", ("gel",)),
    "thanh_phan": ("Thành phần", "kể MỘT thành phần có tên trong thông tin, minh hoạ bằng đúng thành phần đó", r"lo hoi|nha dam|glycerin|thanh phan|urea", ("gel", "hop")),
    "diu_nhe": ("Dịu nhẹ", "dịu nhẹ, không hương liệu nồng, hợp vùng da nhạy cảm", r"diu|\bph\b|nhay cam|khong huong", ("gel", "hop")),
    "thiet_ke": ("Thiết kế bao bì", "hình dáng, màu, nắp, cách đứng, vẻ tối giản của bao bì", r"hop|tuyp|nap|thiet ke|toi gian", ("gel", "hop")),
    "mang_theo": ("Nhỏ gọn mang theo", "nhỏ gọn, bỏ túi, mang đi", r"nho gon|mang theo|bo tui|du lich", ("gel", "hop")),
    "kin_dao": ("Kín đáo", "giao hàng, cất giữ kín đáo", r"kin dao|giao hang", ("gel", "hop")),
    "dung_chung": ("Dùng cùng áo mưa", "dùng chung được với áo mưa (chỉ nói bằng lời, hình vẫn chỉ một sản phẩm)", r"dung chung|ao mua", ("gel",)),
    "ben_trong": ("Bên trong hộp", "mở ra thấy gì: các gói, cách xếp", r"goi|ben trong", ("hop",)),
}


def y_chinh_cho(loai: str, thong_tin: Optional[str]) -> Dict[str, Tuple[str, str, str, Tuple[str, ...]]]:
    """Các ý chính dùng được cho sản phẩm này: hợp loại, và (khi biết thông tin) có nhắc trong thông tin."""
    g = _gon(thong_tin) if thong_tin is not None else None
    return {k: v for k, v in Y_CHINH.items() if loai in v[3] and (g is None or re.search(v[2], g))}


# Mã cảnh. vat: vật chính của cảnh ("sp" = sản phẩm chính, "hop" = hộp ngoài) để chọn ảnh mẫu một vật; kieu: "tay" hoặc "sp"
# (chỉ sản phẩm, không tay); bop: cảnh bóp / nặn chất ra; loai: loại sản phẩm dùng được. Câu tiếng Anh có "the product" để máy
# làm clip theo đúng ảnh mẫu; KHÔNG có chữ xoay sản phẩm hay đẩy sát nhãn.
CANH: Dict[str, Dict] = {
    # Dùng cho cả gel lẫn hộp
    "sp_dung_yen": {"ten": "Sản phẩm đứng giữa set, ánh sáng mềm", "vat": "sp", "kieu": "sp", "loai": ("gel", "hop"),
                    "en": "The closed product stands upright alone in the centre of the set, front facing the camera, perfectly still."},
    "sp_anh_quet": {"ten": "Vệt sáng quét qua sản phẩm đứng yên", "vat": "sp", "kieu": "sp", "loai": ("gel", "hop"),
                    "en": "The closed product stands still in the centre; a soft band of light slowly sweeps across it from left to right."},
    "sp_phan_chieu": {"ten": "Sản phẩm trên mặt bóng, có bóng phản chiếu", "vat": "sp", "kieu": "sp", "loai": ("gel", "hop"),
                      "en": "The closed product stands on a glossy surface of the set with its soft mirror reflection below it, still."},
    "sp_canh_hoa": {"ten": "Sản phẩm tựa cạnh một bông hoa", "vat": "sp", "kieu": "sp", "loai": ("gel", "hop"),
                    "en": "The closed product leans gently beside one single flower on the set, front facing the camera, still."},
    "sp_nam_nghieng": {"ten": "Sản phẩm nằm nghiêng trên mặt set", "vat": "sp", "kieu": "sp", "loai": ("gel", "hop"),
                       "en": "The closed product lies on its side on the surface of the set, front label up, completely still."},
    "tay_dat_vao": {"ten": "Một bàn tay đặt sản phẩm vào giữa set", "vat": "sp", "kieu": "tay", "loai": ("gel", "hop"),
                    "en": "One hand enters and places the closed product upright in the centre of the set, then slowly withdraws."},
    "tay_cam_hop": {"y": ('thiet_ke',), "ten": "Tay cầm hộp đóng đưa vào khung", "vat": "hop", "kieu": "tay", "loai": ("gel", "hop"),
                    "en": "One hand brings the closed box into the frame and holds it still, front facing the camera."},
    "hop_dong_yen": {"y": ('thiet_ke',), "ten": "Hộp đóng đứng yên giữa set", "vat": "hop", "kieu": "sp", "loai": ("gel", "hop"),
                     "en": "Only the closed box stands upright in the centre of the set, front facing the camera, still."},
    # Gel
    "mo_hop_rut_tuyp": {"y": ('thiet_ke',), "ten": "Mở nắp hộp, rút tuýp ra", "vat": "hop", "kieu": "tay", "loai": ("gel",),
                        "en": "Two hands open the top flap of the box and slowly pull the product out of it, upward."},
    "bat_nap": {"y": ('thiet_ke',), "ten": "Ngón cái bật nắp tuýp", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                "en": "One hand holds the product upright, front facing the camera, and the thumb flips its cap open; nothing comes out yet."},
    "bop_dau_ngon": {"y": ('chat',), "ten": "Bóp gel ra đầu ngón tay", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                     "en": "One hand squeezes a small amount of the content from the opening onto the fingertip of the other hand held just below it."},
    "bop_dia": {"y": ('chat',), "ten": "Bóp gel vào một chiếc đĩa sứ nhỏ", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                "en": "One hand squeezes the content from the opening into a small empty white ceramic dish on the set."},
    "bop_vong_xoan": {"y": ('chat',), "ten": "Bóp gel thành vòng xoắn trên đĩa", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                      "en": "One hand squeezes the content slowly into a neat glossy swirl on a small white dish."},
    "bop_len_da": {"y": ('chat',), "ten": "Bóp một giọt gel lên viên đá lạnh", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                   "en": "One hand squeezes a single drop of the content from the opening onto a clear ice cube on the set."},
    "bop_len_kinh": {"y": ('chat', 'rua_sach'), "ten": "Bóp gel lên tấm kính trong", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                     "en": "One hand squeezes a short line of the content onto a clear glass panel lying on the set, seen from above."},
    "bop_len_thia_su": {"y": ("chat",), "ten": "Bóp một giọt gel lên thìa sứ trắng", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                        "en": "One hand squeezes one visible glossy bead of content onto the bowl of a small white porcelain spoon on the set. The product stays visible beside the spoon."},
    "bop_len_khay_den": {"y": ("chat",), "ten": "Bóp một vệt gel lên khay đen mờ", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                        "en": "One hand squeezes a short visible line of content onto a flat matte-black studio tray, making the clear texture stand out. The product stays visible beside the tray."},
    "bop_cang_tay": {"y": ("chat",), "ten": "Bóp một giọt gel lên cẳng tay", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                     "en": "A bare forearm rests on the studio surface. One hand holds the product in frame and squeezes one small visible bead of content from the open nozzle onto the forearm. Frame only the hand, forearm, product and gel."},
    "bop_cang_chan": {"y": ("chat",), "ten": "Bóp một giọt gel lên cẳng chân", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                      "en": "A bare lower leg from knee to ankle rests on a neutral studio support. One hand holds the product in frame and squeezes one small visible bead of content from the open nozzle onto the calf. Frame only the hand, lower leg, product and gel."},
    # Giọt tự rơi, không có tay bóp: KHÔNG tính là cảnh bóp (luật PlayAh cần một cảnh bóp/nặn thật bằng tay).
    "giot_roi": {"y": ('chat',), "ten": "Một giọt gel rơi chậm từ miệng tuýp", "vat": "sp", "kieu": "sp", "loai": ("gel",),
                 "en": "The product is held upside down by a stand, cap open, and one slow glossy drop of the content falls from its opening."},
    "keo_soi": {"y": ('chat',), "ten": "Kéo sợi gel giữa hai ngón tay", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                "en": "Thumb and index finger, already coated with the content, slowly open to stretch a thin glossy strand; the product stands softly blurred behind."},
    "nghieng_dia": {"y": ('chat',), "ten": "Nghiêng đĩa cho gel chảy chậm", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                    "en": "One hand slowly tilts the small dish so the content flows smoothly across it; the product stands softly blurred behind."},
    "can_chat": {"y": ('chat',), "ten": "Cận chất gel bóng dưới ánh sáng", "vat": "sp", "kieu": "sp", "loai": ("gel",),
                 "en": "Macro close-up of the content already resting on the set surface, glossy highlights moving slightly; the product softly blurred behind."},
    "vuot_mu_tay": {"y": ('chat', 'diu_nhe'), "ten": "Vuốt gel mỏng trên mu bàn tay", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                    "en": "One fingertip spreads a thin layer of the content across the back of the other hand; the product stands softly blurred behind."},
    "thoa_cang_tay": {"y": ("chat",), "ten": "Thoa gel trên cẳng tay, thấy rõ vệt gel", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                      "en": "A bare forearm rests on the studio surface. One fingertip slowly spreads the small bead of content already on the forearm into a visible thin glossy streak. Keep the content and the real product together in frame; no face or torso."},
    "thoa_cang_chan": {"y": ("chat",), "ten": "Thoa gel trên cẳng chân, thấy rõ vệt gel", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                       "en": "A bare lower leg from knee to ankle rests on a neutral studio support. One fingertip slowly spreads the small bead of content already on the calf into a visible thin glossy streak. Keep the content and the real product together in frame; no face or torso."},
    "cham_ngon": {"y": ('chat',), "ten": "Hai đầu ngón chạm giọt gel rồi tách ra", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                  "en": "Two fingertips touch a drop of the content and slowly part; the product stands softly blurred behind."},
    "dong_nap_dat_lai": {"y": ('thiet_ke',), "ten": "Đóng nắp, đặt tuýp về giữa set", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                         "en": "One hand snaps the cap of the product closed and sets it upright back in the centre of the set."},
    # Cảnh KHOE GEL (13/11/2026, owner: "chủ yếu show off to customer về gel này", nghĩ thêm ngoài gợi ý). Chỉ cho thấy điều nhìn
    # được (độ trong, độ bóng, độ sánh, độ trượt, tan trong nước), không hứa công dụng.
    "bop_ve_tim": {"y": ("chat",), "ten": "Bóp gel vẽ hình trái tim trên tấm kính", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                   "en": "One hand squeezes the content in one continuous line, drawing a small heart shape on a clear glass panel, seen from above. The product stays visible beside it."},
    "bop_canh_hoa": {"y": ("chat",), "ten": "Bóp một giọt gel lên cánh hoa, đọng như giọt sương", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                     "en": "One hand squeezes a single small bead of the content onto a white flower petal; it sits there round and glossy like a dew drop. The product stays visible beside the flower."},
    "bop_giay_mau": {"y": ("chat",), "ten": "Bóp gel lên tờ giấy màu, màu giấy vẫn nguyên", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                     "en": "One hand squeezes a short line of the content onto a sheet of bright coloured paper; the colour shows through it unchanged. The product stays visible beside the paper."},
    "bop_canh_giot_nuoc": {"y": ("chat",), "ten": "Bóp một giọt gel cạnh một giọt nước trên kính", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                           "en": "On a clear glass panel that already holds one drop of plain water, one hand squeezes one drop of the content right next to it. The product stays visible beside the panel."},
    "bop_lua_trang": {"y": ("rua_sach",), "ten": "Bóp một giọt gel lên mảnh lụa trắng", "vat": "sp", "kieu": "tay", "loai": ("gel",), "bop": True,
                      "can": r"vet|o tren vai|khong de lai",
                      "en": "One hand squeezes a single drop of the content onto a smooth piece of white silk fabric on the set. The product stays visible beside it."},
    "nhin_xuyen_chu": {"y": ("chat",), "ten": "Giọt gel trên kính đặt lên chữ in, nhìn xuyên vẫn rõ chữ", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                       "en": "One hand slides the glass panel holding the drop of content over a card printed with large plain black letters; the letters stay sharp and readable through the gel. The product stays visible beside it."},
    "ep_hai_kinh": {"y": ("chat",), "ten": "Ép giọt gel giữa hai tấm kính thành lớp mỏng trong", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                    "en": "One hand lowers a second clear glass slide onto the drop of content and presses gently; it spreads into a thin perfectly clear round layer between the slides. The product stays visible beside it."},
    "truot_doc": {"y": ("chat",), "ten": "Gel trượt chậm xuống tấm acrylic đặt dốc", "vat": "sp", "kieu": "sp", "loai": ("gel",),
                  "en": "The line of content slowly glides down a tilted clear acrylic sheet in one smooth glossy trail; the product stands beside the sheet."},
    "anh_sang_xuyen": {"y": ("chat",), "ten": "Đèn chiếu xuyên giọt gel, ánh sáng lấp lánh", "vat": "sp", "kieu": "sp", "loai": ("gel",),
                       "en": "A small beam of light shines through the drop of content resting on glass, making bright sparkles and a clear glow; the product stands softly blurred behind."},
    "tha_vao_nuoc": {"y": ("rua_sach",), "ten": "Thả giọt gel vào ly nước trong, gel tan dần", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                     "can": r"goc nuoc",
                     "en": "A fingertip dips the small bead of content into a clear glass of still water; it slowly dissolves into soft clear swirls. The product stands beside the glass."},
    "so_giot_nuoc": {"y": ("chat",), "ten": "Nghiêng kính: giọt nước loang, giọt gel giữ dáng", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                     "en": "One hand tilts the glass panel slightly: the water drop runs away quickly while the drop of content keeps its round shape and moves slowly. The product stays visible beside it."},
    "lat_up_dia": {"y": ("chat",), "ten": "Lật úp chiếc đĩa, gel vẫn bám lại", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                   "en": "One hand slowly turns the small dish upside down; the content stays clinging to it in one glossy mound. The product stays visible beside it."},
    "keo_soi_que": {"y": ("chat",), "ten": "Hai que gỗ nhỏ kéo gel thành sợi dài", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                    "en": "Two small wooden sticks dip into the content and slowly pull apart, stretching one long thin glossy strand. The product stays visible beside it."},
    "lau_lua_sach": {"y": ("rua_sach",), "ten": "Lau giọt gel trên lụa, lụa không để vết", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                     "can": r"vet|o tren vai|khong de lai",
                     "en": "One fingertip wipes the drop of content off the white silk with a damp cloth; the silk is left clean with no mark. The product stays visible beside it."},
    # Cảnh minh hoạ ý chính khác chất gel (13/11/2026: người dùng chê mẻ "na ná nhau, bóp gel, xoa gel lên tay hết").
    "xa_nuoc_kinh": {"y": ("rua_sach",), "ten": "Rót nước lên vệt gel trên kính, gel trôi sạch", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                     "can": r"goc nuoc|xa|rua|sach",
                     "en": "A hand pours a little clear water from a small glass over the line of content on the glass panel; it rinses away cleanly. The product stands softly blurred behind."},
    "lau_khan_trang": {"y": ("rua_sach",), "ten": "Lau giọt gel bằng khăn trắng, khăn không để vết", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                       "can": r"vet|o tren vai|khong de lai",
                       "en": "One hand wipes the drop of content off the set with a folded white cotton cloth; the cloth stays clean with no mark. The product stands softly blurred behind."},
    "la_nha_dam": {"y": ("thanh_phan",), "ten": "Lá nha đam cắt đôi nằm cạnh sản phẩm", "vat": "sp", "kieu": "sp", "loai": ("gel", "hop"),
                   "can": r"lo hoi|nha dam",
                   "en": "One fresh aloe vera leaf, cut open lengthwise, lies beside the closed product standing upright on the set, still."},
    "dung_tren_nap": {"y": ("thiet_ke",), "ten": "Tuýp đứng lộn ngược trên nắp", "vat": "sp", "kieu": "sp", "loai": ("gel",),
                      "can": r"dung duoc tren nap|dung tren nap",
                      "en": "The closed product stands upside down, balanced on its cap, alone in the centre of the set, perfectly still."},
    "can_nap_lat": {"y": ("thiet_ke",), "ten": "Cận nắp: ngón tay lật nắp mở rồi đóng", "vat": "sp", "kieu": "tay", "loai": ("gel",),
                    "can": r"nap",
                    "en": "Close-up of the cap only: one fingertip flips the cap open and snaps it closed again; nothing comes out."},
    "sp_trong_long_tay": {"y": ("mang_theo",), "ten": "Sản phẩm nằm gọn trong lòng bàn tay", "vat": "sp", "kieu": "tay", "loai": ("gel", "hop"),
                          "can": r"nho gon|mang theo|bo tui|du lich",
                          "en": "The closed product rests small and neat in one open palm held still in the centre of the set, front facing the camera."},
    "bo_vao_tui": {"y": ("mang_theo",), "ten": "Bỏ sản phẩm vào túi vải nhỏ, kéo dây", "vat": "sp", "kieu": "tay", "loai": ("gel", "hop"),
                   "can": r"nho gon|mang theo|bo tui|du lich",
                   "en": "One hand slips the closed product into a small soft linen drawstring pouch on the set and pulls the string closed."},
    "hop_giao_kin": {"y": ("kin_dao",), "ten": "Mở thùng giao hàng trơn, lấy hộp ra", "vat": "hop", "kieu": "tay", "loai": ("gel", "hop"),
                     "can": r"kin dao|giao hang",
                     "en": "Two hands open a plain unmarked brown shipping carton on the set and lift the closed box out of it."},
    # Hộp, gói
    "mo_nap_hop": {"y": ('thiet_ke',), "ten": "Nhấc nắp hộp lên", "vat": "sp", "kieu": "tay", "loai": ("hop",),
                   "en": "Two hands lift the lid of the product straight up and set it down beside it; the inside is not shown yet."},
    "lay_mot_goi": {"y": ('ben_trong',), "ten": "Rút một gói từ trong hộp", "vat": "sp", "kieu": "tay", "loai": ("hop",),
                    "en": "One hand takes a single sealed pack out of the open product, slowly, and holds it up still."},
    "xep_goi_cot": {"y": ('ben_trong',), "ten": "Xếp các gói thành một cột", "vat": "sp", "kieu": "tay", "loai": ("hop",),
                    "en": "One hand stacks the sealed packs neatly into one single column on the set beside the open product."},
    "can_mot_goi": {"y": ('ben_trong',), "ten": "Cận một gói nằm trên mặt set", "vat": "sp", "kieu": "sp", "loai": ("hop",),
                    "en": "Close-up of one single sealed pack resting on the set surface, still; the product softly blurred behind."},
    "goi_giua_ngon": {"y": ('ben_trong',), "ten": "Kẹp một gói giữa hai ngón tay", "vat": "sp", "kieu": "tay", "loai": ("hop",),
                      "en": "One hand holds a single sealed pack between two fingers, still, in front of the softly blurred product."},
    "nhin_vao_hop": {"y": ('ben_trong',), "ten": "Nhìn từ trên vào hộp đang mở", "vat": "sp", "kieu": "sp", "loai": ("hop",),
                     "en": "Top-down view into the open product showing the sealed packs inside, neatly in place, still."},
    "dong_hop_lai": {"y": ('thiet_ke',), "ten": "Đậy nắp hộp, đặt về giữa set", "vat": "sp", "kieu": "tay", "loai": ("hop",),
                     "en": "Two hands put the lid back on the product and leave it standing in the centre of the set."},
}

# Ánh sáng, máy khi mẻ không chọn mẫu (reel studio PlayAh: sản phẩm là chính, sáng mềm, nét, cắt nhanh).
KHOA_STUDIO = ("Clean studio product commercial look: soft diffused key light, gentle rim light, crisp focus on the product, "
               "smooth calm motion, short fast-paced reel shot.")

# Cảnh có gel đã nằm sẵn (trên tay, trên đĩa, trên mặt set): chỉ được đứng sau một cảnh bóp.
CO_CHAT_SAN = ("keo_soi", "nghieng_dia", "can_chat", "vuot_mu_tay", "thoa_cang_tay", "thoa_cang_chan",
               "cham_ngon", "xa_nuoc_kinh", "lau_khan_trang", "nhin_xuyen_chu", "ep_hai_kinh", "truot_doc", "anh_sang_xuyen",
               "tha_vao_nuoc", "so_giot_nuoc", "lat_up_dia", "keo_soi_que", "lau_lua_sach")
# Cảnh cần gel đã được bóp lên ĐÚNG một chỗ trước đó (không chỉ "có cảnh bóp nào đó").
CAN_BOP_TRUOC = {"nghieng_dia": ("bop_dia", "bop_vong_xoan"), "lat_up_dia": ("bop_dia", "bop_vong_xoan"),
                 "xa_nuoc_kinh": ("bop_len_kinh", "bop_ve_tim"), "so_giot_nuoc": ("bop_canh_giot_nuoc",),
                 "nhin_xuyen_chu": ("bop_len_kinh", "bop_ve_tim", "bop_canh_giot_nuoc"), "ep_hai_kinh": ("bop_len_kinh", "bop_canh_giot_nuoc"),
                 "anh_sang_xuyen": ("bop_len_kinh", "bop_ve_tim", "bop_canh_giot_nuoc", "bop_len_da"),
                 "truot_doc": ("bop_len_kinh", "bop_len_khay_den"), "lau_lua_sach": ("bop_lua_trang",),
                 "thoa_cang_tay": ("bop_cang_tay",), "thoa_cang_chan": ("bop_cang_chan",)}
# Cảnh chỉ để khoe chất. Video có ý chính KHÁC chất gel không được dùng (chỉ giữ đúng một cảnh bóp, phần còn lại minh hoạ ý chính).
CHI_KHOE_CHAT = ("keo_soi", "nghieng_dia", "can_chat", "cham_ngon", "giot_roi", "thoa_cang_tay", "thoa_cang_chan",
                 "nhin_xuyen_chu", "ep_hai_kinh", "truot_doc", "anh_sang_xuyen", "so_giot_nuoc", "lat_up_dia", "keo_soi_que")

# Ngưỡng coi hai đề cương là trùng (xem `trung`).
NGUONG_CHUOI = 0.6      # chuỗi mã cảnh giống nhau từ 60% (dãy con chung dài nhất / độ dài dài hơn)
NGUONG_TAP = 0.8        # cùng bộ cảnh chỉ đổi thứ tự (Jaccard tập mã cảnh)
NGUONG_Y = 0.5          # câu thông điệp giống nhau (Jaccard cụm 3 chữ cái, bỏ dấu)

# Bề mặt nhận gel là mô-típ hình của video. Hai cảnh bóp khác mã nhưng cùng bề mặt vẫn nhìn gần như nhau.
BE_MAT_GEL = {"bop_dau_ngon": "tay", "bop_dia": "dia", "bop_vong_xoan": "dia", "bop_len_da": "da_lanh",
              "bop_len_kinh": "kinh", "bop_len_thia_su": "thia_su", "bop_len_khay_den": "khay_den",
              "bop_cang_tay": "cang_tay", "bop_cang_chan": "cang_chan", "bop_ve_tim": "hinh_ve", "bop_canh_hoa": "canh_hoa",
              "bop_giay_mau": "giay_mau", "bop_canh_giot_nuoc": "kinh_nuoc", "bop_lua_trang": "lua"}


def mo_tip_hinh(dc: Dict) -> str:
    """Bề mặt gel đầu tiên của một video; dùng để so trùng kiểu hình, ngoài chuỗi mã cảnh."""
    return next((BE_MAT_GEL[m] for m in _ma(dc) if m in BE_MAT_GEL), "")


# Chỗ gel được xử lý SAU khi bóp ra (thoa, kéo, nghiêng, lau, rửa). Cùng với BE_MAT_GEL tạo thành mô-típ hình của video: hai video
# cùng thoa trên một chỗ, hay cùng "bóp rồi xoa trên bàn tay", nhìn như nhau dù khác mã cảnh (13/11/2026, owner dặn).
XU_LY_GEL = {"keo_soi": "ban_tay", "cham_ngon": "ban_tay", "vuot_mu_tay": "ban_tay", "thoa_cang_tay": "cang_tay",
             "thoa_cang_chan": "cang_chan", "nghieng_dia": "dia", "lau_khan_trang": "khan", "xa_nuoc_kinh": "kinh",
             "nhin_xuyen_chu": "xuyen_chu", "ep_hai_kinh": "ep_kinh", "truot_doc": "doc", "anh_sang_xuyen": "anh_sang",
             "tha_vao_nuoc": "ly_nuoc", "so_giot_nuoc": "so_nuoc", "lat_up_dia": "up_dia", "keo_soi_que": "que", "lau_lua_sach": "lua"}
TEN_CHO = {"ban_tay": "bàn tay", "tay": "bàn tay", "cang_tay": "cẳng tay", "cang_chan": "cẳng chân", "dia": "đĩa", "khan": "khăn",
           "kinh": "tấm kính", "da_lanh": "đá lạnh", "thia_su": "thìa sứ", "khay_den": "khay đen", "hinh_ve": "hình vẽ trên kính",
           "canh_hoa": "cánh hoa", "giay_mau": "giấy màu", "kinh_nuoc": "kính có giọt nước", "lua": "lụa trắng"}
# Cảnh có cẳng tay / cẳng chân: câu khoá riêng thay cho "HANDS ONLY" (cảnh này cố ý có cẳng tay hoặc cẳng chân).
CANH_CHI_THE = ("bop_cang_tay", "bop_cang_chan", "thoa_cang_tay", "thoa_cang_chan")
KHOA_CHI_THE = ("BODY LIMITS: only the hands and the one forearm or lower leg (below the knee) named in this shot are in frame; no face, "
                "no head, no neck, no torso, no chest, no hips, no thighs, no other people, no mirror, nothing intimate.")


def tren_tay(dc: Dict) -> bool:
    """Video có bóp hoặc xoa gel trên bàn tay (đầu ngón, mu tay, giữa các ngón): lối mòn, mỗi mẻ chỉ một video."""
    return any(BE_MAT_GEL.get(m) == "tay" or XU_LY_GEL.get(m) == "ban_tay" for m in _ma(dc))


def cho_thoa(dc: Dict) -> List[str]:
    """Những chỗ gel được thoa / xử lý trong video, theo thứ tự."""
    return [XU_LY_GEL[m] for m in _ma(dc) if m in XU_LY_GEL]


def _co(d: Dict, *ma: str) -> bool:
    return any(m in ma for m in _ma(d))


# Định dạng UGC của một video (13/11/2026: owner "nó có rất nhiều loại UGC mà"). Mỗi video trong mẻ một định dạng khác nhau, khi còn
# định dạng chưa dùng. `kiem`: code soát hình / cách kể có khớp định dạng không (AI nói unboxing mà không có cảnh mở hộp thì loại).
# Mọi định dạng vẫn trong ngôn ngữ reel studio: chỉ tay (cẳng tay / cẳng chân ở cảnh thoa), sản phẩm là chính.
DINH_DANG: Dict[str, Tuple[str, str, Callable[[Dict], bool], Tuple[str, ...]]] = {
    "mo_hop": ("Đập hộp lần đầu", "mở bao bì ngay trên hình, kể điều thấy được khi mở",
               lambda d: _co(d, "mo_hop_rut_tuyp", "hop_giao_kin", "mo_nap_hop"), ("gel", "hop")),
    "asmr": ("ASMR, gần như không lời", "tiếng thật của thao tác dẫn video (bật nắp, bóp, mở hộp); lời rất ít",
             lambda d: d.get("cach_ke") == "it_loi", ("gel", "hop")),
    "top_3": ("3 lý do", "đúng 3 ý, mỗi ý một cảnh, có đánh số", lambda d: d.get("cach_mo") == "dem_3" or d.get("cach_ke") == "liet_ke", ("gel", "hop")),
    "huong_dan": ("Hướng dẫn từng bước", "chỉ cách dùng: mở, lấy lượng vừa đủ, đóng lại", lambda d: d.get("cach_ke") == "huong_dan", ("gel", "hop")),
    "tra_loi_bl": ("Trả lời bình luận", "mở bằng một câu hỏi của người xem, cả video là câu trả lời", lambda d: d.get("cach_mo") == "binh_luan", ("gel", "hop")),
    "kiem_chung": ("Kiểm chứng lời đồn", "đặt một câu hỏi rồi làm phép thử ngay trên hình; kết quả phải đúng THÔNG TIN SẢN PHẨM",
                   lambda d: _co(d, "xa_nuoc_kinh", "lau_khan_trang", "nghieng_dia", "keo_soi", "thoa_cang_tay", "thoa_cang_chan"), ("gel",)),
    "hieu_lam": ("Hiểu lầm và sự thật", "nêu một hiểu lầm hay gặp, cho xem điều ngược lại", lambda d: d.get("cach_mo") == "lat_nguoc", ("gel", "hop")),
    "pov": ("POV góc nhìn người dùng", "chữ 'POV: ...' ở đầu, máy như mắt người đang cầm sản phẩm",
            lambda d: any(c.get("goc") == "tu_tren" for c in d.get("canh") or []), ("gel", "hop")),
    "trong_tui": ("Trong túi mình có gì", "sản phẩm như một món luôn mang theo", lambda d: _co(d, "bo_vao_tui", "sp_trong_long_tay"), ("gel", "hop")),
    "soi_bao_bi": ("Soi bao bì, đọc thành phần", "đi qua từng chi tiết bao bì hoặc một thành phần có tên",
                   lambda d: _co(d, "can_nap_lat", "dung_tren_nap", "la_nha_dam", "hop_dong_yen", "sp_anh_quet", "nhin_vao_hop"), ("gel", "hop")),
    "thoa_man": ("Oddly satisfying", "chuỗi chuyển động chậm, đã mắt của chất, gần như không lời",
                 lambda d: sum(m in _ma(d) for m in ("bop_vong_xoan", "giot_roi", "nghieng_dia", "keo_soi", "can_chat")) >= 2, ("gel",)),
    "an_tuong_dau": ("Ấn tượng đầu tiên", "cảm nhận thật khi lần đầu cầm, mở, chạm", lambda d: d.get("cach_ke") == "cam_nhan", ("gel", "hop")),
    "hoi_nhanh": ("Hỏi nhanh đáp gọn", "2-3 câu hỏi hay gặp, mỗi cảnh trả lời một câu", lambda d: d.get("cach_ke") == "tra_loi", ("gel", "hop")),
    "truoc_sau": ("Trước - sau", "một khó chịu thường gặp, rồi lúc có sản phẩm; không chê sản phẩm khác", lambda d: d.get("cach_ke") == "so_sanh", ("gel", "hop")),
    "checklist": ("Checklist trước khi chọn", "các điều nên xem khi chọn loại sản phẩm này, sản phẩm đáp ứng điều nào trong THÔNG TIN",
                  lambda d: d.get("cach_mo") in ("dem_3", "chi_tiet") or d.get("cach_ke") in ("liet_ke", "giai_thich"), ("gel", "hop")),
}


def dinh_dang_cho(loai: str) -> Dict[str, Tuple]:
    return {k: v for k, v in DINH_DANG.items() if loai in v[3]}


def canh_cho(loai: str) -> Dict[str, Dict]:
    return {k: v for k, v in CANH.items() if loai in v["loai"]}


# ── So trùng ─────────────────────────────────────────────────────────────────

def _gon(chu: str) -> str:
    chu = unicodedata.normalize("NFD", str(chu or "").lower())
    chu = "".join(c for c in chu if unicodedata.category(c) != "Mn").replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", " ", chu).strip()


def _lcs(a: List[str], b: List[str]) -> int:
    dp = [0] * (len(b) + 1)
    for x in a:
        truoc = 0
        for j, y in enumerate(b, 1):
            cu = dp[j]
            dp[j] = truoc + 1 if x == y else max(dp[j], dp[j - 1])
            truoc = cu
    return dp[-1]


def giong_chuoi(a: List[str], b: List[str]) -> float:
    return _lcs(a, b) / max(len(a), len(b), 1)


def giong_tap(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(len(sa | sb), 1)


def giong_chu(a: str, b: str) -> float:
    def cum(s):
        s = _gon(s).replace(" ", "")
        return {s[i:i + 3] for i in range(max(len(s) - 2, 0))}
    ca, cb = cum(a), cum(b)
    return len(ca & cb) / max(len(ca | cb), 1) if ca and cb else 0.0


def _ma(dc: Dict) -> List[str]:
    return [c["ma"] for c in dc.get("canh") or []]


def trung(a: Dict, b: Dict) -> Tuple[bool, float, List[str]]:
    """(có trùng không, điểm giống 0..1, lý do). So CẤU TRÚC (chuỗi cảnh, cách mở, cách kể) trước, câu chữ sau."""
    ma_a, ma_b = _ma(a), _ma(b)
    chuoi, tap, y = giong_chuoi(ma_a, ma_b), giong_tap(ma_a, ma_b), giong_chu(a.get("thong_diep"), b.get("thong_diep"))
    motip_a, motip_b = mo_tip_hinh(a), mo_tip_hinh(b)
    khac_hinh_gel = a.get("y_chinh") == b.get("y_chinh") == "chat" and motip_a and motip_b and motip_a != motip_b
    ly_do = []
    if chuoi >= NGUONG_CHUOI:
        ly_do.append(f"chuỗi cảnh giống {round(chuoi * 100)}%")
    if tap >= NGUONG_TAP and len(ma_a) > 1:
        ly_do.append(f"cùng bộ cảnh, chỉ đổi thứ tự ({round(tap * 100)}%)")
    if a.get("cach_mo") == b.get("cach_mo") and a.get("cach_ke") == b.get("cach_ke"):
        ly_do.append("cùng cách mở và cùng cách kể")
    if len(ma_a) > 2 and ma_a[:2] == ma_b[:2] and ma_a[-1:] == ma_b[-1:]:
        ly_do.append("cùng 2 cảnh đầu và cùng cảnh cuối")
    # Cùng sản phẩm có thể nói cùng một đặc điểm, miễn cách cho thấy gel trên hình khác thật.
    if y >= NGUONG_Y and not khac_hinh_gel:
        ly_do.append(f"thông điệp giống {round(y * 100)}%")
    if motip_a and motip_a == motip_b:
        ly_do.append(f"cùng kiểu hình gel trên {motip_a}")
    if tren_tay(a) and tren_tay(b):
        ly_do.append("cả hai đều bóp / xoa gel trên bàn tay")
    chung_thoa = [c for c in dict.fromkeys(cho_thoa(a)) if c in cho_thoa(b) and c in ("ban_tay", "cang_tay", "cang_chan")]
    if chung_thoa:
        ly_do.append("cùng thoa gel trên " + ", ".join(TEN_CHO.get(c, c) for c in chung_thoa))
    diem = max(chuoi, tap * 0.9, 0 if khac_hinh_gel else y,
               0.7 if a.get("cach_mo") == b.get("cach_mo") and a.get("cach_ke") == b.get("cach_ke") else 0)
    return bool(ly_do), round(diem, 2), ly_do


def bang_trung(ds: List[Dict]) -> Dict:
    """Điểm giống từng cặp đề cương và cặp gần nhau nhất, để trang hiện."""
    cap = []
    for i in range(len(ds)):
        for j in range(i + 1, len(ds)):
            co, diem, ly_do = trung(ds[i], ds[j])
            cap.append({"a": i, "b": j, "diem": diem, "trung": co, "ly_do": ly_do or _vi_sao_gan(ds[i], ds[j])})
    gan = max(cap, key=lambda x: x["diem"]) if cap else None
    return {"cap": cap, "gan_nhat": gan, "so_cap_trung": sum(1 for c in cap if c["trung"])}


def _vi_sao_gan(a: Dict, b: Dict) -> List[str]:
    """Hai đề cương không trùng thì vẫn nói chúng gần nhau ở đâu (để người xem tự đánh giá)."""
    ra = []
    chung = [m for m in _ma(a) if m in set(_ma(b))]
    if chung:
        ra.append("chung cảnh: " + ", ".join(CANH[m]["ten"] for m in dict.fromkeys(chung) if m in CANH))
    if a.get("y_chinh") and a.get("y_chinh") == b.get("y_chinh"):
        ra.append("cùng ý chính")
    if a.get("dinh_dang") and a.get("dinh_dang") == b.get("dinh_dang"):
        ra.append("cùng định dạng")
    if a.get("cach_mo") == b.get("cach_mo"):
        ra.append("cùng cách mở")
    if a.get("cach_ke") == b.get("cach_ke"):
        ra.append("cùng cách kể")
    return ra


# ── Soát một đề cương ────────────────────────────────────────────────────────

def chuan(tho: Dict, loai: str, so_canh: int, luu_y: str = "", thong_tin: Optional[str] = None) -> Tuple[Optional[Dict], str]:
    """Gọt một đề cương AI trả về về đúng khuôn. Trả (đề cương, "") hoặc (None, lý do loại). `thong_tin` None: không đối chiếu thông tin."""
    import luat_nhan_hang
    if not isinstance(tho, dict):
        return None, "sai khuôn"
    cho_phep = canh_cho(loai)
    canh = []
    for c in tho.get("canh") if isinstance(tho.get("canh"), list) else []:
        c = c if isinstance(c, dict) else {"ma": c}
        ma = str(c.get("ma") or "").strip()
        if ma not in cho_phep:
            return None, f"mã cảnh lạ hoặc không hợp loại sản phẩm: {ma or '(trống)'}"
        goc = str(c.get("goc") or "").strip()
        canh.append({"ma": ma, "goc": goc if goc in GOC_MAY else "ngang_mat", "ta": _cat(c.get("ta"), 160) or cho_phep[ma]["ten"],
                     "y_nghia": _cat(c.get("y_nghia"), 160)})
    if len(canh) != so_canh:
        return None, f"cần đúng {so_canh} cảnh, AI trả {len(canh)}"
    ma = [c["ma"] for c in canh]
    if len(set(ma)) != len(ma):
        return None, "một cảnh bị lặp trong cùng video"
    y = str(tho.get("y_chinh") or "").strip()
    # Owner 13/11: "chủ yếu show off to customer về gel này": video gel NÀO cũng có một khoảnh khắc khoe gel (cảnh bóp thật bằng tay).
    if loai == "gel" and not any(CANH[m].get("bop") for m in ma):
        return None, "video gel thiếu cảnh bóp gel ra (video nào cũng phải khoe gel)"
    # Gel không tự hiện ra (bài học 05/11: gel ra từ thân tuýp, tuýp tự hiện ra): cảnh gel đã nằm sẵn phải đứng sau cảnh bóp.
    bop = next((i for i, m in enumerate(ma) if CANH[m].get("bop")), None)
    co_chat = [i for i, m in enumerate(ma) if m in CO_CHAT_SAN]
    if co_chat and (bop is None or min(co_chat) < bop):
        return None, "có cảnh gel đã nằm sẵn trước cảnh bóp gel ra"
    for m, can_truoc in CAN_BOP_TRUOC.items():
        if m in ma and not any(b in ma[:ma.index(m)] for b in can_truoc):
            return None, f"{CANH[m]['ten']} mà chưa bóp gel lên đúng vị trí trước đó"
    duoc = y_chinh_cho(loai, thong_tin)
    if y not in duoc:
        return None, f"ý chính lạ hoặc thông tin sản phẩm không nhắc tới: {y or '(trống)'}"
    if thong_tin is not None:
        g = _gon(thong_tin)
        thieu = [m for m in ma if CANH[m].get("can") and not re.search(CANH[m]["can"], g)]
        if thieu:
            return None, f"cảnh cần một điều thông tin sản phẩm không nhắc: {CANH[thieu[0]]['ten']}"
    so_bop = sum(1 for m in ma if CANH[m].get("bop"))
    if y != "chat":
        so_khoe = sum(m in CHI_KHOE_CHAT for m in ma)
        if so_bop > 1 or so_khoe > 1 or (so_bop + so_khoe) * 2 > len(ma) or ("vuot_mu_tay" in ma and y != "diu_nhe"):
            return None, f"ý chính là {Y_CHINH[y][0]} mà video lại chủ yếu khoe chất gel (một cảnh bóp, tối đa một cảnh khoe)"
        if any(y in (CANH[m].get("y") or ()) for m in CANH) and not any(y in (CANH[m].get("y") or ()) for m in ma):
            return None, f"không cảnh nào minh hoạ ý chính {Y_CHINH[y][0]}"
    set_ = str(tho.get("set") or "").strip()
    cach_mo, cach_ke = str(tho.get("cach_mo") or "").strip(), str(tho.get("cach_ke") or "").strip()
    dd = str(tho.get("dinh_dang") or "").strip()
    if dd not in dinh_dang_cho(loai):
        return None, f"định dạng UGC lạ hoặc không hợp loại sản phẩm: {dd or '(trống)'}"
    if not DINH_DANG[dd][2]({"canh": canh, "cach_mo": cach_mo, "cach_ke": cach_ke}):
        return None, f"định dạng {DINH_DANG[dd][0]} mà hình hoặc cách kể không khớp"
    if set_ not in BO_SET:
        return None, f"set lạ: {set_ or '(trống)'}"
    if cach_mo not in CACH_MO or cach_ke not in CACH_KE:
        return None, "cách mở hoặc cách kể không có trong danh sách"
    dc = {"ten": _cat(tho.get("ten"), 70), "thong_diep": _cat(tho.get("thong_diep"), 220), "moc": _cat(tho.get("moc"), 60),
          "cach_mo": cach_mo, "cach_ke": cach_ke, "set": set_, "y_chinh": y, "dinh_dang": dd, "canh": canh, "khac_gi": _cat(tho.get("khac_gi"), 200)}
    if not dc["ten"] or not dc["thong_diep"] or not dc["moc"]:
        return None, "thiếu tên, thông điệp hoặc câu móc"
    vi_pham = luat_nhan_hang.soat("\n".join([dc["ten"], dc["thong_diep"], dc["moc"]] + [c["ta"] for c in canh]), luu_y)
    if vi_pham:
        return None, "sai luật nói: " + "; ".join(vi_pham[:2])
    return dc, ""


def _cat(chu, dai: int) -> str:
    return re.sub(r"\s+", " ", str(chu or "")).strip()[:dai]


# ── Hỏi AI cho một sản phẩm ──────────────────────────────────────────────────

HE = ("Bạn là đạo diễn quảng cáo ngắn cho một nhãn hàng gel bôi trơn và bao cao su ở Việt Nam. Bạn lên đề cương cho nhiều video "
      "reel studio KHÁC NHAU THẬT SỰ của cùng một sản phẩm. Chỉ trả JSON.")


def _bang_ma(d: Dict) -> str:
    return "\n".join(f"- {k}: {v[0]}" for k, v in d.items())


MUC_TIEU_GEL = (
    "MỤC TIÊU CHÍNH: khoe chính chất gel cho khách thấy, mỗi video một cách khoe KHÁC: độ trong (nhìn xuyên thấy chữ, màu giấy giữ "
    "nguyên), độ bóng (ánh sáng xuyên qua, giọt như sương trên cánh hoa), độ sánh (giữ dáng cạnh giọt nước, lật đĩa vẫn bám, kéo sợi), "
    "độ trượt (trượt xuống mặt dốc, ép mỏng giữa hai tấm kính), gốc nước (tan trong ly nước, lau sạch trên lụa) và các bề mặt khác "
    "nhau (đĩa, kính, đá lạnh, thìa sứ, khay đen, cánh hoa, cẳng tay, cẳng chân). Chỉ cho thấy điều nhìn được, không hứa công dụng. "
    "Video nào cũng có ít nhất một cảnh bóp gel ra bằng tay; cảnh gel đã nằm sẵn chỉ được đứng SAU cảnh bóp lên đúng chỗ đó.")


def loi_hoi(sp: Dict, so_video: int, so_canh: int, phong_cach: str, goi_y: List[Dict], da_co: List[Dict],
            tu_choi: List[str]) -> str:
    loai = sp["loai"]
    g = _gon(sp.get("thong_tin") or "")
    canh = "\n".join(f"- {k}: {v['ten']}" + (" (cảnh bóp)" if v.get("bop") else "") + (" [chỉ sản phẩm, không tay]" if v["kieu"] == "sp" else "")
                     + (f" [minh hoạ: {', '.join(Y_CHINH[x][0] for x in v['y'])}]" if v.get("y") else "")
                     for k, v in canh_cho(loai).items() if not v.get("can") or re.search(v["can"], g))
    y_duoc = y_chinh_cho(loai, sp.get("thong_tin") or "")
    da_dung = {d.get("y_chinh") for d in da_co}
    y_chu = "\n".join(f"- {k}: {v[0]}: {v[1]}" + (" (ĐÃ DÙNG, chọn ý khác)" if k in da_dung else "") for k, v in y_duoc.items())
    dd_dung = {d.get("dinh_dang") for d in da_co}
    dd_chu = "\n".join(f"- {k}: {v[0]}: {v[1]}" + (" (ĐÃ DÙNG)" if k in dd_dung else "") for k, v in dinh_dang_cho(loai).items())
    da_co_chu = "\n".join(f"- \"{d['ten']}\": định dạng {d.get('dinh_dang')}, ý chính {d.get('y_chinh')}, cách khoe gel "
                          f"{'/'.join(TEN_CHO.get(x, x) for x in [mo_tip_hinh(d)] + cho_thoa(d) if x) or '-'}, mở {d['cach_mo']}, kể {d['cach_ke']}, set {d['set']}, cảnh {' → '.join(_ma(d))}"
                          for d in da_co) or "(chưa có)"
    goi_y_chu = "\n".join(f"- {c['ten']}: {c.get('mo_ta') or ''}" for c in goi_y) or "(không có)"
    return f"""SẢN PHẨM: {sp['ten']} (loại: {'gel, bóp ra được' if loai == 'gel' else 'hộp, bên trong là các gói'})
THÔNG TIN SẢN PHẨM (nguồn DUY NHẤT cho mọi ý về sản phẩm; trống thì không được nêu đặc điểm nào, chỉ nói về việc cầm, mở, nhìn):
{(sp.get('thong_tin') or '').strip() or '(trống)'}

{sp.get('luat') or ''}

NGÔN NGỮ QUAY (bắt buộc, mọi video): reel studio, sản phẩm là chính trong mọi cảnh, cắt nhanh; chỉ có bàn tay, cẳng tay
hoặc cẳng chân khi mã cảnh yêu cầu (không mặt, không thân người, không vùng nhạy cảm),
mỗi video MỘT set studio. Không cảnh đời sống (phòng tắm, xe, bàn trang điểm, phòng ngủ). Không xoay sản phẩm, không đẩy máy sát nhãn.
{MUC_TIEU_GEL if loai == 'gel' else ''}
Đổi bề mặt nhận gel giữa các video trong cùng mẻ (tay, cẳng tay, cẳng chân, đĩa, kính, đá lạnh, thìa sứ, khay đen);
không mặc định lặp lại bóp ra đầu ngón rồi xoa trên mu bàn tay. Cảnh thoa trên cẳng tay/cẳng chân chỉ cho thấy vệt gel thật,
không được suy diễn công dụng hay kết quả trên da.

PHONG CÁCH QUAY HỌC TỪ VIDEO MẪU (chỉ mượn ánh sáng, góc máy, nhịp; KHÔNG chép trình tự cảnh của mẫu):
{phong_cach or '(không có mẫu)'}

HƯỚNG GỢI Ý TỪ CONCEPT NGƯỜI DÙNG CHỌN (được dùng làm cảm hứng, không bắt buộc):
{goi_y_chu}

Ý CHÍNH (mỗi video MỘT ý chính, các video KHÁC ý chính nhau; chỉ có các ý dưới đây vì thông tin sản phẩm có nhắc):
{y_chu}
"Chất gel" chỉ là MỘT trong các ý chính. Video có ý chính khác vẫn khoe gel ở một cảnh bóp (và tối đa một cảnh khoe chất),
các cảnh còn lại minh hoạ ý chính của video đó.

ĐỊNH DẠNG UGC (mỗi video MỘT định dạng, các video KHÁC định dạng nhau; hình và cách kể phải khớp định dạng):
{dd_chu}

MÃ ĐƯỢC DÙNG (chỉ dùng đúng các mã này):
Set studio:
{_bang_ma(BO_SET)}
Góc máy:
{_bang_ma(GOC_MAY)}
Cách mở:
{chr(10).join(f'- {k}: {v[0]}: {v[1]}' for k, v in CACH_MO.items())}
Cách kể:
{chr(10).join(f'- {k}: {v[0]}: {v[1]}' for k, v in CACH_KE.items())}
Cảnh:
{canh}

ĐÃ CÓ (không được trùng với các đề cương này):
{da_co_chu}
{('CÁC ĐỀ CƯƠNG LƯỢT TRƯỚC BỊ LOẠI VÌ: ' + '; '.join(tu_choi[-8:])) if tu_choi else ''}

YÊU CẦU: viết {so_video} đề cương, mỗi đề cương đúng {so_canh} cảnh (mỗi cảnh là một clip). Các đề cương phải khác nhau THẬT SỰ:
- khác ĐỊNH DẠNG (mã dinh_dang), khác Ý CHÍNH (mã y_chinh), khác thông điệp, khác cách mở, khác cách kể, khác chuỗi hành động;
- khác CÁCH KHOE GEL: không hai video nào bóp lên cùng một bề mặt, không hai video nào thoa trên cùng một chỗ, nhiều nhất MỘT video
  bóp / xoa gel trên bàn tay (đầu ngón, mu tay, kéo sợi giữa ngón);
- không hai đề cương nào có chuỗi cảnh giống nhau quá nửa, không chỉ đổi thứ tự cùng một bộ cảnh;
- không hai đề cương nào cùng cặp cách mở + cách kể; mỗi đề cương một set khác nhau nếu đủ set;
- đổi câu móc hay đổi phông nền KHÔNG được tính là khác.
Mỗi cảnh phải là hệ quả của cảnh trước (vì sao cảnh này đứng ở đây).

Trả về: {{"de_cuong": [{{"ten": "tên ngắn tiếng Việt", "thong_diep": "một câu: người xem hiểu gì", "moc": "câu móc 2-6 chữ hiện chữ to ở giây đầu",
"dinh_dang": "mã định dạng UGC", "y_chinh": "mã ý chính", "cach_mo": "mã", "cach_ke": "mã", "set": "mã", "khac_gi": "khác các đề cương còn lại ở đâu",
"canh": [{{"ma": "mã cảnh", "goc": "mã góc máy", "ta": "tả cảnh bằng tiếng Việt, ngắn", "y_nghia": "vì sao cảnh này ở đây"}}]}}]}}"""


def len_cho_san_pham(sp: Dict, so_video: int, so_canh: int, phong_cach: str = "", goi_y: Optional[List[Dict]] = None,
                     hoi: Optional[Callable] = None, da_co: Optional[List[Dict]] = None) -> Dict:
    """
    Lập `so_video` đề cương khác nhau cho một sản phẩm. `sp`: {"ten", "loai", "thong_tin", "luu_y", "luat"}. `da_co`: đề cương đã
    giữ (khi nghĩ lại một thẻ). Trả {"de_cuong", "dat", "yeu_cau", "tu_choi", "so_luot"}; không đủ thì `dat` < `yeu_cau`.
    """
    hoi = hoi or (lambda he, de: pc.hoi_ai(he, de, max_tokens=8000))
    so_video = max(1, min(SO_VIDEO_TOI_DA_MOT_SP, int(so_video)))
    giu: List[Dict] = list(da_co or [])
    moi: List[Dict] = []
    tu_choi: List[str] = []
    so_luot = 0
    for _ in range(SO_LUOT_AI):
        can = so_video - len(moi)
        if can <= 0:
            break
        so_luot += 1
        try:
            tra = hoi(HE, loi_hoi(sp, can + DU_THEM, so_canh, phong_cach, goi_y or [], giu, tu_choi))
        except Exception as exc:  # noqa: BLE001 - lượt hỏng thì thử lượt sau, hết lượt thì báo số đạt
            tu_choi.append(f"AI lỗi: {str(exc)[:120]}")
            continue
        for tho in (tra or {}).get("de_cuong") or []:
            if len(moi) >= so_video:
                break
            dc, ly_do = chuan(tho, sp["loai"], so_canh, sp.get("luu_y") or "", sp.get("thong_tin") or "")
            ten = (tho or {}).get("ten") if isinstance(tho, dict) else ""
            if not dc:
                tu_choi.append(f"\"{ten or '?'}\": {ly_do}")
                continue
            y_con = set(y_chinh_cho(sp["loai"], sp.get("thong_tin") or "")) - {g.get("y_chinh") for g in giu}
            dd_con = set(dinh_dang_cho(sp["loai"])) - {g.get("dinh_dang") for g in giu}
            cung_dd = next((g for g in giu if g.get("dinh_dang") == dc["dinh_dang"]), None)
            if cung_dd and dd_con:
                tu_choi.append(f"\"{dc['ten']}\" cùng định dạng {DINH_DANG[dc['dinh_dang']][0]} với \"{cung_dd['ten']}\"")
                continue
            cung_y = next((g for g in giu if g.get("y_chinh") == dc["y_chinh"]), None)
            if cung_y and y_con:
                tu_choi.append(f"\"{dc['ten']}\" cùng ý chính {Y_CHINH[dc['y_chinh']][0]} với \"{cung_y['ten']}\" trong khi còn ý chưa dùng")
                continue
            dung = next(((g, trung(dc, g)[2]) for g in giu if trung(dc, g)[0]), None)
            if dung:
                tu_choi.append(f"\"{dc['ten']}\" trùng \"{dung[0]['ten']}\": {', '.join(dung[1])}")
                continue
            giu.append(dc)
            moi.append(dc)
    _doi_set(moi, giu[:len(giu) - len(moi)])
    return {"de_cuong": moi, "dat": len(moi), "yeu_cau": so_video, "tu_choi": tu_choi, "so_luot": so_luot}


def _doi_set(moi: List[Dict], cu: List[Dict]) -> None:
    """Hai video cùng set studio thì đổi set của video sau sang set chưa dùng (set chỉ là phông nền, đổi không đụng tới ý)."""
    dung = [d["set"] for d in cu]
    for d in moi:
        if d["set"] in dung:
            con = [s for s in BO_SET if s not in dung]
            if con:
                d["set_ai"] = d["set"]
                d["set"] = con[0]
        dung.append(d["set"])


# ── Đề cương → video Review ──────────────────────────────────────────────────

def lenh_canh(dc: Dict, canh: Dict, khoa_mau: str) -> Tuple[str, str]:
    """(việc của cảnh, kiểu "tay" | "sp") cho `soi_video.ghep_lenh`. Set ghi đè bối cảnh của mẫu; mẫu chỉ cho phong cách."""
    m = CANH[canh["ma"]]
    set_en = BO_SET.get(dc.get("set"), BO_SET["sage_tham_long"])[1]
    goc = GOC_MAY.get(canh.get("goc"), GOC_MAY["ngang_mat"])[1]
    gioi_han = ("One box and exactly one tube: the tube starts inside the closed box and appears only when the hands pull it out. "
                "Match each object to its own separate reference image; do not duplicate either object."
                if canh["ma"] == "mo_hop_rut_tuyp" else
                "Keep the product and the glossy content visible together; show only the specified limb, hand and product, no face, torso or intimate body parts. Do not imply a skin benefit."
                if canh["ma"] in ("bop_cang_tay", "bop_cang_chan", "thoa_cang_tay", "thoa_cang_chan") else
                "The product is the hero, large in the frame, one single product only.")
    viec = f"Studio product reel shot. Set: {set_en}. Camera: {goc}. Action: {m['en']} {gioi_han}"
    if khoa_mau:
        viec += f" Lighting and camera feel borrowed from a reference ad (ignore its location and props): {khoa_mau}"
    else:
        viec += " " + KHOA_STUDIO
    return viec, m["kieu"]


def ghep_lenh_canh(dc: Dict, canh: Dict, khoa_mau: str) -> str:
    """Khóa quay cho đề cương; không ép mọi cảnh đứng thẳng hay máy di chuyển như khóa của video mẫu cũ."""
    import soi_video
    viec, kieu = lenh_canh(dc, canh, khoa_mau)
    anh = ("Each object matches its own attached reference image: same shape, colours, logo and printed text."
           if canh["ma"] == "mo_hop_rut_tuyp" else soi_video.KHOA_SP)
    tay = (KHOA_CHI_THE if canh["ma"] in CANH_CHI_THE else soi_video.KHOA_TAY if kieu == "tay" else
           "PRODUCT ONLY: no hands, arms, people or body parts in frame. Follow the camera and product position specified for this shot.")
    return " ".join(("Vertical 9:16 video.", viec, anh, tay, soi_video.KHOA_CHU))


def khung_loi(dc: Dict) -> str:
    """Khung lời cho `lam_hang_loat.viet_gioi_thieu`: lời phải khớp từng cảnh của đề cương."""
    canh = "; ".join(f"cảnh {i + 1}: {c['ta']}" for i, c in enumerate(dc["canh"]))
    y = Y_CHINH.get(dc.get("y_chinh"))
    dd = DINH_DANG.get(dc.get("dinh_dang"))
    return ((f"Định dạng UGC: {dd[0]} ({dd[1]}). " if dd else "") +
            (f"Ý chính của video: {y[0]} ({y[1]}); lời chỉ xoay quanh ý này. " if y else "") +
            f"Thông điệp của video: {dc['thong_diep']}. Cách mở: {CACH_MO[dc['cach_mo']][1]}. Cách kể: {CACH_KE[dc['cach_ke']][1]}. "
            f"Lời đi đúng thứ tự hình: {canh}. Chỉ nêu đặc điểm có trong THÔNG TIN SẢN PHẨM.")


def tom_tat(dc: Dict) -> Dict:
    """Bản để trang hiện: thêm tên tiếng Việt của các mã."""
    return {**dc, "ten_y": Y_CHINH.get(dc.get("y_chinh"), ("",))[0], "ten_dd": DINH_DANG.get(dc.get("dinh_dang"), ("",))[0],
            "ten_mo": CACH_MO.get(dc.get("cach_mo"), ("",))[0], "ten_ke": CACH_KE.get(dc.get("cach_ke"), ("",))[0],
            "ten_set": BO_SET.get(dc.get("set"), ("",))[0],
            "canh": [{**c, "ten": CANH.get(c["ma"], {}).get("ten", c["ma"]), "kieu": CANH.get(c["ma"], {}).get("kieu", "tay"),
                      "bop": bool(CANH.get(c["ma"], {}).get("bop")), "ten_goc": GOC_MAY.get(c.get("goc"), ("",))[0]} for c in dc["canh"]]}


def sua_chu(dc: Dict, moi: Dict, luu_y: str = "") -> Dict:
    """Người dùng sửa tên, thông điệp, câu móc của một đề cương. Chữ mới vẫn phải qua luật nói."""
    import luat_nhan_hang
    ra = dict(dc)
    for k, dai in (("ten", 70), ("thong_diep", 220), ("moc", 60)):
        if k in moi:
            ra[k] = _cat(moi[k], dai)
            if not ra[k]:
                raise ValueError("Tên, thông điệp và câu móc không được để trống.")
    vi_pham = luat_nhan_hang.soat("\n".join([ra["ten"], ra["thong_diep"], ra["moc"]]), luu_y)
    if vi_pham:
        raise ValueError("Chữ mới sai luật nói: " + "; ".join(vi_pham[:3]))
    ra["sua_tay"] = time.time()
    return ra
