import asyncio
from contextlib import asynccontextmanager
from io import BytesIO

import pytest
from PIL import Image

from helper.concurrency import CircuitOpenError
from modules import utils


def _image(format_name="JPEG", size=(32, 48), color="red"):
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format=format_name)
    return output.getvalue()


class _Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(self, status, *, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.content = _Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.responses.pop(0)


class _Concurrency:
    def __init__(self):
        self.failures = []

    def failure(self, reason, **kwargs):
        self.failures.append((reason, kwargs))


def _runtime_slot(concurrency):
    @asynccontextmanager
    async def slot(*_args, **_kwargs):
        yield concurrency

    return slot


def _config():
    return {
        "plex": {"url": "http://plex:32400", "token": "secret"},
        "runtime": {"max_image_mb": 1},
    }


def test_external_artwork_rejects_untrusted_and_unsupported_urls():
    config = _config()
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "http://fanart.tv/image.jpg",
            provider="fanart",
            session=_Session(),
        )
    )[2].startswith("Rejected untrusted")
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "http://other:32400/image.jpg",
            provider="plex",
            session=_Session(),
        )
    )[2].startswith("Rejected artwork URL")
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "https://example/image.jpg",
            provider="unknown",
            session=_Session(),
        )
    )[2].startswith("Unsupported")


def test_external_artwork_success_headers_and_validation(monkeypatch):
    concurrency = _Concurrency()
    monkeypatch.setattr(utils, "runtime_slot", _runtime_slot(concurrency))
    config = _config()
    fanart = _Session(
        [_Response(200, headers={"Content-Type": "image/jpeg"}, chunks=[b"a", b"b"])]
    )
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "https://assets.fanart.tv/image.jpg",
            provider="fanart",
            session=fanart,
        )
    ) == (b"ab", 200, None)
    assert fanart.calls[0][1]["headers"] == {}

    plex = _Session([_Response(200, chunks=[b"plex"])])
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "http://plex:32400/library/image",
            provider="plex",
            session=plex,
        )
    ) == (b"plex", 200, None)
    assert plex.calls[0][1]["headers"] == {"X-Plex-Token": "secret"}

    not_image = _Session(
        [_Response(200, headers={"Content-Type": "text/html"}, chunks=[b"x"])]
    )
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=not_image,
        )
    )[2] == "Response is not an image"
    declared = _Session(
        [_Response(200, headers={"Content-Length": str(2 * 1024 * 1024)})]
    )
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=declared,
        )
    )[2] == "Artwork exceeds MAX_IMAGE_MB"
    invalid_declared = _Session(
        [_Response(200, headers={"Content-Length": "unknown"}, chunks=[b"ok"])]
    )
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=invalid_declared,
        )
    )[0] == b"ok"
    streamed = _Session([_Response(200, chunks=[b"x" * (1024 * 1024 + 1)])])
    assert asyncio.run(
        utils._download_external_artwork(
            config,
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=streamed,
        )
    )[2] == "Artwork exceeds MAX_IMAGE_MB"


def test_external_artwork_retries_rate_limits_and_failures(monkeypatch):
    concurrency = _Concurrency()
    monkeypatch.setattr(utils, "runtime_slot", _runtime_slot(concurrency))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(utils.asyncio, "sleep", no_sleep)
    session = _Session(
        [
            _Response(429, headers={"Retry-After": "invalid"}),
            _Response(500),
            _Response(404),
        ]
    )
    result = asyncio.run(
        utils._download_external_artwork(
            _config(),
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=session,
            retries=3,
        )
    )
    assert result == (None, 404, "fanart artwork returned HTTP 404")
    assert concurrency.failures[0] == ("rate_limit", {"cooldown": 2})
    assert concurrency.failures[1][0] == "server_error"

    session = _Session([_Response(429, headers={"Retry-After": "120"})])
    result = asyncio.run(
        utils._download_external_artwork(
            _config(),
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=session,
            retries=1,
        )
    )
    assert result[2] == "fanart artwork download failed"
    assert concurrency.failures[-1] == ("rate_limit", {"cooldown": 60})

    session = _Session(error=OSError("offline"))
    assert asyncio.run(
        utils._download_external_artwork(
            _config(),
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=session,
            retries=1,
        )
    )[2] == "offline"


def test_external_artwork_circuit_and_cancellation(monkeypatch):
    @asynccontextmanager
    async def circuit(*_args, **_kwargs):
        raise CircuitOpenError("fanart", 3)
        yield

    monkeypatch.setattr(utils, "runtime_slot", circuit)
    result = asyncio.run(
        utils._download_external_artwork(
            _config(),
            "https://fanart.tv/image.jpg",
            provider="fanart",
            session=_Session(),
        )
    )
    assert result[2] == "fanart circuit is open for 3.0 more second(s)"

    @asynccontextmanager
    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError
        yield

    monkeypatch.setattr(utils, "runtime_slot", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            utils._download_external_artwork(
                _config(),
                "https://fanart.tv/image.jpg",
                provider="fanart",
                session=_Session(),
            )
        )


def test_image_analysis_rejects_bad_formats_aspect_and_provider_dimensions():
    with pytest.raises(ValueError, match="Unsupported"):
        utils.analyze_image_content(_image("GIF"))
    with pytest.raises(ValueError, match="Poster aspect"):
        utils.analyze_image_content(_image(size=(100, 20)))
    with pytest.raises(ValueError, match="Background aspect"):
        utils.analyze_image_content(
            _image(size=(10, 100)), asset_type="background"
        )
    with pytest.raises(ValueError, match="provider metadata"):
        utils.analyze_image_content(
            _image(), expected_image={"width": 1000, "height": 1500}
        )
    analysis = utils.analyze_image_content(_image("PNG", color="white"))
    assert analysis["blank"] is True
    assert len(analysis["perceptual_hash"]) == 16


def test_artwork_analysis_cache_and_policy_edge_cases(monkeypatch, tmp_path):
    utils._ARTWORK_ANALYSIS_MEMORY.clear()
    monkeypatch.setattr(
        utils,
        "load_artwork_analysis",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    candidate = {"provider": "fanart", "file_path": "/one.jpg"}
    assert utils._artwork_content_analysis(candidate) is None
    assert utils._artwork_content_analysis(candidate) is None

    asset = tmp_path / "Season01.jpg"
    asset.write_bytes(b"owned")
    checksum = utils.sha256_file(asset)
    allowed = utils.asset_write_allowed(
        {"assets": {"update_policy": "managed"}},
        "show",
        asset,
        "season",
        season_number=1,
        cached_entry={
            "seasons": {
                "1": {"season_path": str(asset), "season_checksum": checksum}
            }
        },
    )
    assert allowed == (True, "managed")
    monkeypatch.setattr(
        utils,
        "sha256_file",
        lambda _path: (_ for _ in ()).throw(OSError("permission denied")),
    )
    assert utils.asset_write_allowed(
        {"assets": {"update_policy": "managed"}},
        "show",
        asset,
        "season",
        season_number=1,
        cached_entry={
            "seasons": {
                "1": {"season_path": str(asset), "season_checksum": checksum}
            }
        },
    ) == (False, "unverifiable")
