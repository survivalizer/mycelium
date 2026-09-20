# Series-aware `.strm` repair and honest maintenance labels

Date: 2026-09-20. Status: draft, awaiting user review.

## Goal

The automatic `.strm` repair that runs every six hours, and the "Repair
broken strm files" button, only ever look at movies. The library on the
reference install is three movies and several hundred episodes, so a
stale address or an orphaned token in an episode file is never repaired
on its own. This design teaches the repair the series folder layout,
schedules it for both media types, and makes the one other movies-only
maintenance action say so.

## Background

`strm_generator.repair_expired_strms(media_type="movie")` walks
`MEDIA_PATH/movies` in two passes: title folders with no `.strm` at all
(requeued through the processor), and existing `.strm` files whose
content is not the current catbox proxy URL for a known token (rewritten
from the title's virtual item, or deleted and requeued when no item
exists). It assumes one `.strm` per title folder with the title's `.nfo`
beside it, and when it relinks it takes the first virtual item of the
title.

Series break every one of those assumptions: files live in
`<Show>/Season NN/`, one per episode, each with its own token; the show's
`.nfo` is `tvshow.nfo` one level above the file; and "the first virtual
item of the title" is an arbitrary episode. Calling the function with
`media_type="series"` today could write one episode's address into
another episode's file. That is why it has been movies-only, and why the
fix is not a changed default.

The scan shape the code needs already exists elsewhere:
`cleanup._remove_duplicates` keys episodes by show, season and episode;
`db.get_virtual_item_by_episode(imdb_id, season, episode)` finds the row
for one episode; `nfo_generator._read_imdb_from_nfo` reads `tvshow.nfo`;
`catbox_packs.detach_episode` shows how an episode goes back on the
wanted list.

## Decisions

| Question | Decision |
|---|---|
| Where the series logic lives | A second walker inside `repair_expired_strms`, selected by `media_type`, sharing the maintenance lock, the token snapshot and the result shape with the movie walker |
| Missing files for series | Not the repair's job. A show folder or season folder with no `.strm` is owned by the series monitor, which already tracks missing episodes in `wanted_episodes`. The series walker has no pass 1 |
| An episode file whose token has no row | Orphaned: the `.strm` and its `.nfo` are removed, Jellyfin is told, and the episode is put back on the wanted list so the monitor searches it, the same recipe `detach_episode` uses minus the hash exclusion (there is no hash to exclude) |
| Guard against a cascade | If more than half of a show's episode files are orphaned in one run, that show is skipped and logged at warning. A database restored from an old backup must not turn into a library-wide delete followed by a `disk_sync` purge |
| Files the walker cannot read | An episode file whose name carries no season and episode tag, or whose show has no readable IMDb id, is counted as skipped and never touched |
| The scheduled job and the button | Both run movies then series through one new entry point `repair_all_strms()`; the button's description stops saying "movie" |
| The duplicate cleanup button | Stays movies-only in code; its description says so. Series duplicates are already removed by the cleanup run (`_remove_duplicates`, quality-aware, one file per episode), so a second implementation would be a duplicate of a duplicate remover |

## 1. The series walker

Root: `MEDIA_PATH/series`. For every `*.strm` under it, in path order:

1. **Identify the episode.** Season and episode come from the file name
   with a pattern that accepts `S01E03`, `S1E3` and three-digit episode
   numbers (`S01E123`), since the existing `_EP_RE` stops at two digits.
   No match: `skipped`.
2. **Identify the show.** The show folder is the file's grandparent when
   the parent is a `Season NN` folder, else the parent. The IMDb id comes
   from `tvshow.nfo` in the show folder through `_read_imdb_from_nfo`,
   falling back to the movie walker's regex over any `.nfo` in that
   folder. No id: `skipped`.
3. **Compare with the database.** `db.get_virtual_item_by_episode(imdb,
   season, episode)`:
   - Row exists and the file content equals `catbox.proxy_url(row.token)`:
     `ok`.
   - Row exists and the content differs (stale host, an expired direct
     CDN URL, a token that belongs to another row): the file is rewritten
     to `proxy_url(row.token)`, Jellyfin is told the path changed
     (`note_change(path, "Modified")`), `relinked`.
   - No row: the file is collected as an orphan for the show, decided in
     step 4.
4. **Decide the orphans per show.** With `n` episode files seen for the
   show and `o` orphans: if `o > n / 2`, log at warning
   `repair_strms: <show>: <o> of <n> episode files have no virtual item,
   skipping the show` and count them as `guarded`. Otherwise each orphan
   is removed together with its `.nfo`, Jellyfin is told
   (`note_change(path, "Deleted")`), and the episode is registered wanted:
   `upsert_wanted_episode(imdb, request.tmdb_id, request.title or show
   folder name, season, episode, None)` then `mark_episode_status(imdb,
   season, episode, "wanted")`, or `"not_aired"` when the wanted row
   already carries a future air date. Counted as `requeued`.

The walker runs under the same `_maintenance_lock` as the movie walker,
so it never overlaps a cleanup, a migration or the other walker. It
needs no token snapshot: every file is resolved with one lookup by show,
season and episode at the moment it is examined, so a row created
mid-run is seen, and a few hundred lookups cost SQLite nothing.

Result dict: the movie walker's keys (`scanned, ok, missing_strm,
orphaned_tokens, relinked, requeued, skipped`) plus `guarded`.
`missing_strm` is always zero for series (no pass 1) and
`orphaned_tokens` is `requeued + guarded`. The Maintenance tab's result
line is rewritten in section 2, so no key is kept for its sake alone.

## 2. Entry points

- `strm_generator.repair_all_strms() -> dict` runs
  `repair_expired_strms("movie")` then `repair_expired_strms("series")`
  and returns `{"movie": {...}, "series": {...}}`. One lock acquisition
  per walker, so a failure in one does not hold the other.
- The scheduler job `strm_repair` calls `repair_all_strms` instead of the
  bare movie function. Its log line becomes "Scheduled automatic .strm
  repair (movies and series) every 6h".
- `POST /ui/api/repair-strms` calls `repair_all_strms` and returns the
  nested dict. The route is internal (`/ui/*`), so the changed shape is
  not a compatibility event.
- The Maintenance tab: the button's description becomes "Rewrites .strm
  files that point at an expired address or a token Mycelium no longer
  knows, for movies and series"; its result line sums the two walkers
  (`scanned`, `ok`, `relinked`, `requeued`, `guarded`).

## 3. The duplicate cleanup button

`strm_generator.cleanup_duplicate_strms` stays as it is. The button's
description changes from "Removes extra .strm files from folders that
have more than one" to "Removes extra .strm files from movie folders
that have more than one. Series duplicates are handled by Repair strm
files". No test asserts on either description today.

## 4. What does not change

- The movie walker's logic, results and log lines.
- `_write_strm` still refuses to overwrite an existing file; a requeued
  episode gets a new file only because the orphan was removed first.
- No route path, method or setting. Nothing in `docs/COMPATIBILITY.md`
  moves.

## Testing

`tests/test_strm_repair_series.py`, own `_isolated_db` fixture,
`MEDIA_PATH` monkeypatched to a temp tree, `jellyfin.note_change` and
the processor faked, no network:

- A stale-host episode file is rewritten to the current proxy URL and
  Jellyfin is told; `relinked == 1`.
- An episode file with a matching token is left byte-identical; `ok == 1`.
- An orphaned episode file is removed with its `.nfo`, a wanted row
  appears with status `wanted`, Jellyfin is told; `requeued == 1`.
- Two of three files orphaned in one show: nothing is removed, no wanted
  row, `guarded == 2`, a warning is logged naming the show.
- A file without an episode tag and a show without `tvshow.nfo` are
  `skipped` and untouched.
- Three-digit episode numbers resolve.
- `repair_all_strms` returns both keys and calls each walker once.
- Source text: the scheduler job targets `repair_all_strms`; the route
  calls it.

Mutation checks on each new test. Frontend: `npx tsc --noEmit`,
`npx vitest run`, `npm run build`, the bundle committed (two
description strings change).

## Delivery

One plan, three tasks: the series walker with its tests; the entry
points, scheduler and route; the two descriptions with the rebuilt
bundle. Changelog under `### Fixed`. Released on request.

## Risks

- The guard threshold of one half is a judgement; the `arr_sync` purge
  guard uses the same fraction and has not misfired.
- A show whose `tvshow.nfo` was never written is skipped entirely, which
  is the safe direction; the NFO generator on the Maintenance tab fills
  those in.
- Rewriting a file changes its modification time. Jellyfin's realtime
  monitor may re-read it, which is what we want after an address change.
