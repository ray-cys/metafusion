import io
import random

import pytest
from PIL import Image, PngImagePlugin

from helper.artwork_limits import KOMETA_ARTWORK_MAX_BYTES, fit_artwork_bytes


def noise(kind="PNG", mode="RGB"):
    rng = random.Random(1)
    data = bytes(rng.randrange(256) for _ in range(180 * 180 * len(mode)))
    image = Image.frombytes(mode, (180, 180), data)
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("owner", "metafusion")
    image.save(output, format=kind, **({"pnginfo": info} if kind == "PNG" else {}))
    return output.getvalue()


def test_small_unchanged_and_hard_ceiling():
    data = noise()
    assert fit_artwork_bytes(data, max_bytes=KOMETA_ARTWORK_MAX_BYTES * 2) is data
    with pytest.raises(ValueError):
        fit_artwork_bytes(data, max_bytes=0)


@pytest.mark.parametrize(
    ("kind", "mode"), [("PNG", "RGB"), ("PNG", "RGBA"), ("JPEG", "RGB"), ("WEBP", "RGB")]
)
def test_oversized_normalized_and_png_provenance(kind, mode):
    result = fit_artwork_bytes(noise(kind, mode), max_bytes=8000)
    assert len(result) <= 8000
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == kind
        if kind == "PNG":
            assert image.text["owner"] == "metafusion"
        if mode == "RGBA":
            assert image.mode == mode


def test_unfit_and_unsupported_preserve_destination(tmp_path):
    destination = tmp_path / "poster.png"
    destination.write_bytes(b"old")
    with pytest.raises(ValueError):
        fit_artwork_bytes(noise(), max_bytes=1, minimum=(180, 180))
    with pytest.raises(ValueError):
        fit_artwork_bytes(noise(), max_bytes=1)
    image = Image.new("RGB", (20, 20))
    out = io.BytesIO()
    image.save(out, format="BMP")
    with pytest.raises(ValueError):
        fit_artwork_bytes(out.getvalue(), max_bytes=100)
    assert destination.read_bytes() == b"old"


def test_real_upload_ceiling_with_large_generated_png():
    image = Image.frombytes("RGB", (2048, 2048), random.Random(8).randbytes(2048 * 2048 * 3))
    output = io.BytesIO()
    image.save(output, format="PNG")
    assert len(output.getvalue()) > KOMETA_ARTWORK_MAX_BYTES
    result = fit_artwork_bytes(output.getvalue())
    assert len(result) <= KOMETA_ARTWORK_MAX_BYTES
    with Image.open(io.BytesIO(result)) as decoded:
        assert decoded.format == "PNG" and decoded.width > 1000


def test_save_poster_keeps_existing_file_when_limit_cannot_be_met(tmp_path, monkeypatch):
    import asyncio

    from modules import utils

    original_fit = fit_artwork_bytes
    monkeypatch.setattr(
        utils,
        "fit_artwork_bytes",
        lambda data, **kwargs: original_fit(data, max_bytes=100, **kwargs),
    )
    target = tmp_path / "poster.png"
    target.write_bytes(b"existing artwork")
    result, error = asyncio.run(
        utils.save_poster(
            noise(), target, config={"poster_set": {"min_width": 180, "min_height": 180}}
        )
    )
    assert result is False and "byte limit" in error
    assert target.read_bytes() == b"existing artwork"
