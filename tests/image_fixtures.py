from __future__ import annotations

from io import BytesIO

from PIL import Image


def png_bytes(
    *,
    color: str | tuple[int, int, int] = "white",
    size: tuple[int, int] = (4, 3),
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def jpeg_bytes(
    *,
    color: str | tuple[int, int, int] = "white",
    size: tuple[int, int] = (4, 3),
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()
