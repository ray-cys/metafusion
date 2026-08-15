import asyncio

from helper import tmdb as tmdb_module
from helper.plex import get_plex_metadata


def test_tmdb_request_without_session_does_not_raise_or_log_secret(monkeypatch):
    events = []
    monkeypatch.setattr(
        tmdb_module,
        "log_tmdb_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    result = asyncio.run(
        tmdb_module.tmdb_api_request(
            {"tmdb": {"api_key": "super-secret", "language": "en", "region": "US"}},
            "movie/1",
            session=None,
        )
    )

    assert result == {}
    assert events[-1][1]["query"]["api_key"] == "***"
    assert "super-secret" not in repr(events)


def test_plex_metadata_error_path_has_initialized_context(monkeypatch):
    events = []
    monkeypatch.setattr(
        "helper.plex.log_plex_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    class FlakyItem:
        title = "Example"
        year = 2020
        type = "movie"
        librarySection = None
        guids = []

        def __init__(self):
            self.rating_key_reads = 0

        @property
        def ratingKey(self):
            self.rating_key_reads += 1
            if self.rating_key_reads == 1:
                raise RuntimeError("temporary rating key failure")
            return "123"

    metadata = asyncio.run(get_plex_metadata(FlakyItem()))

    assert metadata["title"] == "Example"
    assert any(event == "plex_failed_extract_item_id" for event, _ in events)
