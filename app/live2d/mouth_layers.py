from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image


def clean_mouth_from_face(face: Image, mouth_mask: Image) -> Image:
    """用嘴唇上下的肤色补底脸，保持可动嘴唇的原始纹理和角色透明轮廓。"""
    from PIL import ImageFilter

    bounds = mouth_mask.getbbox()
    if bounds is None:
        return face
    left, top, right, bottom = bounds
    margin = max(2, round((right - left) * 0.10))
    search = max(4, bottom - top)
    region = (
        max(0, left - margin),
        max(0, top - search - margin),
        min(face.width, right + margin),
        min(face.height, bottom + search + margin),
    )
    removed = mouth_mask.crop(region).filter(ImageFilter.MaxFilter(2 * margin + 1))
    source = face.crop(region)
    patch = source.copy()
    pixels, result, mask = source.load(), patch.load(), removed.load()
    for x in range(source.width):
        covered = [y for y in range(source.height) if mask[x, y]]
        if not covered:
            continue
        above = next(
            (y for y in range(covered[0] - 1, -1, -1) if pixels[x, y][3] >= 192),
            None,
        )
        below = next(
            (
                y
                for y in range(covered[-1] + 1, source.height)
                if pixels[x, y][3] >= 192
            ),
            None,
        )
        if above is None and below is None:
            continue
        if above is None:
            above = below
        if below is None:
            below = above
        upper, lower = pixels[x, above], pixels[x, below]
        for y in covered:
            if pixels[x, y][3] == 0:
                continue
            weight = (y - above) / (below - above) if above != below else 0
            rgb = tuple(
                round(a + (b - a) * weight)
                for a, b in zip(upper[:3], lower[:3], strict=True)
            )
            result[x, y] = (*rgb, pixels[x, y][3])
    cleaned = face.copy()
    cleaned.paste(patch, region[:2])
    return cleaned


def paint_open_mouth(lips: Image, face_mask: Image) -> Image | None:
    """用原嘴唇的宽度、斜率和颜色绘制随开合出现的口腔层。"""
    from math import atan2, degrees, pi, sin

    from PIL import Image as PillowImage
    from PIL import ImageChops, ImageDraw, ImageStat

    alpha = lips.getchannel("A")
    if (bounds := alpha.getbbox()) is None:
        return None
    left, top, right, bottom = bounds
    width = right - left
    if width < 8 or bottom - top < 2:
        return None

    visible = [
        (x, y)
        for y in range(top, bottom)
        for x in range(left, right)
        if alpha.getpixel((x, y)) >= 128
    ]
    if not visible:
        return None
    mid_x = sum(x for x, _ in visible) / len(visible)
    mid_y = sum(y for _, y in visible) / len(visible)
    cross = sum((x - mid_x) * (y - mid_y) for x, y in visible)
    variance_x = sum((x - mid_x) ** 2 for x, _ in visible)
    variance_y = sum((y - mid_y) ** 2 for _, y in visible)
    angle = max(
        -32.0, min(32.0, degrees(atan2(2 * cross, variance_x - variance_y)) / 2)
    )
    lip_color = ImageStat.Stat(lips.crop(bounds), alpha.crop(bounds)).mean[:3]

    # 保留唇缘、牙齿和舌头，开口时可辨认上下唇而非露出一块深色。
    supersample = 4
    size = width * supersample
    surface = PillowImage.new("RGBA", (size, size))
    draw = ImageDraw.Draw(surface)

    def lens(inset: float, rise: float, drop: float) -> list[tuple[float, float]]:
        upper, lower = [], []
        for step in range(41):
            fraction = step / 40
            taper = sin(pi * fraction) ** 0.85
            x = size * (inset + (1 - 2 * inset) * fraction)
            upper.append((x, size * (0.47 - rise * taper)))
            lower.append((x, size * (0.47 + drop * taper)))
        return upper + lower[::-1]

    lip = tuple(round(value * 0.86) for value in lip_color)
    shadow = tuple(round(value * 0.29) for value in lip_color)
    draw.polygon(lens(0.02, 0.17, 0.25), fill=(*lip, 255))
    interior = lens(0.055, 0.13, 0.185)
    draw.polygon(interior, fill=(*shadow, 255))
    mask = PillowImage.new("L", surface.size)
    ImageDraw.Draw(mask).polygon(interior, fill=255)
    features = PillowImage.new("RGBA", surface.size)
    detail = ImageDraw.Draw(features)
    detail.rounded_rectangle(
        (0.13 * size, 0.31 * size, 0.87 * size, 0.44 * size),
        radius=0.035 * size,
        fill=(232, 216, 211, 255),
    )
    tongue = tuple(
        round(value * 0.80 + warm)
        for value, warm in zip(lip_color, (20, 4, 9), strict=True)
    )
    detail.ellipse(
        (0.28 * size, 0.56 * size, 0.72 * size, 0.74 * size), fill=(*tongue, 255)
    )
    features.putalpha(ImageChops.multiply(features.getchannel("A"), mask))
    surface.alpha_composite(features)
    patch = surface.rotate(-angle, resample=PillowImage.Resampling.BICUBIC).resize(
        (width, width), resample=PillowImage.Resampling.LANCZOS
    )
    output = PillowImage.new("RGBA", lips.size)
    output.paste(patch, (left, round(mid_y - width / 2)))
    output.putalpha(ImageChops.multiply(output.getchannel("A"), face_mask))
    return output if output.getbbox() else None
