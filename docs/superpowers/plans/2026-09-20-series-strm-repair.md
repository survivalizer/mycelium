# Series-aware `.strm` Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The six-hourly `.strm` repair and its Maintenance button cover series as well as movies, with a guard against mass deletion, and the one remaining movies-only button says so.

**Architecture:** A second walker inside `strm_generator.repair_expired_strms`, selected by `media_type`, resolves every episode file by show, season and episode against `virtual_items`, rewrites stale addresses, and requeues orphans per show under a one-half guard. A new `repair_all_strms()` runs both walkers and is what the scheduler and the route call. Two button descriptions and one API type change in the frontend.

**Tech Stack:** Python 3.12, SQLite via `db.py`, pytest; React with TypeScript for the two description strings.

**Spec:** `docs/superpowers/specs/2026-09-20-series-strm-repair-design.md`

## Global Constraints

- Never write two hyphens in a row anywhere (code, comments, docs, commit messages); markdown table separators and CLI flags inside code spans excepted.
- The repo is public: no keys, tokens or addresses. Example hosts are `mycelium.example`.
- Work on branch `main`. No `Co-Authored-By`. Every commit ends with the trailer line exactly: `Claude-Session: https://claude.ai/code/session_01JmQRizubE8QR1E6aUwA1Jx`
- Tests never import `app.py`, `appcore.py` or `routes/*`; startup and route facts are asserted on source text via `_src` from `tests/_helpers.py` and `src_for_route` from `tests/_routes.py`. Every DB test file has its own `_isolated_db` autouse fixture; no conftest; no network.
- Python: `PYTHONDONTWRITEBYTECODE=1 .venv-sdd/bin/python -m pytest tests/ -q -p no:cacheprovider` (1318 tests green at the start). Frontend: `cd frontend && npx tsc --noEmit && npx vitest run`, then `npm run build` and commit `static/app/` on any frontend change.
- The movie walker's logic, results and log lines do not change. No route path, method or setting changes.
- Mutation checks after every new test: copy the touched files to the session scratchpad first, break the implementation, the test must fail, restore by copying back. Never `git checkout` a file with uncommitted edits.
- Log strings follow the file's house style of a single hyphen padded by two spaces (`  -  `) for asides, never two hyphens.

---

## File Structure

| File | Responsibility |
|---|---|
| `strm_generator.py` | The series walker (`_repair_series_strms_locked`, `_series_show_folder`, `_series_imdb`, `_series_requeue`, `_EP_TAG_RE`), the dispatch in `repair_expired_strms`, and `repair_all_strms()` |
| `tests/test_strm_repair_series.py` | Runtime tests of the walker on a temp media tree plus source-text guards for the scheduler and the route |
| `app.py` | Scheduler job `strm_repair` targets `repair_all_strms` |
| `routes/setup.py` | `POST /ui/api/repair-strms` returns the nested result |
| `frontend/src/api.ts` | `RepairCounts` type and the nested response type of `repairStrms` |
| `frontend/src/pages/admin/Maintenance.tsx` | Two description strings and the summed result line |
| `CHANGELOG.md` | `## [Unreleased]`, `### Fixed` |

---

### Task 1: The series walker

**Files:**
- Modify: `strm_generator.py` (the block starting at `def repair_expired_strms(media_type: str = "movie") -> dict:`, and new helpers placed directly above it)
- Test: `tests/test_strm_repair_series.py` (new)

**Interfaces:**
- Consumes: `db.get_virtual_item_by_episode(imdb_id, season, episode) -> dict | None`, `db.get_request_by_imdb(imdb_id) -> dict | None`, `db.upsert_wanted_episode(imdb_id, tmdb_id, title, season, episode, air_date)`, `db.get_wanted_episode(imdb_id, season, episode) -> dict | None`, `db.mark_episode_status(imdb_id, season, episode, status)`, `catbox.proxy_url(token) -> str`, `jellyfin.note_change(path, update_type)` with `update_type` in `Created`, `Modified`, `Deleted`.
- Produces: `strm_generator.repair_expired_strms("series") -> dict` with keys `scanned, ok, missing_strm, orphaned_tokens, relinked, requeued, skipped, guarded`; the lock-busy dict of `repair_expired_strms` gains `"guarded": 0` for both media types.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_strm_repair_series.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv-sdd/bin/python -m pytest tests/test_strm_repair_series.py -q -p no:cacheprovider`

Expected: failures. The stale-address test fails because the series branch does not exist (the movie walker scans `series/` as if it were a movies tree and reports nothing relinked); the lock test fails on the missing `guarded` key.

- [ ] **Step 3: Add the helpers and the walker**

In `strm_generator.py`, directly above `def repair_expired_strms(media_type: str = "movie") -> dict:`, add:

```python
# Season and episode from an episode file name: S01E03, S1E3, S01E123.
# _EP_RE near the top of this module stops at two episode digits; long
# running shows pass 99.
_EP_TAG_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})(?!\d)")
_SEASON_DIR_RE = re.compile(r"^Season \d+$")
_IMDB_IN_NFO_RE = re.compile(
    r"<imdbid>(tt\d+)</imdbid>"
    r"|<uniqueid[^>]*type=['\"]imdb['\"][^>]*>(tt\d+)</uniqueid>"
    r"|(tt\d{7,})"
)


def _series_show_folder(strm_path: Path) -> Path:
    """The show folder for an episode file: the grandparent when the file
    sits in a Season NN folder, else the parent."""
    parent = strm_path.parent
    return parent.parent if _SEASON_DIR_RE.match(parent.name) else parent


def _series_imdb(show_folder: Path) -> str | None:
    """IMDb id of a show: tvshow.nfo first, any other .nfo in the show
    folder as a fallback. None when nothing readable names one."""
    candidates = [show_folder / "tvshow.nfo"]
    candidates += [p for p in sorted(show_folder.glob("*.nfo")) if p.name != "tvshow.nfo"]
    for nfo in candidates:
        if not nfo.is_file():
            continue
        try:
            text = nfo.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = _IMDB_IN_NFO_RE.search(text)
        if m:
            return next(g for g in m.groups() if g)
    return None


def _series_requeue(imdb_id: str, show_name: str, strm_path: Path,
                    season: int, episode: int) -> None:
    """Remove an orphaned episode file and put the episode back on the
    wanted list: the recipe catbox_packs.detach_episode uses, minus the
    hash exclusion, because an orphan has no hash to exclude."""
    import datetime as _dt
    import jellyfin
    for path in (strm_path, strm_path.with_suffix(".nfo")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("repair_strms: could not remove %s: %s", path, exc)
    jellyfin.note_change(strm_path, "Deleted")
    req = db.get_request_by_imdb(imdb_id) or {}
    title = req.get("title") or show_name
    db.upsert_wanted_episode(imdb_id, req.get("tmdb_id"), title, season, episode, None)
    row = db.get_wanted_episode(imdb_id, season, episode) or {}
    air_date = row.get("air_date")
    status = "wanted"
    if air_date and air_date > _dt.date.today().isoformat():
        status = "not_aired"
    db.mark_episode_status(imdb_id, season, episode, status)
    log.info("repair_strms: orphaned S%02dE%02d of %s removed, requeued as %s",
             season, episode, show_name, status)


def _repair_series_strms_locked() -> dict:
    """The series walker of repair_expired_strms. Files live under
    MEDIA_PATH/series/<Show>/Season NN/, one per episode with its own
    token, so every file is resolved by show, season and episode. There is
    no pass 1: a folder with no .strm belongs to the series monitor.
    Orphans are decided per show, and a show with more than half of its
    files orphaned is skipped, so a database restored from an old backup
    cannot turn into a library-wide delete."""
    import catbox as _catbox
    import jellyfin

    zero = {"scanned": 0, "ok": 0, "missing_strm": 0, "orphaned_tokens": 0,
            "relinked": 0, "requeued": 0, "skipped": 0, "guarded": 0}
    root = Path(MEDIA_PATH) / "series"
    if not root.is_dir():
        return zero

    scanned = ok = relinked = requeued = skipped = guarded = 0
    per_show: dict[Path, dict] = {}

    for strm_path in sorted(root.rglob("*.strm")):
        scanned += 1
        m = _EP_TAG_RE.search(strm_path.name)
        if not m:
            skipped += 1
            log.info("repair_strms: no episode tag in %s  -  skipping", strm_path.name)
            continue
        season, episode = int(m.group(1)), int(m.group(2))
        show = _series_show_folder(strm_path)
        entry = per_show.setdefault(show, {"imdb": _series_imdb(show), "files": 0, "orphans": []})
        if not entry["imdb"]:
            skipped += 1
            continue
        entry["files"] += 1
        try:
            content = strm_path.read_text(encoding="utf-8").strip()
        except OSError:
            skipped += 1
            continue
        row = db.get_virtual_item_by_episode(entry["imdb"], season, episode)
        if row is None:
            entry["orphans"].append((strm_path, season, episode))
            continue
        want = _catbox.proxy_url(row["token"])
        if content == want:
            ok += 1
            continue
        try:
            strm_path.write_text(want, encoding="utf-8")
        except OSError as exc:
            log.warning("repair_strms: could not write %s: %s", strm_path, exc)
            skipped += 1
            continue
        jellyfin.note_change(strm_path, "Modified")
        relinked += 1
        log.info("repair_strms: rewrote %s → token %s", strm_path.name, row["token"])

    for show, entry in per_show.items():
        orphans = entry["orphans"]
        if not orphans:
            continue
        if len(orphans) * 2 > entry["files"]:
            log.warning("repair_strms: %s: %d of %d episode files have no virtual item, skipping the show",
                        show.name, len(orphans), entry["files"])
            guarded += len(orphans)
            continue
        for strm_path, season, episode in orphans:
            _series_requeue(entry["imdb"], show.name, strm_path, season, episode)
            requeued += 1

    log.info("repair_strms (series): ok=%d relinked=%d requeued=%d guarded=%d skipped=%d",
             ok, relinked, requeued, guarded, skipped)
    return {
        "scanned": scanned, "ok": ok, "missing_strm": 0,
        "orphaned_tokens": requeued + guarded, "relinked": relinked,
        "requeued": requeued, "skipped": skipped, "guarded": guarded,
    }
```

Then change `repair_expired_strms` itself. Its docstring's first line becomes `"""Find and fix all unplayable movie and series entries.` and a paragraph is appended before `Returns a summary dict with counts.`:

```
    Series files (media_type="series") are walked by _repair_series_strms_locked:
    resolved per episode, orphans requeued per show under a one-half guard,
    no pass 1.
```

The lock-busy dict gains `"guarded": 0`, and the `try` body dispatches:

```python
    if not _maintenance_lock.acquire(blocking=False):
        log.warning("repair_expired_strms: maintenance already running  -  skipping")
        return {"scanned": 0, "ok": 0, "missing_strm": 0, "orphaned_tokens": 0,
                "relinked": 0, "requeued": 0, "skipped": 0, "guarded": 0}
    try:
        if media_type == "movie":
            return _repair_expired_strms_locked("movie")
        return _repair_series_strms_locked()
    finally:
        _maintenance_lock.release()
```

Finally, in `_repair_expired_strms_locked`, add `"guarded": 0` to both of its return dicts (the early `return {...}` when the root is missing, and the final `return {...}`), so both walkers report the same keys. Nothing else in the movie walker changes.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv-sdd/bin/python -m pytest tests/test_strm_repair_series.py -q -p no:cacheprovider`

Expected: 9 passed.

- [ ] **Step 5: Mutation checks**

Copy `strm_generator.py` to the scratchpad first. Three mutations, each restored by copying back:

1. In the guard, change `len(orphans) * 2 > entry["files"]` to `False`. Expected: the guard test fails (files removed, `guarded == 0`).
2. Remove the `strm_path.write_text(want, ...)` line. Expected: the stale-address and three-digit tests fail.
3. In `_series_requeue`, remove the `db.mark_episode_status(...)` call. Expected: the orphan test fails on `row["status"]`.

- [ ] **Step 6: Full suite and commit**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv-sdd/bin/python -m pytest tests/ -q -p no:cacheprovider`

Expected: 1327 passed (1318 plus the 9 new).

```bash
git add strm_generator.py tests/test_strm_repair_series.py
git commit -m "fix(strm): the repair walks series files too, with a per-show orphan guard

Claude-Session: https://claude.ai/code/session_01JmQRizubE8QR1E6aUwA1Jx"
```

---

### Task 2: Both walkers from the scheduler and the route

**Files:**
- Modify: `strm_generator.py` (after `repair_expired_strms`), `app.py` (the `if CATBOX_MODE:` block that schedules `strm_repair`), `routes/setup.py` (`ui_api_repair_strms`)
- Test: `tests/test_strm_repair_series.py` (append)

**Interfaces:**
- Consumes: `strm_generator.repair_expired_strms(media_type) -> dict` from Task 1.
- Produces: `strm_generator.repair_all_strms() -> dict` returning `{"movie": <counts>, "series": <counts>}`; `POST /ui/api/repair-strms` answers that dict as JSON.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strm_repair_series.py`:

```python
from _routes import src_for_route  # noqa: E402


def test_repair_all_runs_both_walkers_once_each(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(strm_generator, "repair_expired_strms",
                        lambda media_type="movie": (calls.append(media_type), {"scanned": 1})[1])
    result = strm_generator.repair_all_strms()
    assert calls == ["movie", "series"]
    assert result == {"movie": {"scanned": 1}, "series": {"scanned": 1}}


def test_the_scheduler_and_the_route_use_repair_all():
    app_src = _src("app.py")
    assert "strm_generator.repair_all_strms," in app_src
    assert "strm_generator.repair_expired_strms," not in app_src
    assert "movies and series" in app_src
    route_src = src_for_route("/ui/api/repair-strms")
    body = route_src.split('@bp.post("/ui/api/repair-strms")')[1].split("\n@bp.")[0]
    assert "strm_generator.repair_all_strms()" in body
    assert 'repair_expired_strms(media_type="movie")' not in body
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv-sdd/bin/python -m pytest tests/test_strm_repair_series.py -q -p no:cacheprovider -k "repair_all or scheduler"`

Expected: 2 failed (`repair_all_strms` does not exist; the source-text assertions miss).

- [ ] **Step 3: Implement**

In `strm_generator.py`, directly after `repair_expired_strms`:

```python
def repair_all_strms() -> dict:
    """Both walkers, one lock acquisition each, so a lock held during one
    does not skip the other. Returns {"movie": counts, "series": counts}."""
    return {"movie": repair_expired_strms("movie"),
            "series": repair_expired_strms("series")}
```

In `app.py`, the scheduler block becomes:

```python
    if CATBOX_MODE:
        scheduler.add_job(
            strm_generator.repair_all_strms,
            trigger="interval", hours=6,
            id="strm_repair", next_run_time=None,
        )
        log.info("Scheduled automatic .strm repair (movies and series) every 6h")
```

In `routes/setup.py`, `ui_api_repair_strms` becomes:

```python
@bp.post("/ui/api/repair-strms")
@auth.require_auth
def ui_api_repair_strms():
    """Repair .strm files for movies and series: rewrite a file whose URL is
    not the current catbox proxy URL for its token, requeue a file whose
    token Mycelium no longer knows. Answers {"movie": counts, "series":
    counts}; see strm_generator.repair_expired_strms for the keys."""
    if not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    return jsonify(**strm_generator.repair_all_strms())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv-sdd/bin/python -m pytest tests/test_strm_repair_series.py -q -p no:cacheprovider`

Expected: 11 passed.

- [ ] **Step 5: Mutation check and full suite**

Copy `app.py` to the scratchpad, change the job target back to `strm_generator.repair_expired_strms,`, run the file: the scheduler test must fail. Restore by copying back. Then the full suite: `PYTHONDONTWRITEBYTECODE=1 .venv-sdd/bin/python -m pytest tests/ -q -p no:cacheprovider`, expected 1329 passed. Also `.venv-sdd/bin/python scripts/route_table.py --check` must still print the match line (no route changed).

- [ ] **Step 6: Commit**

```bash
git add strm_generator.py app.py routes/setup.py tests/test_strm_repair_series.py
git commit -m "fix(strm): schedule the repair for movies and series, route answers both

Claude-Session: https://claude.ai/code/session_01JmQRizubE8QR1E6aUwA1Jx"
```

---

### Task 3: Honest descriptions, the summed result line, changelog

**Files:**
- Modify: `frontend/src/api.ts` (the `repairStrms` entry and a new exported type beside `export type RepairItem`), `frontend/src/pages/admin/Maintenance.tsx` (two `ActionButton` blocks), `frontend/src/pages/admin/Maintenance.test.tsx` (only if a test exercises the repair button), `CHANGELOG.md`, `static/app/` (rebuilt)

**Interfaces:**
- Consumes: the nested `{movie, series}` response from Task 2.
- Produces: nothing downstream.

- [ ] **Step 1: The API type**

In `frontend/src/api.ts`, next to the existing `export type RepairItem`, add:

```ts
export type RepairCounts = {
  scanned: number; ok: number; missing_strm: number; orphaned_tokens: number;
  relinked: number; requeued: number; skipped: number; guarded: number;
};
```

and change the `repairStrms` entry to:

```ts
  repairStrms: () =>
    http<{ movie: RepairCounts; series: RepairCounts }>('/ui/api/repair-strms', { method: 'POST' }),
```

- [ ] **Step 2: The two buttons**

In `frontend/src/pages/admin/Maintenance.tsx`, the repair button block becomes:

```tsx
        <ActionButton
          label="Repair broken strm files"
          desc="Rewrites .strm files that point at an expired address or a token Mycelium no longer knows, for movies and series"
          run={async () => {
            const d = await api.repairStrms();
            const sum = (k: keyof RepairCounts) => (d.movie?.[k] ?? 0) + (d.series?.[k] ?? 0);
            return `scanned: ${sum('scanned')}, ok: ${sum('ok')}, relinked: ${sum('relinked')}, requeued: ${sum('requeued')}, guarded: ${sum('guarded')}`;
          }}
        />
```

with `import type { RepairCounts, RepairItem } from '../../api';` replacing the existing `RepairItem` type import at the top of the file.

The duplicate cleanup block's description becomes:

```tsx
          desc="Removes extra .strm files from movie folders that have more than one. Series duplicates are handled by Repair strm files"
```

- [ ] **Step 3: Frontend checks**

Run: `cd frontend && npx tsc --noEmit && npx vitest run`

Expected: clean, 313 passed. If a test in `Maintenance.test.tsx` clicks the repair button and awaits its result, its `repairStrms` mock must resolve `{ movie: {...}, series: {...} }` with the eight keys; update that mock, nothing else.

- [ ] **Step 4: Rebuild and changelog**

Run: `cd frontend && npm run build` (writes `static/app/`).

In `CHANGELOG.md`, add directly under the title block, above `## [1.0.0]`, if `## [Unreleased]` does not exist yet:

```markdown
## [Unreleased]

### Fixed

- The automatic `.strm` repair (every six hours) and the "Repair broken
  strm files" button now cover series as well as movies. An episode file
  whose address is stale is rewritten to the current proxy URL; a file
  whose token Mycelium no longer knows is removed and the episode goes
  back on the wanted list. A show with more than half of its files
  orphaned in one run is skipped and logged, so a database restored from
  an old backup cannot cascade into a library-wide delete. The repair
  endpoint answers one block of counts per media type.
- The "Clean up duplicate strm files" button says it covers movies only,
  which is what it did all along; series duplicates are removed by the
  cleanup run.
```

Check the docs and the in-app manual for the old wording: `grep -rn "folders that have more than one\|Scans movie .strm" docs/ README.md` and update any hit to the new descriptions.

- [ ] **Step 5: Full checks and commit**

Run the Python suite once more (nothing should change: 1329 passed) and `cd frontend && npx tsc --noEmit && npx vitest run`.

```bash
git add frontend/src/api.ts frontend/src/pages/admin/Maintenance.tsx frontend/src/pages/admin/Maintenance.test.tsx CHANGELOG.md static/app docs README.md
git commit -m "fix(maintenance): repair button sums movies and series, duplicate cleanup says movies

Claude-Session: https://claude.ai/code/session_01JmQRizubE8QR1E6aUwA1Jx"
```

(Only add the test file, docs and README if they changed.)

---

## Whole-branch review and release

One reviewer pass over `<base>..HEAD` on the most capable model, one fix wave, one scoped re-review. Then, on request, a patch release: `APP_VERSION` in `version.py`, `## [Unreleased]` to `## [1.0.1] - <date>`, the entry in `releases.json`, commit `chore(release): 1.0.1`, tag `v1.0.1`, push, watch the Release and CI runs.
