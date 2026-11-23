"""Bound every published artwork payload below Kometa/Plex's upload ceiling."""

import io
import logging

from PIL import Image, ImageOps, PngImagePlugin

# Kometa reports a 10,480,000-byte ceiling. Leave room below that boundary.
KOMETA_ARTWORK_MAX_BYTES = 10_000_000


def fit_artwork_bytes(content, *, max_bytes=KOMETA_ARTWORK_MAX_BYTES, minimum=(1, 1)):
    """Keep compliant bytes unchanged; recompress/resize oversized images safely.

    Preserve PNG alpha/text and image format. Never silently switch a .png into
    JPEG. A payload that cannot meet the limit and minimum dimensions is rejected
    before a destination is replaced. Download budgets remain independent.
    """
    limit = min(KOMETA_ARTWORK_MAX_BYTES, int(max_bytes))
    if limit <= 0:
        raise ValueError("Artwork byte limit must be positive")
    if len(content) <= limit:
        return content
    with Image.open(io.BytesIO(content)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        kind = source.format
        text = PngImagePlugin.PngInfo()
        for key, value in getattr(source, "text", {}).items():
            text.add_text(key, value)
        if kind not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("Oversized artwork has an unsupported image format")
        original_size = image.size
        while image.width >= minimum[0] and image.height >= minimum[1]:
            qualities = (95, 90, 85, 80) if kind != "PNG" else (None,)
            for quality in qualities:
                output = io.BytesIO()
                options = {"optimize": True}
                if kind == "PNG":
                    options["pnginfo"] = text
                else:
                    options["quality"] = quality
                image.save(output, format=kind, **options)
                result = output.getvalue()
                if len(result) <= limit:
                    logging.getLogger(__name__).info(
                        "[Artwork] Size normalized | Bytes: %d -> %d | Dimensions: %s -> %s | Limit: %d",
                        len(content),
                        len(result),
                        original_size,
                        image.size,
                        limit,
                    )
                    return result
            size = (max(1, int(image.width * 0.85)), max(1, int(image.height * 0.85)))
            if size == image.size:
                break
            image = image.resize(size, Image.Resampling.LANCZOS)
    raise ValueError("Artwork cannot meet Kometa's byte limit within minimum dimensions")
