"""The .strm repair walks series too: season folders, one token per
episode, and an orphan guard per show. The movie walker is untouched;
these tests only build a series tree."""
import logging
import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import catbox  # noqa: E402
import db  # noqa: E402
import jellyfin  # noqa: E402
import strm_generator  # noqa: E402
from _helpers import _src, _drop_cached_conn  # noqa: E402

HOST = "https://mycelium.example"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    _drop_cached_conn()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    _drop_cached_conn()
    db.init()
    yield
    _drop_cached_conn()


@pytest.fixture
def media(tmp_path, monkeypatch):
    """A media root with an empty series tree, the catbox host pinned, and
    Jellyfin notes captured instead of sent."""
    root = tmp_path / "media"
    (root / "series").mkdir(parents=True)
    monkeypatch.setattr(strm_generator, "MEDIA_PATH", str(root))
    monkeypatch.setattr(catbox, "catbox_host", lambda: HOST)
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(jellyfin, "note_change", lambda p, t: notes.append((str(p), t)))
    return root, notes


def _show(root: Path, name: str = "Reacher", imdb: str = "tt9288030", nfo: bool = True) -> Path:
    show = root / "series" / name
    (show / "Season 01").mkdir(parents=True)
    if nfo:
        (show / "tvshow.nfo").write_text(
            f'<tvshow><title>{name}</title><uniqueid type="imdb">{imdb}</uniqueid></tvshow>',
            encoding="utf-8")
    return show


def _episode(show: Path, season: int, episode: int, content: str) -> Path:
    path = show / f"Season {season:02d}" / f"{show.name} S{season:02d}E{episode:02d}.strm"
    path.write_text(content, encoding="utf-8")
    path.with_suffix(".nfo").write_text("<episodedetails/>", encoding="utf-8")
    return path


def _register(imdb: str, season: int, episode: int, strm_path: Path) -> str:
    """One virtual item for an episode, inserted directly so the test has
    no dependency on what catbox.register does besides the insert."""
    token = secrets.token_hex(8)
    db.insert_virtual_item(token, "a" * 40, "magnet:?xt=urn:btih:" + "a" * 40,
                           f"{strm_path.parent.parent.name} S{season:02d}E{episode:02d}",
                           "series", strm_path=str(strm_path), imdb_id=imdb,
                           season=season, episode=episode)
    return token


def test_a_stale_address_is_rewritten_and_jellyfin_is_told(media):
    root, notes = media
    show = _show(root)
    path = _episode(show, 1, 3, "placeholder")
    token = _register("tt9288030", 1, 3, path)
    path.write_text(f"http://mycelium:8088/stream/{token}", encoding="utf-8")

    result = strm_generator.repair_expired_strms("series")

    assert path.read_text(encoding="utf-8") == f"{HOST}/stream/{token}"
    assert result["relinked"] == 1 and result["ok"] == 0 and result["requeued"] == 0
    assert (str(path), "Modified") in notes


def test_a_matching_file_is_left_alone(media):
    root, notes = media
    show = _show(root)
    path = _episode(show, 1, 3, "placeholder")
    token = _register("tt9288030", 1, 3, path)
    path.write_text(f"{HOST}/stream/{token}", encoding="utf-8")
    before = path.stat().st_mtime_ns

    result = strm_generator.repair_expired_strms("series")

    assert result["ok"] == 1 and result["relinked"] == 0
    assert path.stat().st_mtime_ns == before
    assert notes == []


def test_an_orphan_is_removed_and_put_back_on_the_wanted_list(media):
    root, notes = media
    show = _show(root)
    for ep in (1, 2):
        p = _episode(show, 1, ep, "placeholder")
        token = _register("tt9288030", 1, ep, p)
        p.write_text(f"{HOST}/stream/{token}", encoding="utf-8")
    orphan = _episode(show, 1, 3, f"{HOST}/stream/{'f' * 16}")
    orphan_nfo = orphan.with_suffix(".nfo")

    result = strm_generator.repair_expired_strms("series")

    assert not orphan.exists() and not orphan_nfo.exists()
    row = db.get_wanted_episode("tt9288030", 1, 3)
    assert row is not None and row["status"] == "wanted" and row["title"] == "Reacher"
    assert (str(orphan), "Deleted") in notes
    assert result["requeued"] == 1 and result["orphaned_tokens"] == 1 and result["guarded"] == 0
    assert result["ok"] == 2


def test_an_orphan_with_a_future_air_date_is_requeued_as_not_aired(media):
    root, notes = media
    show = _show(root)
    for ep in (1, 2):
        p = _episode(show, 1, ep, "placeholder")
        token = _register("tt9288030", 1, ep, p)
        p.write_text(f"{HOST}/stream/{token}", encoding="utf-8")
    orphan = _episode(show, 1, 3, f"{HOST}/stream/{'f' * 16}")
    orphan_nfo = orphan.with_suffix(".nfo")
    db.upsert_wanted_episode("tt9288030", None, "Reacher", 1, 3, "2999-01-01")

    result = strm_generator.repair_expired_strms("series")

    assert not orphan.exists() and not orphan_nfo.exists()
    assert result["requeued"] == 1
    row = db.get_wanted_episode("tt9288030", 1, 3)
    assert row is not None and row["status"] == "not_aired"


def test_the_guard_skips_a_show_whose_files_are_mostly_orphans(media, caplog):
    root, notes = media
    show = _show(root)
    good = _episode(show, 1, 1, "placeholder")
    token = _register("tt9288030", 1, 1, good)
    good.write_text(f"{HOST}/stream/{token}", encoding="utf-8")
    orphans = [_episode(show, 1, ep, f"{HOST}/stream/{'f' * 16}") for ep in (2, 3)]

    with caplog.at_level(logging.WARNING):
        result = strm_generator.repair_expired_strms("series")

    assert all(p.exists() and p.with_suffix(".nfo").exists() for p in orphans)
    assert db.get_wanted_episode("tt9288030", 1, 2) is None
    assert result["guarded"] == 2 and result["requeued"] == 0 and result["orphaned_tokens"] == 2
    assert any("Reacher" in r.getMessage() and "skipping the show" in r.getMessage()
               for r in caplog.records)
    assert notes == []


def test_exactly_half_orphaned_is_not_guarded(media):
    root, _ = media
    show = _show(root)
    good = _episode(show, 1, 1, "placeholder")
    token = _register("tt9288030", 1, 1, good)
    good.write_text(f"{HOST}/stream/{token}", encoding="utf-8")
    orphan = _episode(show, 1, 2, f"{HOST}/stream/{'f' * 16}")

    result = strm_generator.repair_expired_strms("series")

    assert not orphan.exists()
    assert result["requeued"] == 1 and result["guarded"] == 0


def test_untagged_files_and_shows_without_an_imdb_id_are_skipped(media):
    root, notes = media
    tagged_show = _show(root)
    untagged = tagged_show / "Season 01" / "Reacher extras.strm"
    untagged.write_text("http://mycelium:8088/stream/deadbeefdeadbeef", encoding="utf-8")
    no_nfo_show = _show(root, name="Unknown Show", nfo=False)
    unresolvable = _episode(no_nfo_show, 1, 1, "http://mycelium:8088/stream/deadbeefdeadbeef")

    result = strm_generator.repair_expired_strms("series")

    assert result["skipped"] == 2 and result["scanned"] == 2
    assert untagged.read_text(encoding="utf-8").startswith("http://mycelium:8088")
    assert unresolvable.exists()
    assert notes == []


def test_three_digit_episode_numbers_resolve(media):
    root, _ = media
    show = _show(root, name="One Piece", imdb="tt0388629")
    path = _episode(show, 1, 123, "placeholder")
    token = _register("tt0388629", 1, 123, path)
    path.write_text(f"http://mycelium:8088/stream/{token}", encoding="utf-8")

    result = strm_generator.repair_expired_strms("series")

    assert result["relinked"] == 1
    assert path.read_text(encoding="utf-8") == f"{HOST}/stream/{token}"


def test_a_busy_maintenance_lock_returns_the_zero_shape_with_guarded():
    assert strm_generator._maintenance_lock.acquire(blocking=False)
    try:
        result = strm_generator.repair_expired_strms("series")
    finally:
        strm_generator._maintenance_lock.release()
    assert result["guarded"] == 0 and result["scanned"] == 0
    assert set(result) == {"scanned", "ok", "missing_strm", "orphaned_tokens",
                           "relinked", "requeued", "skipped", "guarded"}


def test_the_movie_walker_still_reports_the_same_keys(media):
    root, _ = media
    (root / "movies").mkdir()
    result = strm_generator.repair_expired_strms("movie")
    assert set(result) == {"scanned", "ok", "missing_strm", "orphaned_tokens",
                           "relinked", "requeued", "skipped", "guarded"}
