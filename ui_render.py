"""Rendering antialiased delle barre e delle scrollbar.

Le forme sono disegnate con Pillow a 4x e ridotte con LANCZOS: i bordi
arrotondati e i gradienti risultano puliti, senza scalettature.
"""

import math

from PIL import Image, ImageDraw

SCALE = 4


def _vertical_gradient(size, top, bottom):
    width, height = size
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    last = max(1, height - 1)
    for y in range(height):
        ratio = y / last
        color = tuple(
            int(top[index] + (bottom[index] - top[index]) * ratio)
            for index in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    return image


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=max(0, radius), fill=255)
    return mask


def _blend(first, second, ratio):
    ratio = max(0.0, min(1.0, ratio))
    return tuple(
        int(first[index] + (second[index] - first[index]) * ratio)
        for index in range(3))


def pill_thumb(width, height, hover=False):
    """Thumb arrotondato della scrollbar, con gradiente verticale."""
    width = max(2, int(width))
    height = max(2, int(height))
    big = (width * SCALE, height * SCALE)
    top = (176, 176, 176) if hover else (142, 142, 142)
    bottom = (118, 118, 118) if hover else (92, 92, 92)
    gradient = _vertical_gradient(big, top, bottom)
    mask = _rounded_mask(big, big[0] // 2)
    image = Image.new("RGBA", big, (0, 0, 0, 0))
    image.paste(gradient, (0, 0), mask)

    # Sottile riflesso superiore per dare volume.
    highlight = Image.new("L", big, 0)
    ImageDraw.Draw(highlight).rounded_rectangle(
        (SCALE, SCALE, big[0] - SCALE - 1, big[1] // 2),
        radius=max(1, (big[0] - SCALE) // 2), fill=46)
    image.paste(Image.new("RGBA", big, (255, 255, 255, 255)), (0, 0),
                Image.composite(highlight, Image.new("L", big, 0), mask))
    return image.resize((width, height), Image.LANCZOS)


def progress_bar(width, height, percent, mode="downloading", phase=0.0):
    """Barra di avanzamento: traccia grigio chiaro + riempimento rosso."""
    width = max(4, int(width))
    height = max(4, int(height))
    big = (width * SCALE, height * SCALE)
    image = Image.new("RGBA", big, (0, 0, 0, 0))
    radius = big[1] // 2

    # Traccia grigio chiaro con bordo tenue.
    track = _vertical_gradient(big, (238, 238, 238), (205, 205, 205))
    track_mask = _rounded_mask(big, radius)
    image.paste(track, (0, 0), track_mask)
    border = Image.new("L", big, 0)
    ImageDraw.Draw(border).rounded_rectangle(
        (0, 0, big[0] - 1, big[1] - 1), radius=radius,
        outline=255, width=max(1, SCALE // 2))
    image.paste(Image.new("RGBA", big, (120, 120, 120, 70)), (0, 0), border)

    percent = max(0.0, min(100.0, float(percent)))
    if percent <= 0:
        return image.resize((width, height), Image.LANCZOS)

    relative = percent / 100.0
    fill_width = max(1, int(round(big[0] * relative)))
    fill_width = min(fill_width, big[0])
    fill_radius = min(radius, fill_width // 2)

    if mode == "completed":
        top, bottom = (233, 38, 32), (166, 0, 0)
    elif mode == "paused":
        top, bottom = (148, 44, 38), (102, 10, 10)
    elif mode == "downloading":
        pulse = 0.5 + 0.5 * math.sin(phase)
        top = _blend((196, 20, 12), (255, 92, 66), pulse)
        bottom = _blend((124, 0, 0), (188, 0, 0), pulse)
    else:
        top, bottom = (206, 28, 20), (148, 0, 0)

    fill = _vertical_gradient((fill_width, big[1]), top, bottom)
    fill_mask = _rounded_mask((fill_width, big[1]), fill_radius)
    image.paste(fill, (0, 0), fill_mask)

    # Riflesso interno sul bordo superiore del riempimento.
    gloss = Image.new("L", (fill_width, big[1]), 0)
    ImageDraw.Draw(gloss).rounded_rectangle(
        (SCALE, SCALE, fill_width - SCALE - 1, big[1] // 2),
        radius=max(1, fill_radius - SCALE), fill=52)
    image.paste(Image.new("RGBA", (fill_width, big[1]), (255, 255, 255, 255)),
                (0, 0), Image.composite(gloss, Image.new("L", (fill_width, big[1]), 0),
                                        fill_mask))

    # Banda chiara in movimento, visibile solo durante il download.
    if mode == "downloading" and fill_width > 24 * SCALE and percent < 100:
        band_width = min(30 * SCALE, fill_width - 2 * SCALE)
        band = Image.new("L", (band_width, big[1]), 0)
        band_draw = ImageDraw.Draw(band)
        for x in range(band_width):
            alpha = int(96 * (math.sin(math.pi * x / max(1, band_width - 1)) ** 1.4))
            band_draw.line([(x, 0), (x, big[1])], fill=alpha)
        span = max(1, fill_width - band_width)
        position = 0.5 + 0.5 * math.sin(phase * 0.9)
        offset = int(span * position)
        layer = Image.new("L", big, 0)
        layer.paste(band, (offset, 0))
        layer = Image.composite(layer, Image.new("L", big, 0), track_mask)
        image.paste(Image.new("RGBA", big, (255, 255, 255, 255)), (0, 0), layer)

    return image.resize((width, height), Image.LANCZOS)
