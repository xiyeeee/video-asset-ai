import pytest

from app.live2d.mouth_layers import clean_mouth_from_face

image = pytest.importorskip("PIL.Image")
image_draw = pytest.importorskip("PIL.ImageDraw")


def test_face_backing_removes_baked_lips_and_preserves_transparency() -> None:
    face = image.new("RGBA", (40, 30))
    image_draw.Draw(face).rectangle((3, 4, 34, 25), fill=(195, 142, 135, 255))
    lips = image.new("L", face.size)
    image_draw.Draw(lips).line((11, 12, 25, 16), fill=255, width=3)
    face.paste((85, 25, 32, 255), (0, 0, *face.size), lips)
    before = face.tobytes()

    cleaned = clean_mouth_from_face(face, lips)

    assert cleaned.getpixel((18, 14)) == (195, 142, 135, 255)
    assert cleaned.getchannel("A").tobytes() == face.getchannel("A").tobytes()
    assert cleaned.getpixel((7, 8)) == face.getpixel((7, 8))
    assert face.tobytes() == before


def test_open_mouth_has_distinct_teeth_and_cavity_inside_face() -> None:
    from app.live2d.mouth_layers import paint_open_mouth

    lips = image.new("RGBA", (100, 80))
    image_draw.Draw(lips).line((31, 30, 69, 38), fill=(161, 89, 96, 255), width=4)
    face_mask = image.new("L", lips.size)
    image_draw.Draw(face_mask).rectangle((20, 12, 70, 63), fill=255)
    mouth = paint_open_mouth(lips, face_mask)

    assert mouth is not None
    assert mouth.getbbox()[2] <= 71
    pixels = mouth.load()
    opaque = [
        pixels[x, y]
        for y in range(mouth.height)
        for x in range(mouth.width)
        if pixels[x, y][3] >= 240
    ]
    assert any(red > 215 and green > 190 for red, green, _, _ in opaque)
    assert any(red < 60 and green < 50 for red, green, _, _ in opaque)
