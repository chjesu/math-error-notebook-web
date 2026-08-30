"""Decode and re-encode user images before they cross the storage boundary."""

from __future__ import annotations

from io import BytesIO
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


_IMAGE_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
}


def normalize_image_upload(
    content: bytes,
    media_type: str,
    *,
    max_dimension: int = 10_000,
    max_pixels: int = 40_000_000,
) -> bytes:
    """Return orientation-correct pixels without user-controlled metadata."""
    expected_format = _IMAGE_FORMATS.get(media_type)
    if expected_format is None:
        return content
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                if source.format != expected_format:
                    raise ValueError("image content does not match its media type")
                width, height = source.size
                if (
                    width < 1
                    or height < 1
                    or width > max_dimension
                    or height > max_dimension
                    or width * height > max_pixels
                ):
                    raise ValueError("image dimensions exceed the upload limit")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                if expected_format == "JPEG":
                    rendered = oriented.convert("RGB")
                elif oriented.mode in {"RGBA", "LA"} or (
                    oriented.mode == "P" and "transparency" in oriented.info
                ):
                    rendered = oriented.convert("RGBA")
                else:
                    rendered = oriented.convert("RGB")
                clean = Image.new(rendered.mode, rendered.size)
                clean.paste(rendered)
                clean.info.clear()
                output = BytesIO()
                if expected_format == "JPEG":
                    clean.save(
                        output,
                        format="JPEG",
                        quality=92,
                        optimize=True,
                        progressive=True,
                        exif=b"",
                        icc_profile=None,
                    )
                else:
                    clean.save(
                        output,
                        format="PNG",
                        optimize=True,
                        exif=b"",
                        icc_profile=None,
                        pnginfo=None,
                    )
                return output.getvalue()
    except ValueError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
    ) as exc:
        raise ValueError("invalid image upload") from exc
