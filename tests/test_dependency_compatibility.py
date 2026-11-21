"""Exercise installed provider dependencies together without external credentials."""

import asyncio
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import aiohttp
import pytest
import requests
from aiolimiter import AsyncLimiter
from multidict import CIMultiDict
from PIL import Image
from plexapi.server import PlexServer


@pytest.fixture
def provider_http():
    image_bytes = io.BytesIO()
    with Image.new("RGB", (16, 24), "navy") as image:
        image.save(image_bytes, format="PNG")
    artwork = image_bytes.getvalue()
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            captured.append({
                "path": path,
                "token": self.headers.get("X-Plex-Token"),
                "selectors": self.headers.get_all("X-Selector"),
            })
            status, content_type = 200, "application/xml"
            if path == "/":
                body = (
                    b'<MediaContainer machineIdentifier="fixture" '
                    b'friendlyName="Dependency fixture" version="1.43.3.10896" />'
                )
            elif path == "/library/metadata/42":
                body = (
                    b'<MediaContainer size="1"><Video type="movie" ratingKey="42" '
                    b'key="/library/metadata/42" title="Dependency fixture" '
                    b'year="2026" guid="plex://movie/fixture">'
                    b'<Guid id="tmdb://12345" /><Media><Part '
                    b'file="/media/Fixture (2026)/movie.mkv" />'
                    b'</Media></Video></MediaContainer>'
                )
            elif path == "/artwork":
                body, content_type = artwork, "image/png"
            else:
                status, body = 429, b"retry later"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "one=1")
            self.send_header("Set-Cookie", "two=2")
            if status == 429:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", captured
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_installed_plexapi_requests_and_xml_inventory_remain_compatible(provider_http):
    base_url, captured = provider_http
    with requests.Session() as session:
        session.trust_env = False
        server = PlexServer(base_url, "synthetic-test-token", session=session, timeout=2)
        item = server.fetchItem("/library/metadata/42")
        assert server.machineIdentifier == "fixture"
        assert item.ratingKey == 42 and item.year == 2026
        assert item.guids[0].id == "tmdb://12345"
        assert item.media[0].parts[0].file == "/media/Fixture (2026)/movie.mkv"
    assert all(record["token"] == "synthetic-test-token" for record in captured)


def test_installed_http_stack_preserves_duplicate_headers_retries_and_image_bytes(provider_http):
    base_url, captured = provider_http

    async def exercise():
        headers = CIMultiDict([("X-Selector", "one"), ("X-Selector", "two")])
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
            async with session.get(f"{base_url}/limited") as response:
                assert response.status == 429 and response.headers.get("Retry-After") == "1"
                await response.read()
            async with session.get(f"{base_url}/artwork", headers=headers) as response:
                assert response.status == 200
                assert response.headers.getall("Set-Cookie") == ["one=1", "two=2"]
                data = await response.read()
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (16, 24) and image.format == "PNG"
            image.verify()

    asyncio.run(exercise())
    assert captured[-1]["selectors"] == ["one", "two"]


def test_installed_rate_limiter_recovers_after_a_waiter_is_cancelled():
    async def exercise():
        limiter = AsyncLimiter(1, 0.02)
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.wait_for(limiter.acquire(), timeout=2)
        async with limiter:
            assert limiter.max_rate == 1

    asyncio.run(exercise())
