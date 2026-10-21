"""PNG-этикетка для мобильных термопринтеров (Niimbot, Phomemo): 1-битная картинка
точно под размер ленты при 203 dpi — открывается в приложении принтера через «Поделиться»."""
import io
import os

import qrcode
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")


def _font(px):
    return ImageFont.truetype(BOLD, max(8, int(px)))


def _fit(draw, text, max_w, px):
    """Шрифт поменьше, пока строка не влезет; если и самый мелкий не влез — обрезаем с «…»."""
    while px > 9 and draw.textlength(text, font=_font(px)) > max_w:
        px -= 1
    font = _font(px)
    if draw.textlength(text, font=font) > max_w:
        while text and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        text += "…"
    return text, font


def render(url, lines, width_mm=40, height_mm=30, dpi=203):
    """lines: [(текст, доля высоты под шрифт), …] сверху вниз; QR слева во всю высоту."""
    dots = dpi / 25.4
    w, h = round(width_mm * dots), round(height_mm * dots)
    pad = max(4, round(1.2 * dots))
    img = Image.new("1", (w, h), 1)
    draw = ImageDraw.Draw(img)

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    side = h - 2 * pad
    module = side // (len(matrix) + 2)  # целое число точек на модуль — без размытия
    qr_px = module * (len(matrix) + 2)
    ox, oy = pad, (h - qr_px) // 2
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if on:
                x0, y0 = ox + (x + 1) * module, oy + (y + 1) * module
                draw.rectangle([x0, y0, x0 + module - 1, y0 + module - 1], fill=0)

    tx = ox + qr_px + pad
    max_w = w - tx - pad
    y = pad
    for text, share in lines:
        if not text or max_w < 20:
            continue
        line, font = _fit(draw, text, max_w, h * share)
        box = draw.textbbox((0, 0), line, font=font)
        if y + (box[3] - box[1]) > h - pad:
            break
        draw.text((tx, y - box[1]), line, font=font, fill=0)
        y += (box[3] - box[1]) + max(2, round(h * 0.035))
    out = io.BytesIO()
    img.save(out, "PNG", dpi=(dpi, dpi), optimize=True)
    return out.getvalue()
