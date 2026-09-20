# Changelog

All notable changes to Mycelium are documented in this file.

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

## [1.0.0] - 2026-09-14

### Added

- `docs/COMPATIBILITY.md`, the 1.0 compatibility promise: the frozen
  integration routes, the environment variables by tier (supported,
  advanced, deployment, internal), the deprecation rule, breaking-change
  and database guarantees, and versioning. Guarded by
  `tests/test_compatibility.py`, which fails the suite the moment the
  document and the code disagree.
- A startup warning for a deprecated environment variable still set:
  `deprecations.warn_deprecated_env()`, logged once next to the existing
  filter-migration warning. The deprecation map is empty at 1.0.
- A pre-upgrade backup and a recorded schema version. At startup, before
  any migration runs, Mycelium reads the last recorded `SCHEMA_VERSION`
  through a raw connection; when it differs from the version now booting,
  `backup.run()` takes a backup first (a failure is logged and never
  blocks startup), and the new version is recorded once startup
  completes. The startup log now reads `Mycelium <version>, database
  schema from <previous version or "fresh">`. `docs/RECOVERY.md` documents
  the upgrade and rollback procedure, with a table of what each release
  since 0.17.0 added to the database.

### Removed

- The `JELLYFIN_REFRESH_DELAY_SEC` setting. It had no effect: nothing in
  the codebase read it outside its own Settings schema field, so it never
  delayed anything. Not a deprecation (nothing to point users at) and not
  a breaking change (nothing depended on it doing something).

### Security

- `/spore-nfs/tree` and `/spore-nfs/size/<token>` now answer only a caller
  on the container's loopback interface, and the Go streaming front refuses
  to proxy the whole `/spore-nfs/` family from outside, exactly as it
  already refused `/internal/`. Both routes sit outside the login gate
  because the NFS and SMB share helpers have no session, so before this
  anyone who could reach Mycelium could list every playable token (each an
  unauthenticated capability link to a library item) and ask its file size.
  The image's start command now points both helpers at gunicorn's own
  loopback address in either streaming-front mode; a helper run outside the
  container needs `MYCELIUM_BASE` set to a loopback address of its own.
- The Go streaming front now appends its own peer address to
  X-Forwarded-For instead of passing it through untouched. Previously
  every request behind the front reached Flask as if it came from
  loopback, which made TRUSTED_PROXY_AUTH accept a forwarded admin
  username from any caller and made the login rate limiter one shared
  bucket for every visitor. The trusted-proxy header check now uses the
  real peer address unconditionally. The login rate limiter keys on the
  real client address only when the proxy sitting in front of Mycelium
  (the outer reverse proxy, or the streaming front itself) is listed in
  Settings, Security, Trusted networks; without that, it keys on the
  proxy's own address, same as before this fix. On a default install the
  outer proxy runs in its own container, so operators who want the
  limiter to key per visitor need to list that proxy's network there.
  Upgrading matters for one setup in particular: an install with the
  streaming front enabled that relied on TRUSTED_PROXY_AUTH while
  TRUSTED_PROXY_NETWORKS was left at its default accepted a forwarded
  username from everyone before this release and accepts it from nobody
  after it, because the address checked is now the real peer instead of
  loopback. Listing the network the proxy connects from in Settings,
  Security, Trusted networks restores it, and is what the setting was
  always meant to hold.
- torrentio.py no longer logs the full Torrentio request URL at INFO.
  TORRENTIO_OPTS, appended into that URL, is a config segment users paste
  from Torrentio's own configure page and can carry a debrid API key; the
  log line now carries only the imdb id and media type. The error path
  leaked the same value too: requests embeds the full URL in
  raise_for_status()'s exception text, which scrapers._redact_exc did not
  scrub; it now redacts TORRENTIO_OPTS the same way it already redacts
  the Torznab and Debridio secrets.
- /ui/logs now requires an admin session, matching its sibling
  /ui/api/logs. Any logged-in non-admin could previously read the last
  100 raw log lines, including scraper and CDN URLs, tokens and
  exception text. The rejection now answers with the same JSON shape as
  the sibling route instead of a bare 403.
- /ui/api/virtual-items (the list route) now requires an admin session.
  Tokens are unauthenticated capability URLs, so any logged-in non-admin
  could previously dump and redistribute the entire library as anonymous
  stream links.

### Fixed

- Webhook auth diagnostics (routes/integration.py) now log
  auth.peer_address() instead of request.remote_addr, which is always
  loopback behind the Go streaming front. Log-only; the secret
  comparison itself was never affected.
- Web Player: `_check_enabled()` no longer aborts 403 when auth is disabled.
  `auth.current_user_record()` is always None in that single-user no-auth
  mode, so the gate previously locked the entire Web Player out on any
  no-auth install; it now allows the request there, matching every other
  gate in the codebase.
- /ui/api/discover/search clamps `page` with admin_query.clamp_int instead
  of a bare `int()` call. `?page=x` used to raise a ValueError (a 500);
  a large value went straight to TMDB unclamped.

### Changed

- Internal: catbox.py's idle release, hourly TorBox id check and
  season-pack handling moved into their own modules, catbox_jobs.py and
  catbox_packs.py. Their log lines now carry catbox_jobs or catbox_packs
  as the logger name instead of catbox; the messages themselves are
  unchanged.
- Internal: app.py is split. Every route now lives in the `routes/`
  package, one blueprint module per area; the Flask app object, its
  extensions and its error handlers live in appcore.py; `APP_VERSION`
  lives in version.py, and app.py keeps only startup (scheduler, plugin
  loading, entrypoint). No route path, method, response shape or variable
  changed. A route table frozen before the split
  (`tests/fixtures/route_table.json`) is compared against the registered
  routes on every run, so a path that moves, gains a method or disappears
  fails the suite.
- Internal: the two long functions on the play path are split into named
  decisions. catbox.materialize's ladder became three helpers, and
  _prepare_stream's branches moved into stream_decisions.py. Same
  decisions in the same order, same responses; the split is what makes
  each branch testable on its own.
- Internal: radarr.py and sonarr.py no longer carry byte-identical copies
  of root_folders and quality_profiles. Both now come from a shared
  arr_api.py and are re-exported, so radarr.root_folders(...) and
  sonarr.root_folders(...) keep working unchanged, including the picker
  lookups that import either module by name.
- Internal: a safe-cleanup pass removed code that nothing called (dead
  functions, unused imports, an unused frontend hook) and the retired
  `EXCLUDE_DV_P5` line in config.py, which the four-state filter rules
  replaced and nothing read any more. Comments that still described the
  Jinja templates or a task number from an old plan were rewritten, and
  three misleadingly named helpers renamed. No behaviour changed.
- The documentation was swept against 0.17 through 0.29 (README, the
  install guide, docs/INTEGRATIONS.md, docs/SCALING.md). A new guard,
  `tests/test_docs_references.py`, fails the suite when a document names
  an environment variable or a route that does not exist, so a later
  removal cannot leave the docs pointing at it.
- Added `docs/superpowers/reports/2026-09-14-catbox-comparison.md`, a
  written comparison between this project's catbox mode and ElfHosted's
  CatBox, kept as background for the design decisions the play path
  rests on.

## [0.29.0] - 2026-09-14

### Added

- TorBox account pool. Several TorBox API keys can be configured in
  Settings (section Debrid, "TorBox accounts") and are used as equal
  peers: a new torrent goes to the least loaded healthy account (fewest
  torrents, budget left, no recent 429 or auth failure), every play of a
  title uses the account that holds its torrent, and a disabled or failing
  account hands its titles over on their next play. The hourly add budget
  is counted per account. The Overview's TorBox card, the health rows, the
  Library drawer and the Prometheus gauges show accounts by label. A
  single-key install is unchanged: the existing key becomes account 1.

## [0.28.0] - 2026-09-13

### Added

- Hourly TorBox id check (catbox mode). Stored TorBox ids are compared
  with TorBox's own list: an id whose torrent is gone (deleted in the
  TorBox app, expired) is cleared so the next play re-adds cleanly instead
  of discovering the loss first, and an item whose hash lives under
  another id is pointed at that one. Nothing on TorBox is deleted, an item
  that is materializing is left alone, and an empty or failing list
  changes nothing. The Overview's consistency card shows the last run.

### Changed

- Overview: the TorBox card shows the idle cleanup delay again (it read
  "-" since the redesign), a countdown under a minute reads "under 1 min"
  instead of "0 min", the activity feed only knows pills for events the
  backend actually logs, and the latency formatter is shared with the
  Scrapers page.

## [0.27.1] - 2026-09-13

### Fixed

- A pack for another season no longer counts as a season pack for the
  season being searched. Scrapers match on the series, so a season 4
  search also returned season 1 and 2 packs; those were flagged as packs
  and, with eight files and eight episodes, mapped onto season 4 in file
  order. Now a release that names a different season is not a pack for
  this one (in every scraper), and order mapping only applies to files
  that carry no episode tag at all. This affects the season swap's
  candidate list, the processor's pack choice, and the first-play
  reconciliation alike.
- Debridio results show the release name in the swap panel instead of the
  addon label, and get a source label from it like every other scraper's.

## [0.27.0] - 2026-09-13

### Added

- Whole-season release swap. Each season in the Library drawer has a
  "Swap season" button that lists the season packs the scrapers find,
  with a badge per cached pack saying which episodes it contains (from
  TorBox's file list). Picking one moves every episode the pack contains
  to it behind its existing token, with the file id set up front, registers
  wanted episodes the pack contains, and leaves episodes the pack lacks as
  they are (recorded as not in that pack). The result lists what was
  swapped, registered, skipped or busy. "Blacklist the current releases"
  blacklists every release the season was on. Requesting candidates or a
  swap with a season and no episode addresses the whole season.

### Removed

- The continue-watching priority job and its `CONTINUE_WATCHING_INTERVAL_MINUTES`
  setting. It asked Jellyfin for the current user's in-progress series with
  a server API key, which has no user, so it never found anything; the
  monitor searches every wanted episode each run regardless.

## [0.26.1] - 2026-09-13

### Fixed

- Jellyfin 12.0 support. Jellyfin 12 disables the legacy `X-Emby-Token`
  header by default, so every refresh, library lookup and health ping got
  a 401 while the Settings test still said ok (it called an endpoint that
  never needed a key). All requests now send the standard
  `Authorization: MediaBrowser Token` header, which Jellyfin 10.x accepts
  too. The Settings test and the health ping call the authenticated
  system-info endpoint when a key is configured, so a rejected key shows
  as a failure.

## [0.26.0] - 2026-09-13

### Changed

- A season request registers only the episodes its cached pack actually
  contains. TorBox lists a cached pack's files, so the processor matches
  episodes to files at request time, stores each file id up front (the
  first play of every episode skips the reconciliation), and puts the
  episodes the pack lacks on the wanted list immediately with the pack
  recorded as not containing them. Aired ones are searched right away as
  single episodes (three per request, the monitor takes the rest); unaired
  ones wait as "not aired". When TorBox cannot list the files, or lists
  files no episode can be matched to, every episode is registered as
  before and the first play reconciles.

### Fixed

- The first episode of a season pack, whose TorBox file id is 0, was sent
  through the file listing again on every play and failed to play when
  TorBox answered without a files list. A known file id of 0 is now
  treated as known.

## [0.25.4] - 2026-09-13

### Fixed

- An episode detached from a partial season pack is searched right away
  instead of waiting for the next monitor run, and that pack is remembered
  as not containing the episode (`wanted_episodes.excluded_hashes`), so
  neither the monitor's search nor a re-request of the season registers
  the episode against it again. Before, the same pack sorted first in the
  search results and would have been picked once more, detaching again on
  the next play.
- Registering an episode's `.strm` marks its wanted row found. The season
  pack path never did, so a monitored series kept every episode "wanted"
  until the next monitor run noticed the files.
- A detached episode whose air date lies in the future returns as
  "not aired" rather than "wanted", and its wanted row carries the series
  title instead of the episode's own title.
- The monitor's "is this episode on disk" check only looked in a
  `Season N` folder while the `.strm` files live in `Season 0N`, so for
  seasons 1 to 9 every registered episode was flipped back to "wanted" on
  each series check and searched again. Both folder names are checked now.
- A token that re-resolves after its release died no longer picks a pack
  that was recorded as not containing its episode.

## [0.25.3] - 2026-09-13

### Changed

- The admin Library "Wanted" view now also lists titles that are in the
  library but still missing something: series with wanted episodes and
  movies on the wanted-movies list. Before, it only showed requests whose
  own status was wanted or upcoming, so a series that lost episodes to the
  season-pack repair was invisible there.

## [0.25.2] - 2026-09-13

### Fixed

- Episodes of a season pack no longer play the wrong episode. When a pack
  did not contain an episode's file (a pack named for the whole season
  that holds only its first episodes, or files without episode tags),
  the player silently got the largest file in the pack, usually episode
  1 or 2, for every such episode. Now the first play of any episode in a
  pack matches every episode of that season to a file: names in the
  forms S01E03, S1E3, S01.E03, 1x03, E03, Ep03, Episode 3 and a leading
  episode number are recognised, untagged files map by order only when
  the pack has one file per episode, and an episode the pack does not
  contain is detached: its .strm goes, Jellyfin is told, and it returns
  to the wanted list so the monitor searches for it as its own torrent.
  Seasons already broken this way are repaired on their next play.

## [0.25.1] - 2026-09-08

### Fixed

- The Overview's scraper states no longer sit on "unknown". The page
  still never probes inline, but a stale or missing probe result now
  triggers a background refresh so the next poll shows the real state,
  the last known state is shown while that refresh runs, and the probes
  are warmed ten seconds after boot.

## [0.25.0] - 2026-09-08

### Changed

- The admin Overview is rebuilt as five bands, problems first. A status
  strip shows services, scrapers, TorBox adds, failures, the queue,
  titles needing attention and pending approvals, each with a link to
  the tab that fixes it and a glow only when something is wrong. New
  figures: plays today and this week (from the egress rows), pending
  approvals with the oldest age, titles needing attention, per-scraper
  state and latency, titles streamed in the last 15 minutes, and the
  last TorBox 429. The two TorBox cards are one card; metrics, the
  integration endpoints and the top folders are collapsed sections that
  remember their state and load only when opened. Everything the
  database can answer comes from one new `GET /ui/api/overview` call.

## [0.24.1] - 2026-09-08

### Changed

- Comet's own `language` and `resolution` Torznab attributes now feed the
  candidate instead of a title parse alone: the resolution wins when it
  names one of Mycelium's buckets, and the language codes (the same ISO
  codes the language rules use) merge with what the title says.
  MediaFusion sends neither, so nothing changes for it.
- The "required rule on an unsupported source" warning looks up each
  scraper's capabilities in the registry (`scrapers.CAPABILITIES_BY_SOURCE`)
  instead of importing a module named after the source, which the two
  Torznab scrapers never had; a guard test keeps every registered scraper
  listed.

## [0.24.0] - 2026-09-08

### Added

- Comet and MediaFusion as scrapers, read through their Torznab feeds by
  one shared adapter. Each has an enable toggle, a URL and a Test button
  in Settings > Scrapers; MediaFusion defaults to the public ElfHosted
  instance, Comet needs your own instance. They rank between Zilean and
  Torrentio, share the dedupe, outage guard and latency metrics, and show
  on the Scrapers page. Neither reports cache status; TorBox's own check
  decides, as before.

## [0.23.1] - 2026-09-08

### Fixed

- The TorBox add budget counts only uncached adds. TorBox's 60 per hour
  limit applies to torrents it does not have yet; cached adds fall under
  the general per-minute limit. Mycelium counted every add, so the
  "TorBox adds this hour" row and the Overview meter overstated usage and
  the client-side guard could refuse cached adds TorBox would have taken.
  Each add is now logged with what the cache check said, corrected by
  TorBox's own answer; the hour count, the warning threshold and the
  guard cover uncached adds, the per-minute burst guard still covers
  every add, and both figures are shown. README corrected: the limit is
  per API key, not per IP.

## [0.23.0] - 2026-09-08

### Added

- The webhook secret can be rotated from Settings. Rotate issues a new
  auto-generated secret and keeps the previous one valid for 24 hours, so
  Seerr, Radarr, Sonarr and the Jellyfin webhook plugin can be updated one
  by one; a webhook that still sends the old value is logged with its
  address and user agent. `GET /ui/api/webhook-secret` reports
  `previous_valid_until` during that window, `POST
  /ui/api/webhook-secret/rotate` does the rotation, and both refuse when
  the secret comes from the `WEBHOOK_SECRET` environment variable.

## [0.22.1] - 2026-09-08

### Fixed

- A release swap no longer waits without limit for a title that is being
  materialized for playback (the token lock can be held for up to ten
  minutes). The catbox auto-upgrader waits five seconds, then skips the
  title for that cycle; the admin swap waits ten seconds, then reports
  "busy" in the panel. Nothing changes on a timeout.

## [0.22.0] - 2026-09-08

### Changed

- The Overview egress tile counts MKV plays. Those plays are a redirect
  to the TorBox CDN, so nothing passed through Mycelium and the tile read
  near zero on an MKV library. The redirect branch now records the file
  size once per play as an estimate (a play is a redirect for a title
  not resolved in the last two hours), stored as flagged rows in
  `egress_log`; the tile shows proxied plus estimated, with the split in
  its sub-line, and the stats payload gains `egress_estimated_bytes_month`.

## [0.21.2] - 2026-09-08

### Fixed

- Arr stubs carry the source in their name again (`WEBDL-1080p`,
  `Bluray-2160p`). The tag was derived from the magnet's `dn=` name, which
  the scrapers never set, so every stub was named with the bare
  resolution; the stored release label now feeds the same detection, with
  the magnet name as the fallback. Titles processed before 0.21.1 keep the
  bare name until they are processed or swapped again.

## [0.21.1] - 2026-09-08

### Changed

- The source column now means release source. `virtual_items.source` and
  `requests.source` used to hold the scraper's name (torrentio, zilean)
  while the swap panel showed a release label (WEB-DL, BluRay, REMUX); the
  processor now writes the label or nothing, never a scraper name,
  `release_tags.source_label` is the one helper, and a one-off startup
  migration blanks a bare scraper name left over in existing rows.
- The catbox auto-upgrader replaces a release through `release_swap.swap`,
  so both paths share the token lock, the cleared RealDebrid id, the
  fast-start cache drop and the playability reset.
- A swap no longer scrapes twice: the candidate list an admin just looked
  at is kept in memory for five minutes and reused to validate the chosen
  hash. The panel itself always scrapes fresh.
- Library and Requests: a page number past the end clamps to the last page
  and the URL follows; the two read models share `admin_query.py` for
  LIKE escaping, integer clamping and the "added" windows.
- Quotas: duplicate requests by one user for the same title count once;
  disabled users appear in the Quotas card with a "disabled" tag;
  approving a request that was paused for the monthly cap clears the
  pause note.
- Settings: MultiSelect has a keyboard path (arrows, Enter, Backspace on an
  empty input, Escape); a Select whose value is not in its list shows that
  value as a disabled entry instead of a blank; a secret stored in the
  database gets a Clear button, while a value supplied through the
  environment stays; blank-field messages from the service testers use
  the schema labels.
- Admin polish: the title drawer is a proper modal dialog (focus moves in
  on open and back on close, Tab stays inside even when a click moves
  focus out of the panel first, Escape closes); the type icons are hidden
  from screen readers with the type as text; the Arr card only renders
  when the mirror is on; the bulk-action bar keeps its final "N of N
  done" line after a fully successful run; the Blacklist tab loads its
  titles in one grouped query per 400 hashes.

### Fixed

- A negative season or episode reference is rejected by both the
  candidates and the swap endpoint instead of matching nothing.
- The README no longer claims every integration has a Test button; it
  names the services that do.

## [0.21.0] - 2026-09-08

### Added

- Pick another release from the Library drawer: the Release card (movies)
  and every present episode row show the candidate list the processor
  saw, with quality, size, seeders, languages, which scrapers returned it,
  whether TorBox has it cached, and the rule that dropped a candidate.
  Choosing one swaps the release behind the existing token, so the next
  play uses it and nothing on disk changes; the old release can be
  blacklisted in the same step.

## [0.20.1] - 2026-09-08

### Fixed

- The monthly request cap can be set on the Users tab: the column is an
  inline field (blank means no cap) that saves on Enter or when you leave
  it. Until now the tab only displayed the cap, so the quotas enforced
  since 0.20.0 could not be changed from the SPA.

## [0.20.0] - 2026-09-08

### Added

- Admin Requests tab rebuilt on the Library kit: views Pending, Approved,
  Denied and All with counts; filters by user, type and date; sortable
  columns; server-side paging; Approve, inline Deny with a reason, and
  Reopen for a denied request; a Quotas card with each user's requests
  this month against their cap; the auto-approve rules moved under the
  table. Clicking a title opens the same drawer as the Library tab.
- Monthly request quotas are enforced: a user at the cap is refused with a
  clear message, and an auto-approve user at the cap has new requests
  wait for review until the month resets. Admins are never limited.
- The title drawer gains "Forget request", which drops the title from the
  Library table but keeps its files and its request history on the
  Requests tab.

### Changed

- A denied request no longer counts toward the monthly quota; pending and
  approved ones do. A paused request carries its note from creation, so it
  shows no review until an admin acts.

### Removed

- `GET /ui/api/requests/all`, unused since the Library tab.

## [0.19.0] - 2026-09-08

### Added

- Admin Library tab: every title in one table with status and failure
  reason, release, requester, playability, missing-episode and retry
  badges; saved views (Needs attention, Wanted, Queue, Incomplete series,
  Unmirrored); filters by type, status, problem, requester and date;
  sortable columns; server-side paging; a bulk action bar (retry,
  re-resolve, mirror, remove; run now and drop from queue in the Queue
  view). Clicking a title opens a drawer with everything Mycelium knows
  about it and its actions: retry, re-resolve, purge, mirror to the arr,
  blacklist a hash, reset playability, per-episode retry and series
  recheck, a per-title quality override, request history and the
  activity log.
- `activity_log.imdb_id` so the drawer can show a title's history.

### Changed

- The admin Requests tab keeps pending approvals and the auto-approve
  rules; its request list moved to the Library tab. Maintenance lost its
  per-token playability panel (the drawer replaces it); the Blacklist tab
  shows which titles used each hash; the Overview's retry-queue count links
  to the Queue view.

### Fixed

- Retrying a request no longer blanks its quality, source and hash; the
  admin Retry and Remove routes find any request, not only the 1,000 newest;
  bulk retries run at most three pipelines at a time so one click cannot
  spend the TorBox hourly add budget; the Library listing looks playability
  up through one grouped join instead of a scan per title.

## [0.18.0] - 2026-09-07

### Changed

- The setup wizard is driven by the settings schema: its steps are declared
  next to the schema, every field renders through the same kit as Settings
  (labels, help, dropdowns, pickers, Test buttons), and a re-run pre-fills
  the current values. The quality step edits the real filter rules
  (`RESOLUTION_PREFERRED`, `RESOLUTION_EXCLUDED`, `ENCODE_PREFERRED`,
  `LANGUAGE_PREFERRED`); the server-side translation of the retired
  `QUALITY_PREFERENCE`, `ALLOW_4K`, `PREFER_HEVC` and
  `AUDIO_LANGUAGE_PREFERENCE` names is gone.
- New `GET /setup/schema` and `POST /setup/picker/<name>`; `POST
  /setup/test/<kind>` also accepts a JSON body. All three share one setup
  gate, `auth.may_use_setup()`: open while nothing can log in (a first run
  or a bricked install), admin-only otherwise. A logged-in non-admin on an
  install whose wizard never completed could previously reach the save and
  test routes; it no longer can. The legacy password login counts as admin.

## [0.17.0] - 2026-09-07

### Changed

- The admin Settings tab is rebuilt around a field schema: a section
  sidebar, a label and help line on every setting, real controls
  (toggles, dropdowns, searchable multi-select, an orderable sort order,
  number fields with units, password fields with reveal), a Simple/Advanced
  switch, dependent fields that hide until their toggle is on, and a search
  box. Every integration has a Test button that uses the values as typed;
  Radarr and Sonarr root folders and quality profiles are loaded from the arr.
- New settings `RADARR_QUALITY_PROFILE` and `SONARR_QUALITY_PROFILE` (by
  name; blank keeps the first profile). `ARR_SYNC_INTERVAL_MINUTES` and
  `DISK_SYNC_INTERVAL_MINUTES` are editable in Settings (restart required).
- The setup wizard's connection tests and the old `/ui/api/arr-import/*`
  routes go through the same testers; the arr-import routes are kept as
  aliases for one release.

## [0.16.1] - 2026-09-07

### Changed

- The on-disk deletion check (a title whose `.strm` files are gone was
  deleted in Jellyfin) is its own hourly job, `disk_sync`, gated on
  `CATBOX_MODE` and `DISK_SYNC_ENABLED` rather than on the Radarr/Sonarr
  mirror. Jellyfin-only setups without the webhook plugin now converge too.
  `DISK_SYNC_INTERVAL_MINUTES` (60) sets the cadence.
- The arr reconcile counts a title whose arr is not configured (a
  single-arr setup) as skipped instead of failed.

## [0.16.0] - 2026-09-07

### Changed

- The Radarr/Sonarr reconcile is now the source of truth for deletions:
  a title Mycelium mirrored that has vanished from the arr, or whose `.strm`
  files are all gone from disk, is purged on the next run instead of being
  re-added. The delete webhooks become an optimisation; without them cleanup
  converges within `ARR_SYNC_INTERVAL_MINUTES` (default 60, was a fixed six
  hours). An absence from the arr's listing is confirmed with the arr
  before it is believed (Sonarr often lists a series without an imdb id),
  titles touched in the last ten minutes are left alone, and guards refuse
  to purge when an arr lists nothing, when more than half the mirrored
  titles vanish at once, or when the media tree is empty; refused titles
  are re-added as before. `ARR_SYNC_PURGE_ENABLED=false` keeps the mirror
  add-only. Each purge is recorded in the activity feed with its reason.
- `cleanup.rename_messy_series_folders` rolls the folder rename back when
  the path update fails, so folder and database never disagree.

## [0.15.1] - 2026-09-07

### Added

- Two health rows: "Jellyfin libraries" warns when a library on Mycelium's
  media has Trickplay or chapter-image extraction on (each pulls every file
  through the TorBox CDN), and "TorBox adds this hour" shows the add budget
  and turns amber from 45 of 60. Health rows now show their note in the
  Overview.
- The built-in manual documents Radarr, Sonarr, Maintainerr, the Jellyfin
  webhook plugin and library settings, Seerr outcome reporting, and the
  TorBox add budget for auto-requesters such as Suggestarr.

## [0.15.0] - 2026-09-07

### Added

- Stub files for Radarr and Sonarr (`ARR_STUBS_ENABLED`): a tiny fake
  `.mkv` per title in a folder the arrs scan as their root, so mirrored
  titles show as owned with the quality Mycelium found instead of Missing.
  Maintainerr's "delete files" and a manual file delete in Radarr now remove
  the title from Mycelium. A health row reports a missing mount. Needs
  `CATBOX_MODE=true` (stubs are built from Mycelium's virtual items), and a
  root folder is only required for whichever arr is actually configured, so
  a single-arr setup is not locked out.

## [0.14.4] - 2026-09-07

### Fixed

- A root folder picked in Settings > Radarr / Sonarr took effect only after
  a restart; the mirror cached the arr's defaults per process. The cache is
  now keyed on the URL and the root-folder setting.
- The delete webhook checks ownership locally before any TMDB call, so
  Jellyfin's ItemDeleted for titles in other libraries costs nothing.
- The reconcile job no longer counts a title the arr already had as added.

### Changed

- Arr API calls time out after 8 seconds instead of 15; a request can make
  several while its title lock is held.

## [0.14.3] - 2026-09-07

### Added

- Settings > Radarr / Sonarr: a Test button per arr that reports the arr's
  version, and root-folder dropdowns filled from the arr with a Load folders
  button. Both use the URL and key as typed, so you can check before saving.

## [0.14.2] - 2026-09-07

### Fixed

- Remove from library really tells Seerr now. 0.14.1 sent Seerr's "deleted"
  media status, which Seerr 3.4.1 accepts and ignores, so the title stayed
  Available until Seerr's next availability sync. The purge now removes
  Seerr's media record (what its "clear media data" button does) and the
  title is requestable again at once.
- Seerr reporting finds the title by TMDB id, so it also works for titles
  requested before 0.14.0, for series, and for Discover-originated titles
  that exist in Seerr; the stored Seerr request id is the fallback. A
  failure declines every open Seerr request for the title, not just one.

## [0.14.1] - 2026-09-07

### Fixed

- Remove from library now tells Seerr the title is gone (media marked
  Deleted), so it can be requested again at once instead of after Seerr's
  next availability sync.
- Delete and Remove from library now also clear the per-user request rows,
  which kept showing a stale Processing or Approved badge on the poster and
  a greyed-out button in the detail view after the title was removed.
- The Delete confirmation now says the title stays in Jellyfin and points at
  Remove from library.
- The first Jellyfin refresh after a host reboot was debounced away.

### Changed

- Documented Jellyfin's library monitor delay: a reported change takes about
  a minute to show, by design.

## [0.14.0] - 2026-09-06

### Added

- Radarr/Sonarr mirror (`ARR_SYNC_ENABLED`): titles Mycelium adds are created
  in the arr as monitored, search-off entries and removed on purge, with a
  six-hourly reconcile. The arrs are bookkeeping for Seerr, Maintainerr and
  calendar widgets; they never download anything.
- `/webhook/arr`: Radarr `MovieDelete`, Sonarr `SeriesDelete` and the
  Jellyfin webhook plugin's `ItemDeleted` now run the full library purge, so
  Maintainerr and deletions made in Jellyfin no longer leave rows, monitoring
  and dedup keys behind.
- Seerr outcome reporting (`SEERR_REPORT_STATUS`): success marks the media
  available immediately, a terminal failure declines the request, and a
  title wanted for longer than `SEERR_DECLINE_WANTED_AFTER_DAYS` is declined
  once. Requests no longer sit on "Processing" forever.

### Changed

- Jellyfin is told which paths changed (`/Library/Media/Updated`) after adds,
  upgrades and purges instead of being asked for a full library scan. The
  cleanup job still requests a full scan. `JELLYFIN_MEDIA_PATH` translates
  paths when the two containers mount the media differently.

### Fixed

- `/webhook/arr` no longer purges a title on a Jellyfin `ItemDeleted` echo of
  our own repair or upgrade: it now checks whether a `.strm` for the title
  still exists on disk before acting, and ignores the event if so. Arr-sourced
  events keep purging unconditionally.
- Three more `.strm` deletion sites (season-pack consolidation, repair's
  requeue, duplicate-strm cleanup) now call `jellyfin.note_change(..., "Deleted")`,
  so the targeted refresh actually knows about them.
- `seerr_report.on_failed` no longer declines a request while the retry queue
  still has an attempt left; a later successful retry could not un-decline it.
- A successful decline now clears the title's webhook dedup key, so a person
  can re-request it right away instead of waiting out the 24h window.

## [0.13.0] - 2026-09-04

### Added

- **Per-resolution size caps.** `MAX_SIZE_GB_BY_RESOLUTION` takes something like `2160p=60,1080p=15` and caps each resolution on its own, falling back to `MAX_SIZE_GB` for anything it does not name. A single global cap could not say "large 4K files are fine, but keep 1080p modest", which is the case most libraries actually want. A malformed entry is skipped with a warning rather than breaking the search, and an entry naming a resolution Mycelium never produces is dropped the same way instead of silently capping nothing.
- **A size tie-break setting.** `PREFER_SMALLER_FILES` decides which end of the size range wins once every other preference is equal. It defaults to true, which is exactly what Mycelium already did, so nothing changes unless you turn it off to prefer the largest file instead.

Both are editable in **Admin, Settings, Size & seeders** and take effect without a restart.

### Fixed

- The screenshot in the README showed an interface that no longer existed. It now shows the current one, and is a quarter of the size.

### Changed

- The type checker runs clean, and the build now fails on any new type error rather than tolerating a fixed number of them.
- Added contribution guidelines, issue templates that route security reports privately, and automated dependency updates across all five ecosystems this project uses.
- Every link in the README that still pointed at the upstream project this was forked from now points here, including an install command that named an image this project does not publish.

## [0.12.0] - 2026-09-04

### Added

- **The setup wizard creates your first admin account.** An install with authentication turned on and no account, no password and no single sign-on could reach its setup wizard but not survive it: finishing the wizard marked setup complete, and that is exactly what closes the window for creating the first administrator, leaving an install that was configured, locked, and impossible to get into. The wizard now asks for that account as its last step, but only when it is needed. Nothing changes for an install without authentication, or one that already has a way in.
- **A recovery guide** at `docs/RECOVERY.md`, covering what to do when Mycelium comes back with an empty library or the wrong data. It leads with working out whether anything was actually lost, because the incident that prompted it was a container attached to the wrong volume, where restoring a backup would have turned a remount into real data loss. It also names the window where the repair job deletes stream files it cannot match to the database, which is why the first instruction is to stop the container.

### Fixed

- **A failed restore now says so.** It redirected to the same page whether it worked or not, so a restore that did nothing was indistinguishable from one that succeeded, which is the worst way for this particular button to fail. The response also states that a restart is required, because the running app keeps reading the old database file until it is restarted.
- **The egress meter cannot silently stop reporting.** Its endpoint had to be exempted from cross-site request protection for the streaming front to reach it, and that exemption going missing produces no error anywhere: reports are refused and the usage figure quietly reads zero. Two tests now hold it in place, and a malformed report is refused rather than recorded as zero bytes.
- **The rate-limit log is pruned** instead of growing forever. It stores its timestamps as numbers rather than dates, so it needed its own pass; the shared one would have deleted every row rather than the old ones.
- Tightened the first-admin path exemption to an exact match, replaced a deprecated timestamp call in the watchdog, and covered the single sign-on branch of the credential check that had no test.

## [0.11.1] - 2026-09-03

### Changed

- **The native hash index syncs only what is new.** It downloaded the full 1.45 GB community hashlist snapshot on every run, four times a day, to find the pages that had been added since the last one. Upstream adds roughly 37 pages a day, so a six hour window needs about nine of them: that is around 160 MB of transfer per page discovered, close to 6 GB a day. The sync now lists the repository in one request and fetches only the pages it has not indexed, which is a few hundred KB for a normal run. This is safe because the repository is append-only, verified against 40 consecutive upstream commits: every one added a page and none modified one, so a page indexed once never has to be read again. The full snapshot is still used where it genuinely costs less, for the first backfill or a long gap, and a failure while listing now retries on the next run instead of falling back to the 1.45 GB download.

## [0.11.0] - 2026-09-02

### Added

- **Monthly proxied egress on the admin Overview.** TorBox bans permanently after three bandwidth warnings, and nothing measured how close you were. The Go streaming front now reports the bytes it served for each stream over a loopback-only endpoint, and the Overview shows the running month against your plan's floor. The tile is labelled "Proxied egress" deliberately: MKV titles redirect straight to the CDN and never pass through the proxy, so the figure is a floor, not a total.

### Fixed

- **The watchdog alerts when the database is empty but the library is not.** On 2 September production lost its entire database and nothing fired, because the deadman check only measured the age of the last successful add: with no activity rows at all it returned early. It now alerts when there are zero library items in the database while `.strm` files still exist on disk, which is the signature of a database pointed at the wrong volume. Keying on the media tree rather than a settings row matters, because a settings row is lost along with the database it was meant to detect.
- **Resolved CDN links no longer outlive TorBox's window.** TorBox opens a returned link for three hours; Mycelium cached them for twenty-three, so a cached link could be dead before it was ever used. The cache now expires at two and a half hours, inside the provider's window. The liveness check and re-resolve path stay as the backstop for links that die early.
- **A fresh install with authentication enabled can reach its setup wizard.** With `AUTH_ENABLED=true` and no users, no password hash and no single sign-on, the wizard sat behind a login that could never succeed. The setup surface is now reachable while, and only while, no credential exists at all. Note the remaining limitation: completing the wizard marks setup finished, which closes the first-admin window, so that path still needs a first-admin step before it is usable end to end.
- **The setup endpoints refuse anonymous writes on a configured install.** `/setup/save` and `/setup/skip` checked only whether users existed, so an install whose last user had been deleted could be written to without authenticating. They now carry the same completed-setup guard their sibling already had.
- **The egress reports were being rejected silently.** The new reporting endpoint was subject to global CSRF protection while the streaming front, a machine caller on loopback, sends no token. Every report would have been refused with the front ignoring the status, leaving the tile reading zero forever. The endpoint is now exempt like the existing webhooks, and the front logs any rejected report.
- **Authentication no longer costs a database read on every stream request.** The credential check introduced with the setup carve-out ran before the cheap path test, so every playback and every internal resolve paid a query against the same database the egress meter writes to.
- **The egress log is pruned** with the other volatile tables rather than growing without limit.

## [0.10.3] - 2026-09-02

### Added

- **The running version is shown in the app footer.** It was embedded in every page all along (the `app-version` meta tag) but only the login page displayed it, so once you were signed in there was no way to tell which build you were on.

### Fixed

- **`DEBRIDIO_CONFIG_TOKEN` no longer bypasses the TorBox-key privacy default.** The key is stripped by value, at any depth and under any field name, so a renamed schema field (the very case the override exists for) cannot smuggle it out; both base64 alphabets are decoded, since a URL-path blob has good reason to be URL-safe. The override is used verbatim by design, which meant it also skipped the 0.10.0 change that stopped sending your TorBox key to Debridio - and any blob built before that change carries one, so the single setting meant as an escape hatch silently re-enabled the key sharing everything else now avoids. A `providerKey` inside the override is now dropped (unless `DEBRIDIO_SEND_TORBOX_KEY=true`), every other field is preserved untouched, and a blob that is not base64 JSON - the real "they changed something we do not understand" case - is still passed through verbatim with a warning.
- **The admin Releases tab announces the current version again.** `releases.json` is hand-maintained and had silently fallen six releases behind, still calling 0.8.4 the newest while 0.10.2 shipped. Backfilled 0.8.5 through 0.10.2, and a test now fails any release whose version has no entry, so it cannot drift again.

## [0.10.2] - 2026-09-01

### Fixed

- **`/login` sends you to the app when auth is disabled**, instead of rendering a login page with no password form and no SSO button. That dead end read as "login is broken" when the real story was "this deployment has no auth configured" - `is_admin()` already grants full access in that mode.
- **Native-mode Zilean works again.** The built-in SQLite index (`ZILEAN_MODE=native`) has no URL by design, but the health gate required one: the enabled, working native index was silently skipped for every single search, and both status surfaces showed it as down or disabled. Native mode now probes the local index itself (down only when the index database cannot be opened), searches flow through it, and the Overview health card reports it with its hash count.
- **The admin Scrapers page shows every scraper again.** It sourced its list from the traffic-routing filter, which hid exactly the scrapers whose status matters: disabled ones showed nothing at all, and a scraper whose health probe failed vanished from the page instead of showing "down". All scrapers are now listed - disabled ones as such - and when the process has no latency samples yet (they are in-memory, so every restart clears them) the live health probe stands in instead of a permanent "unknown".

## [0.10.1] - 2026-08-31

### Fixed

- **A rate-limited CDN no longer produces fake tiny streams or truncated 206s.** Both found by running `scripts/loadtest-streams.sh` against production: (1) the cold path's file-size HEAD never checked the HTTP status, so under a TorBox CDN 429 storm the 162-byte error page's Content-Length was cached as the file size and clients received a 162-byte "movie" with a success status - the resolve now validates the status, retries a 429 once, never caches failures, and answers 503/502 honestly; (2) the Go front wrote its 206 headers before the first CDN fetch, so a fetch that failed outright became a success status followed by a zero-byte body (a 520 behind Cloudflare, a "corrupt file" player error without it) - the first byte source is now secured before any headers go out, turning dead-on-arrival streams into a 503 with Retry-After (rate limit) or a 502, while ranges served from the cached header still answer even with the CDN down. Mid-stream drops now also resume from the exact byte where the transfer died.
- **The load-test script no longer reports failure after a successful run** (`set -e` was live inside its cleanup trap, where `kill` on already-finished streams returns nonzero).

- **Debridio error logs keep their diagnostic tail.** The redaction that scrubs the config token from URLs was greedy: "/" and alphanumerics are all valid base64, so it swallowed everything up to the next non-base64 character and logged `.../<config>:2:1.json` - media type and IMDB id destroyed, which cost real diagnosis time during a transient Debridio 500. Tokens followed by known path segments now redact to `/<config>/stream/series/tt...`; the greedy pattern stays as the backstop for tokens in any other position. (The 500 itself was Debridio-side and transient: all four providerKey shapes, the deployed keyless one included, return identical results on the same series request. The health gate skips a down scraper for 60 seconds and then re-probes, so no action was needed.)

## [0.10.0] - 2026-08-31

### Added

- **`scripts/loadtest-streams.sh`**: a dependency-free concurrent-stream load test. Opens N rate-limited streams against one warm token and measures `/health` latency while they run, which is the number that actually matters: whether the rest of the app stays responsive under streaming load. Includes a before/after recipe against `STREAM_FRONT_ENABLED=false`.
- **Playability panel in admin Maintenance**, closing two long-standing open points: the playability-state table (items that failed to materialize 3+ times in a row, with failure counts, reasons and last-known-good provider) and a per-item Re-resolve button - previously only reachable with curl against `/ui/api/virtual-items/<token>/re-resolve`.

### Removed

- **The Jinja UI is gone.** The React SPA has been the default since 0.9.0 and a full audit had already confirmed every control was rebuilt; maintaining two UIs meant building every feature twice. Deleted: the three templates (login, setup wizard, admin dashboard), the `/login/classic`, `/setup/classic` and `/admin/classic` escape hatches, the `UI_V2` flag (the SPA is simply the UI now), twelve form-POST routes that no surface had called since the redesign, and every `flash()` call - nothing rendered those messages any more, so they would have accumulated unread in the session cookie forever.

### Changed

- **Debridio is queried as a plain scraper; your TorBox key is no longer sent to it.** The addon's config segment carried the key, but it never validated it: probing the live addon returned byte-identical results with the key present, omitted, blanked or filled with nonsense (538 streams, 538 hashes, 197 cached flags, in every shape). It was only used to build play URLs that Mycelium discards, since it keeps the info hash and resolves every release through its own TorBox client, exactly as it already did for Torrentio and Zilean. All three scrapers now see no debrid credentials at all. `DEBRIDIO_SEND_TORBOX_KEY=true` restores the old behaviour if a future addon version starts requiring it.

### Fixed

- **Preferences saved by the shared-password login persist now.** The legacy single-user login has no users-table row (its session record is a synthetic shim with id 0), so saving UI preferences or plugin toggles matched zero rows and silently reverted - the same bug class as the 0.8.3 region fix, on the two endpoints that fix recorded as follow-ups. They now persist in a settings blob the login's record reads back, so the session payload, plugin fields and webplayer checks all see them.

### Added

- **SECURITY.md**: private vulnerability reporting via GitHub, supported-version policy, and deployment scope notes (what is unauthenticated by design and why).

## [0.9.0] - 2026-08-31

### Changed

- **The React UI is the default** (`UI_V2` now defaults to true). The Jinja views stay reachable: `UI_V2=false` is the escape hatch, and `/login/classic`, `/setup/classic` and `/admin/classic` keep working either way.
- **The admin System health card shows which process serves streams.** The Go front stamps every request it proxies, so the row is live truth rather than an env echo: "Go (active)" or "Python fallback". The toggle itself is the `STREAM_FRONT_ENABLED` environment variable and takes effect on container restart.
- **Streaming moves to a Go front process.** The container's exposed port is now owned by `spore-stream`, a small Go server that serves `/spore-stream/<token>` itself and transparently reverse-proxies every other request to gunicorn. Each open stream costs a goroutine instead of one of gunicorn's OS threads, so the previous hard ceiling on simultaneous streams (the thread pool) is gone and gunicorn's threads serve only short-lived requests. Python keeps every decision: a new loopback-only `/internal/stream-resolve/<token>` endpoint runs materialize, the TorBox budget, CDN liveness checks and background cache builds, and tells the front to redirect (MKV), pass a CDN range through (cold), or serve the moov-first virtual layout from the `.fsh` cache (warm). The byte math is pinned by golden fixtures generated by the Python implementation and verified byte-for-byte by the Go tests, and the Go suite now gates CI and releases. Set `STREAM_FRONT_ENABLED=false` to revert to the previous architecture: gunicorn binds the exposed port directly and its own Flask streaming route, which remains a complete implementation, serves the streams.

## [0.8.6] - 2026-08-31

### Changed

- **The stats overview is computed from aggregate SQL and cached for 60 seconds.** It loaded four entire tables into Python and walked the whole media tree twice per call, and the admin Overview polls it every 30 seconds; all pollers now share one cached result. Two displayed numbers get more accurate along the way: the total request count and the quality histogram were both silently capped at the 1,000 most recent requests and now cover everything.
- **`virtual_items` is indexed on the columns it is filtered on** (`imdb_id` plus `media_type`, and `info_hash`), and `media_items` on `media_type`. Several repair and reconcile paths ran full-table scans per item without these.
- **Wanted-episode reconciliation is batched and debounced.** The connection runs in autocommit mode, so its per-episode UPDATE loop took the writer lock once per episode inside a GET handler; the loop now runs inside one transaction. Both reconcile functions also run at most once per minute no matter how many users are loading the Library, since they are repair work rather than something a page load needs freshly computed.
- **The series-episodes endpoint caches its response for 30 seconds** instead of walking every show and season folder on disk for every viewer, and a purge invalidates the cache immediately so removed titles do not linger.
- **The strm repair job scales linearly.** Pass 2 checked each `.strm` file's token with its own SELECT; it now checks against one snapshot query, falling back to the database on a miss so tokens created mid-run are not misread as orphaned. Pass 1 re-listed the entire library root for every folder that had lost its `.strm`, which went quadratic when a batch broke at once; it now consults a sibling map built in a single pass.
- **The serving thread pool grows from 16 to 64** (overridable with `GUNICORN_THREADS`). Every proxied stream holds its thread for the whole transfer, so the thread count is the hard ceiling on simultaneous open streams; 16 was the ceiling on everything, health checks included. Workers stay pinned at 1, and the reasons (single-flight locks, the scan-burst detector, the login rate limiter's in-memory counters) are now documented at the gunicorn line so a future "add workers" change trips over them.
- **The TorBox createtorrent budget is enforced through the database.** The 60/hour guard lived in a per-process deque; the check-and-reserve now happens in one immediate SQLite transaction, so the budget holds across threads and across processes, and adding workers can never multiply the local guard into N independent counters discovering TorBox's real limit the hard way.
- **The TorBox library-list cache refreshes single-flight.** At TTL expiry every concurrent caller used to independently run the up-to-20-page fetch; now one caller pays it while the rest wait for the result or briefly serve the stale copy.
- **The frontend is code-split.** Each page loads as its own chunk, the ten-tab admin tree only downloads when an admin opens it, and the vendor stack and the webplayer's hls.js sit in their own chunks that stay browser-cached across releases. The entry chunk drops from 949KB to 27KB.
- **The admin all-requests table renders 50 rows per page** instead of mounting up to 5,000 table rows in the DOM at once.

## [0.8.5] - 2026-08-31

### Changed

- **The Library's movie list is paged by the server.** The endpoint used to fetch the 10,000 most recent requests, filter and deduplicate them in Python, and ship the whole result to the browser on every visit, where search and paging happened client-side. Past 10,000 movies the rest of the library silently never appeared. Search, the All/Available/Wanted filter and paging now run in SQL (with a new index on media type and creation date), the response carries one 24-item page plus the toolbar counts, and the Jellyfin id prefetch asks only for the movies on screen instead of building a query string from the entire library.
- **The cleanup repair loop no longer sleeps after healthy files.** It paused two seconds after every `.strm` it scanned, even when the file was fine and no API had been called; on a large library that pause alone stretched a run into hours while the maintenance lock was held. The pause now applies only when a repair attempt actually ran.
- **RealDebrid on-play waits are capped at 45 seconds**, the same cap the TorBox path already had. They inherited the 600-second request-time default, so a single play of an uncached RealDebrid item could hold one of the serving threads for ten minutes.
- **The failed-requests panel polls lighter.** Its endpoint fetched the 500 most recent requests of any status on every poll and filtered in Python; it now asks SQL for failed rows only, over an index that already existed. The poll interval goes from ten to thirty seconds, since every logged-in user with My Requests open runs it.
- **The sidebar's wanted count is a COUNT(*)**, replacing a load of the entire wanted-episodes table on every page navigation.

## [0.8.4] - 2026-08-31

### Fixed

- **The TorBox Usage card works again.** An audit of what the redesign left behind found this was the one live card of the old dashboard that had not been rebuilt: the new Overview's TorBox tile carries only item count and total size, while the old card also showed the plan and a per-download-state breakdown. The old card had in fact stopped working before the redesign began - only its "Loading..." placeholder survived in the template, its filler script having been lost at some point - so `/ui/api/torbox-usage` has been serving a card nobody could read. The React Overview now renders it properly.

## [0.8.3] - 2026-08-31

### Fixed

- **The region picker never saved for the shared-password login, and it lied about it.** That login type's user record is a synthetic shim with id 0; `db.update_user(0, ...)` matched no rows, and the endpoint returned `ok: true` anyway, so the picker appeared to work and silently reverted. Legacy-mode saves now persist as a runtime setting (`LEGACY_USER_REGION`, legacy mode is single-user by definition) and survive restarts; per-user logins keep the user-row path. The same id-0 shim silently no-ops `me/preferences` and `me/plugin-fields` too; those are recorded as a follow-up, same bug class.
- **The default region is United States**, end to end: the session payload's fallback chain is now saved row value, then the legacy setting, then US. The previous fallback was hardcoded `NL` server-side, which also made any frontend-only default change dead code.
- **Toast notifications return.** The redesign dropped the old dashboard's corner toasts on the reasoning that the activity feed covered them; in use it did not. A toast system now confirms the main actions everywhere (request submitted, added to watchlist, sync started with its count, delete and purge and retry) and surfaces failures, and the admin shows live activity toasts again, polling only while the admin is open.
- **The logo mark is back beside the wordmark** in the sidebar and on the login page, as the mockup drew it.

## [0.8.2] - 2026-08-31

### Fixed

- **The admin Overview's integration endpoints card is complete again.** The 0.8.0 port carried only the webhook secret; the Seerr webhook URL, the TorBox push notification URL and the catbox stream prefix now render above it, each with a Copy button, built from the page's own origin exactly as the old dashboard built them.
- **The old dashboard's library tiles are back on the Overview**: Movies, Episodes, Series and Wanted, all from the stats payload the page already fetched.
- **The Library series list is searchable**, matching the movies panel.

### Changed

- **Releases is its own admin tab** rather than a panel at the bottom of Overview.

## [0.8.1] - 2026-08-31

### Fixed

- **Discover rendered briefly and then went black.** The trending hero added in 0.8.0 and the trending row it sits above both use the React Query key `['trending', 'all', 'week']`, but their fetchers returned different shapes: the row unwrapped `.results` into an array while the hero cached the whole `{results: [...]}` envelope. React Query keeps one entry per key, so whichever query resolved first decided what the other received, and when the hero won, the row was handed an object to list. `PosterGrid`'s guard let it through, because an object is truthy and its `length` is `undefined` rather than `0`, and `ScrollStrip` then called `.map` on it: `TypeError: s.map is not a function`, which took the whole page down after first paint. The hero now unwraps `.results` like every other consumer of that key.
- `PosterGrid` checks `Array.isArray` rather than truthiness, so a shape mismatch degrades to the empty state instead of a blank page. Every test in 0.8.0 passed because the hero was only ever rendered on its own with a mocked payload; a new test renders it alongside a trending row under one query client, which is the arrangement that actually breaks.

## [0.8.0] - 2026-08-30

### Changed

- **The entire web UI is redesigned** after the AIOStreams visual language: near-black ground, purple accent, Inter and JetBrains Mono self-hosted (no Google Fonts requests), SVG navigation icons, and a component system (pills, chips, stat tiles, tables) shared by every screen. 146 component tests where the frontend previously had none.
- **Every SPA screen relaid out.** Discover opens with a full-bleed trending hero with a working watchlist action; Library gains stat tiles, filter chips and a grid/table toggle; Watchlist shows Trakt and MDBList source cards with one-click sync; Search renders result rows with overviews, status pills and client-side facets; Requests shows the monthly quota card and approval tiles; Wanted shows movies and episodes side by side with attempt-count colour ramps, a retry-all button, and previously invisible given-up episodes surfaced; Settings splits into Integrations and Preferences.
- **The admin is native.** The 2,297-line embedded Jinja dashboard and the half-wired React admin are replaced by one nine-tab React admin (Overview, Users, Requests, Filter rules, Scrapers, Logs, Maintenance, Blacklist, Settings). A 93-control inventory of both old surfaces was discharged control-by-control; nothing was dropped without a recorded reason. The filter rules editor is a behaviour-proven port of the existing chip editor. The Overview tiles show only real numbers; the mockup's invented metrics (active streams, cache hit rate) were deliberately not faked.
- The admin dashboard no longer ever reloads itself; every panel refreshes in place, and polling stops when its tab is hidden.

### Added

- `GET /ui/api/shell-summary` (sidebar counts, TorBox state), `GET /ui/api/me/quota`, `GET /ui/api/scraper-health` (rolling per-scraper latency from a new in-process ring buffer), `GET /ui/api/logs` (structured, level-filtered), `GET /ui/api/releases`, `GET /ui/api/repair`.
- **`UI_V2` environment variable** (default off). When set, `/login`, `/setup` and `/admin` serve the redesigned React pages; the Jinja versions stay reachable at `/login/classic`, `/setup/classic` and `/admin/classic` regardless, as permanent escape hatches. With it unset, those three routes behave byte-identically to 0.7.7. The auth endpoints themselves are untouched: the React login submits the same form POST to the same `/login`.
- The React setup wizard ports all ten steps and every field of the original, verified field-by-field.

### Fixed

- **Setup wizard quality and language choices now apply.** Since the filter-rules model landed, the wizard's `QUALITY_PREFERENCE`, `ALLOW_4K`, `PREFER_HEVC` and `AUDIO_LANGUAGE_PREFERENCE` fields were silently dropped by the save handler's allow-list; nothing translated them into the rule model, so a fresh install's choices did nothing. `setup_save` now translates them through the same mapping the one-time migration uses (`RESOLUTION_PREFERRED`, `RESOLUTION_EXCLUDED`, `ENCODE_PREFERRED`, `LANGUAGE_PREFERRED`). Applies to both the old and new wizard.
- Approving a request in the admin now refreshes the all-requests table beneath it.
- The admin nav item is hidden from non-admin users again (a regression the redesign's own review caught and fixed before release).

## [0.7.7] - 2026-08-30

### Fixed

- **The admin dashboard reloaded itself every two minutes.** `templates/ui.html` ran `setInterval(() => location.reload(), 120000)`, gated on ten seconds of idleness that only `click` and `keydown` reset. Scrolling and reading never counted as activity, so ten seconds after your last click you were permanently idle and the reload was effectively unconditional. It destroyed scroll position, the open tab, expanded sections and any half-typed input across the whole page. Three of the four timers on that page already patch the DOM in place; the reload existed for one thing only, the repair half of the Maintenance tab, which is server-rendered and had no JSON endpoint behind it. Everything else the reload kept current is either already patched (`refreshHealth`, `refreshOverview`, `pollActivity`) or cannot change without a redeploy (`releases`, `config`, `app_version`).
- The new `GET /ui/api/repair` returns the last cleanup run and the repair items, and `refreshRepair()` patches both blocks in place on the same two-minute timer and the same idle gate. The cadence is unchanged, so no data goes staler; only the page-destroying part is gone.
- The patcher re-applies whatever is typed in the repair search box, because replacing the table rows drops the `display:none` the filter had set on them; without that, searching would silently stop working after the first refresh. It also escapes what it interpolates, which Jinja had been doing for free and hand-built HTML does not: titles, paths and reasons come from torrent names and the filesystem.
- The endpoint is admin-only, since repair items carry filesystem paths and the tab that renders them is behind the admin gate already.

## [0.7.6] - 2026-08-30

### Fixed

- **The NFO title repair never repaired anything.** `repair_tvshow_titles()` guarded with `if not title_el`, but an ElementTree Element's truth value is its *child count*, not whether the find succeeded. `<title>Season 01</title>` has no child elements, so the guard was true for every file and the loop skipped all of them; the function returned `{"fixed": 0}` regardless of what the library contained. Python emits a `DeprecationWarning` about precisely this. `is None` is the correct check. This means the repair has never worked in any version, which is also why the underlying "Season 01" bug survived long enough to be worked around twice.
- **The repair now covers movies as well as series.** The write bug could not reach them - a movie's NFO sits in the movie's own folder, so `_write_nfo` derived its title from the right directory, and a test pins that - but a repair that inspects only half the library cannot report the other half is clean. A movie is rewritten as a `<movie>` document keeping its `<year>`, never as a `<tvshow>`, which would break Jellyfin matching. The Maintenance button is relabelled "Fix library titles" accordingly.

## [0.7.5] - 2026-08-30

### Fixed

- **The retry queue never drained.** Three separate paths let it grow without limit. `processor.process()` re-queued on a mutex miss by calling `db.enqueue_retry` directly instead of going through `retry_queue`, which skipped `schedule()`'s give-up check and passed the attempt count unchanged rather than `+1` - so a title that kept colliding re-queued every 60 seconds indefinitely with its progress toward being abandoned frozen at zero. `run_due()` bails when the TorBox createtorrent budget is nearly spent, leaving rows due but unprocessed, and a row that is never processed never increments its attempt either, so under sustained budget pressure the queue only grew. And `retry_queue` had no `UNIQUE` on `imdb_id` and appeared in no prune target, so a single title could hold many rows and nothing ever removed them. Observed on a live instance as 8 rows for 2 titles, 7 of them the same title - more than `schedule()` alone can produce, which is the signature of the collision path specifically.
- A mutex miss now goes through `requeue_after_collision()`, capped at `MAX_COLLISION_REQUEUES` and deliberately not raising `attempt`, because a scheduling collision is not a failed attempt. The count is held in memory rather than on the row: the row is deleted the moment the retry fires, so a column would reset every cycle and never reach the cap, and the locks it guards are in-process too, so a restart legitimately clears both.
- `enqueue_retry()` is now an upsert against a unique `imdb_id`, taking `MAX(attempt)` so a collision re-queue cannot reset a title's progress. The migration collapses existing duplicates first, keeping the furthest-along attempt, since the unique index cannot be created while duplicates exist and a failed migration means the container does not boot.
- Rows older than a week are pruned twice daily.

### Added

- **A "Clear retry queue" button** in Maintenance. The dashboard already displayed the pending retries but offered no way to act on them.

## [0.7.4] - 2026-08-30

### Fixed

- **Mycelium imported the entire TorBox account unprompted.** `strm_generator.run_once()` in non-catbox mode walked the whole TorBox mylist and wrote a `.strm` for every torrent in it. That is what the "Import TorBox library" button is for, but it also ran on a timer every hour (`STRM_GENERATOR_INTERVAL_HOURS` defaults to 1) and again 30 seconds after every boot, so an account holding content from before Mycelium was installed, or added elsewhere, grew a library nobody asked for. Worse, `process_torrent()` recorded nothing: no request row, and in fixed mode no `virtual_items` row either, so the result played in Jellyfin but appeared in neither the Requests nor the Library tab and could not be removed. `run_once()` and `run_and_refresh()` now take `import_unknown`, and the two unattended call sites pass `False`; deliberate triggers (the button, the TorBox push webhook, post-add, cleanup, recovery) are unchanged. This does not affect fixed-mode URL freshness: `_write_strm()` returns early when the path exists, so the hourly run never refreshed an expiring CDN URL - importing was its only effect. `process_torrent()` also now records a request row for what it materialises when the caller knows the imdb_id, filling a gap without disturbing a request the processor already owns.
- **Every series was named "Season 01" in Jellyfin.** `_write_nfo()` derived the title from `strm_path.parent.name`. For a movie that is the movie's own folder and correct; for an episode it is the SEASON folder, and `tvshow.nfo` is written from an episode path, so every series got `<title>Season 01</title>` and Jellyfin displayed exactly that. The title now comes from the folder the NFO itself sits in. This had been worked around twice without being fixed - `repair_tvshow_titles()` rewrites the bad files afterwards, and `generate_missing_nfos()` refuses to write a "Season XX" string - but neither ran at write time, so every newly added series reproduced it. Existing files are untouched; the new button below repairs them.
- **The retired filter settings were still editable in the admin UI.** 0.7.0 replaced the twelve booleans with the rule model and nothing reads the old keys, but `SETTING_GROUPS` kept listing `QUALITY_PREFERENCE`, `ALLOW_4K`, `EXCLUDE_REMUX`, `EXCLUDE_BLURAY`, `EXCLUDE_CAM`, `PREFER_WEBDL`, `PREFER_HEVC`, `STRICT_NO_CAM`, `AUDIO_LANGUAGE_PREFERENCE` and `EXCLUDE_LANGUAGES`. Toggling one saved a value no code path consults, which reads as a filter being in force when it is not. Both affected groups are renamed to what they now hold: "Quality & filtering" becomes "Size & seeders", "Languages & subtitles" becomes "Subtitles". A test ties the settings page to `migrate_filters.RETIRED` so this cannot drift again.

### Added

- **A "Fix series titles" button** in Maintenance. `repair_tvshow_titles()` and its endpoint already existed but nothing linked to them, so the only way to repair a library full of shows named "Season 01" was to call the URL by hand. A second test walks every literal `/ui/` URL the admin page fetches and asserts `app.py` defines it, so a renamed route fails the suite rather than leaving a button that breaks only when clicked.

## [0.7.3] - 2026-08-30

### Fixed

- **"Remove from library" left the title on screen.** The purge deleted the `.strm` files and their per-episode `.nfo`, and nothing else - but `nfo_generator` also writes the episode still (`<episode>-thumb.jpg`), `poster.jpg`/`fanart.jpg` in both the season folder and the series root, and `tvshow.nfo` in the series root. All of it survived, with two consequences: the folders were never empty so the `rmdir` sweep quietly left the whole tree in place, and Jellyfin, scanning a series folder that still held a valid `tvshow.nfo` and artwork, kept the show in the library. From the outside the button looked like it had done nothing, even though every `.strm` was already gone. The purge now clears those sidecars and removes the folders, deleting only filenames this project writes and skipping any folder that still holds a `.strm`, so a sibling title's files - and anything you put there yourself - keep both their artwork and their directory.
- **A purge could delete the files and never tell Jellyfin.** `jellyfin.refresh_library()` debounces for 60 seconds so that bulk `.strm` generation does not hammer the server, but that also swallowed the refresh for a purge landing shortly after an unrelated one - observed live, 50 seconds after a scheduled refresh. A purge now forces the scan, and both skip paths log at info rather than debug, since the entire effect of a skip is something visibly not happening.
- **`spore-nfs` and `spore-smb` no longer log every refresh.** Each printed `tree refreshed: N files, M dirs` roughly every ten seconds, from two processes, indefinitely - around seventeen thousand identical lines a day. With the compose default of `max-size 10m` / `max-file 5` that rotates real diagnostic history out of the log, and it actively obstructed diagnosing the bug above. Both now log only when the count changes, which also turns the log into a usable record of when the library actually moved.

**Note:** the folder fix applies to future removals. A title purged by an earlier version has already left its artwork behind; those folders contain no `.strm` and can be deleted directly.

## [0.7.2] - 2026-08-30

### Added

- **`PUID` / `PGID` support.** Mycelium writes the `.strm` files that Jellyfin and Plex read and delete. Deleting a file needs write permission on its parent *directory*, so all three have to agree on a user id - and running as root meant Mycelium owned everything it created and Jellyfin could not remove any of it from its web UI. That id is a property of the deployment rather than of the image, so it arrives at runtime through the same two variables Jellyfin and Plex already use, instead of a `USER` line in the Dockerfile. The new entrypoint corrects ownership of the data directories and then drops to `PUID:PGID`. `PUID=0` is the default and execs straight through, so nothing changes for anyone who does not set it.
- The recursive ownership pass is guarded by a marker named `.ownership-<uid>-<gid>`: it runs once, re-runs when the ids change rather than leaving half the tree owned by the old id, and is skipped on every normal restart, where a walk over a large library would otherwise cost minutes on each boot. A chown that fails deliberately does not write the marker, so a broken permission state retries on the next start instead of booting into a database it cannot write. `FORCE_CHOWN=1` repeats the pass on demand.
- Setting `user:` in Compose achieves the same end result, but leaves the operator to fix existing ownership by hand and to remember the setting on every deployment - and a platform that regenerates its Compose file can drop it with no visible symptom beyond new files quietly reverting to root. `PUID`/`PGID` is ordinary configuration, which is the thing such platforms carry correctly.

### Fixed

- **`spore-smb` keeps its port under a non-zero `PUID`.** Port 445 is below 1024 and so normally requires root to bind, which would have silently cost the SMB share to anyone adopting `PUID`. The binary now carries `cap_net_bind_service`, which works at any user id on any host without asking operators to set sysctls or capabilities per deployment. Applied after the `COPY` because capabilities live in the file's extended attributes. Verified not to disturb the default path: the binary still loads normally under the secure-execution mode that file capabilities enable.
- **A dead `spore-nfs` or `spore-smb` is no longer silent.** Both were backgrounded while only gunicorn was `exec`'d, so either could fail at startup or die days later with nothing logged and nothing restarted - the share simply was not there, with no error to search for. Their exits are now reported.

## [0.7.1] - 2026-08-30

### Fixed

- **Deleting a request now actually deletes it.** Delete removed only the `requests` row, leaving behind three things it had created, each of which then poisoned the next request for the same title: the `webhook_events` dedup key (which lives 24h, so re-requesting the title was answered `duplicate` and silently never processed), the `.strm` files and `virtual_items` rows, and the `retry_queue` row (so a deleted request reappeared at the next backoff interval). `db.delete_request()` now clears the dedup keys and queued retries with the row.
- **A re-requested series is no longer marked failed while its episodes sit in the library.** `strm_generator.create_lazy_episode_strm()` returns `False` both when the episode is already registered and when the write genuinely failed, and `processor._lazy_register_season()` treated both as failure. So every season of a re-requested series returned `False`, the request was marked `failed`, and a retry was queued that could never accomplish anything - while Jellyfin and Plex had the whole show. The movie path had been fixed for this already (`_lazy_register_movie`: "strm already exists - still a success"); the series path never was. The new `_already_registered()` asks the database whether the episode has a `virtual_item`, which is the only way to tell "already there" from "write failed" - a disk error is still reported as a failure rather than as a healthy library.

### Added

- **A second "Remove from library" button** beside Delete, in both the classic admin Requests tab and the SPA (admin-only there). Delete keeps its existing meaning: forget the request record, keep the files. Remove from library additionally deletes the `.strm` files, their `.nfo`, the Spore stubs, the `virtual_items` rows and the monitoring rows that would regenerate them, then prunes the emptied folders and refreshes Jellyfin. Folder pruning uses `rmdir` only and never walks above `MEDIA_PATH`, so a directory still holding another title is left exactly as it was.

## [0.7.0] - 2026-08-30

### Changed

- **Release filtering replaced with a four-state rule model.** The twelve boolean filters (`ALLOW_4K`, `EXCLUDE_REMUX`, `EXCLUDE_BLURAY`, `EXCLUDE_CAM`, `STRICT_NO_CAM`, `EXCLUDE_DV_P5`, `PREFER_WEBDL`, `PREFER_HEVC`, `QUALITY_PREFERENCE`, `AUDIO_LANGUAGE_PREFERENCE`, `EXCLUDE_LANGUAGES`, plus the coupling in `EXCLUDE_UNDERSIZED_RELEASES`) are retired in favor of 37 new settings across seven categories - `RESOLUTION`, `SOURCE`, `ENCODE`, `VISUAL_TAG`, `AUDIO_TAG`, `AUDIO_CHANNELS`, `LANGUAGE` - each with its own `_PREFERRED`, `_EXCLUDED`, `_REQUIRED`, `_INCLUDED` and `_STRICT` setting, plus `SORT_ORDER` and the internal `FILTER_RULES_MIGRATED` marker. `preferred` is tie-break only and never rescues a candidate another rule dropped; `included` overrides every other rule in every category, including a `_REQUIRED`/`_EXCLUDED` rule on an unrelated category, so it is the one setting a user can seriously surprise themselves with.
- **Evaluation is order-independent.** Every category now votes against the full candidate pool independently, and the drops are unioned afterwards, instead of the old sequential chain where each filter reshaped the pool before the next one ran. Soft relaxation (falling back rather than emptying the pool) is assessed globally across all non-strict categories together, not per category, so two categories that each drop a different half of the pool are caught even though neither one alone would have emptied it.
- **Every drop now carries a reason.** `filter_rules.evaluate()` returns a `Verdict` per candidate (kept/dropped, which rule, which value, whether that rule self-relaxed), replacing the old silent list-shrinking. `streams.rank_streams_explained()` exposes this; `rank_streams()` keeps its old signature for existing call sites.
- **`EXCLUDE_LANGUAGES` loses its undocumented rescue.** The retired filter never dropped a candidate that also matched `AUDIO_LANGUAGE_PREFERENCE` or `multi`, even if it also matched an excluded language - so a release tagged both "ru" and "en" survived `EXCLUDE_LANGUAGES=ru`. `LANGUAGE_EXCLUDED` has no such exception: it drops on a match, full stop. This is an intentional behaviour change; use `LANGUAGE_INCLUDED` if you want a deliberate rescue instead.
- **Dolby Vision detection now recognises a trailing `.DV` marker.** The retired regex was `\b(dovi|dolby[\s.]?vision|\.dv\.)\b`, which required a bare `dv` to have dots on both sides and so missed a release name ending `...x264.DV`. The new detector's pattern is `\b(dovi|dolby[\s.]?vision|dv)\b`, which catches it. With `EXCLUDE_DV_P5` on (the default) this now drops some releases as `dv_only` that previously passed. That is intended, not a regression: the setting exists to keep Dolby Vision profile 5 (no HDR10 fallback layer) off clients that render it washed out, and the old pattern was simply missing cases it should have caught.
- Fixed the `bluray`/`remux` overlap: the retired `EXCLUDE_BLURAY` regex matched `BluRay.REMUX` releases too, so setting it could silently drop remuxes that `EXCLUDE_REMUX` was never asked to touch. `release_tags.detect_sources()` is mutually exclusive (most-specific pattern wins), so a BluRay remux is tagged `remux`, never both.
- `STRICT_NO_CAM` is decoupled from the undersized-release size check it used to also govern, unrelated to cam rips. The size check now has its own `EXCLUDE_UNDERSIZED_STRICT` toggle; `STRICT_NO_CAM` migrates to `SOURCE_STRICT`.
- `SORT_ORDER` is now configurable (ten criteria: `season_pack`, `resolution`, `cached`, `language`, `source`, `encode`, `visual_tag`, `audio_tag`, `seeders`, `size`), replacing a hardcoded seven-term tuple. The default reproduces the old order exactly.
- **Naming fix:** `QUALITY_PREFERENCE` held a resolution (1080p/2160p/720p) despite its name. The new `RESOLUTION_PREFERRED` (and the rest of the `RESOLUTION_*` family) name it correctly - if you hand-write a `.env`, `QUALITY_PREFERENCE=1080p,2160p` becomes `RESOLUTION_PREFERRED=1080p,2160p`, not a `QUALITY_*` key.
- The retired settings are translated into the new rule rows automatically, once, at startup, guarded by `FILTER_RULES_MIGRATED` - a later startup never re-reads them and clobbers an edit made since in the admin UI. A retired key still present in `.env` after migration is reported (not silently ignored) via a startup warning naming the key and its replacement.
- **The settings page renders the rule editor as seven category panels with per-value chips,** replacing 35 comma-separated text boxes and dropdown-selected lists. Each category's vocabulary is offered as a dropdown picker, so an invalid value cannot be typed. A value stored from `.env` that is not in the vocabulary is now surfaced in the UI struck through with an explanation; previously it warned once at container startup and was then invisible while matching nothing. `preferred` is the only state offering reorder controls, because order only changes behaviour in that state; `included` carries a permanent warning that it overrides every other rule in every category. The editor writes the same settings as before - `setting_<KEY>` hidden inputs on save - so `.env` values and the API remain unaffected.

### Fixed

- **The OIDC toggle in the admin UI now takes effect.** `oidc.is_enabled()` read `config.OIDC_ENABLED`, a snapshot taken from `.env` at startup, so switching OIDC on or off in Settings saved the value and changed nothing; only editing `.env` and restarting worked. It now reads the live settings overlay, falling back to `.env`. `OIDC_ENABLED` is also registered as a boolean setting: without that, storing `false` wrote the string `"false"`, which is truthy, so switching OIDC off would have left it on. A half-working toggle is worse than one that plainly does nothing, so both halves are needed.

## [0.6.6] - 2026-08-29

### Fixed

- Debridio results were being systematically outranked on a criterion they could not win. Debridio ships language information as flag emoji in the stream title, but `debridio.py` never populated `Stream.languages`, so every Debridio result arrived with an empty language set. Language is the third term of the ranking tuple - above WEB-DL, HEVC, seeders and size - and an empty set scores worst-but-one, so for anyone who had set `AUDIO_LANGUAGE_PREFERENCE` (it ships empty by default, so a default install saw no effect from this bug at all) a Debridio release lost to any Torrentio release whose name happened to spell out "ENGLISH", regardless of which was the better file. Debridio is queried first, so this quietly suppressed the scraper that was supposed to lead. Detection now lives in one place, `streams.detect_languages()`, reads flag emoji as well as name tokens, and is used by all three scrapers.
- **This can move a release down as well as up.** A release flagged only French and German previously scored as "unknown"; now it correctly scores below a release in a preferred language, and below an untagged one. That is the intended behaviour - we know more than we did - but a picked release changing after upgrade is expected rather than a fault.
- The detectable language vocabulary grows from four codes (`nl`, `en`, `multi`, `ru`) to 34, so `AUDIO_LANGUAGE_PREFERENCE` and `EXCLUDE_LANGUAGES` can now name languages that were previously undetectable. Both settings are validated when saved: an unknown code such as `english` is rejected with the valid codes listed, where before it was accepted silently and simply never matched. Values arriving from `.env` bypass that check and warn at startup instead.
- Zilean carries no language data of any kind. That is now explicit (`zilean.LANGUAGES_AVAILABLE`) rather than an accident of an empty field, and an empty language set is documented as meaning "the release did not say", never "this release has no audio" - untagged English is the default in release naming, so anything treating absence as a positive fact would discard most of the catalogue.

## [0.6.5] - 2026-08-29

### Added

- **Debridio** joins Zilean and Torrentio as a third scraper, and is queried first when enabled. It is a paid Stremio-protocol addon, so unlike the other two it needs credentials: `DEBRIDIO_API_KEY` must be your *Debridio account* key, not a debrid provider key - Debridio is a search addon that proxies through a provider, and the provider key it proxies with is your existing `TORBOX_API_KEY`, which Mycelium reuses automatically. Both are required; with either missing the scraper reports itself unconfigured and stays out of rotation entirely rather than failing per request. Its stream objects carry no `infoHash` field, so the hash is recovered from `behaviorHints.bingeGroup` with the play-URL path as a fallback - verified consistent across 702 of 702 streams, 88 of which matched Torrentio's hashes for the same title verbatim. The addon's config segment is base64 JSON holding **both** API keys, which makes the request URL itself a secret: every log line, health payload and exception message that could carry it passes through `debridio.redact()` first.
- The Debridio config is deliberately permissive - every resolution, no excluded qualities, no size cap - rather than mirroring Mycelium's own filter settings down into it. The two filter models are not the same kind: Mycelium's filters are soft and self-disabling (`EXCLUDE_REMUX` drops remuxes only while something else survives, then logs "only remux candidates available; allowing them" and takes them anyway), while Debridio's are hard. Pushing ours down would delete streams upstream that `rank_streams` would have chosen to allow, and the fallback that exists precisely for the thin-pickings case would never fire. Filtering stays in one place, at ranking time, with the full pool visible.
- A **`source_unique_win`** metric alongside the existing `source_win`. Win rate on its own overstates whichever scraper sits first in the priority order: the three scrapers overlap heavily, so a source can win nearly every race while contributing almost nothing that the others would not have found a moment later. `source_unique_win` counts only the wins where no other scraper returned that hash at all, which is the number that actually answers "is this source worth querying". The merge records the overlap in `also_seen_in` as it dedupes, so both metrics come from the same pass.

### Fixed

- Candidate discovery is now a single `scrapers.fetch_candidates()` orchestrator instead of nine hand-rolled call sites that had drifted into three different orchestration patterns - some queried Zilean and Torrentio sequentially, some concurrently, some checked health first and some did not, and the dedup rules differed. All three scrapers are now queried concurrently and merged in *priority* order rather than completion order, so which source keeps a duplicated hash is deterministic instead of a race. The shared stream model, parsing helpers and ranking moved out of `torrentio.py` into `streams.py`; they were never Torrentio-specific, they just lived there.
- A transient scraper outage could delete the library. Because the orchestrator catches every scraper exception and returns an empty list, cleanup's repair pass read a total upstream outage as "this title no longer exists anywhere" and unlinked the `.strm`, its `.nfo` and the database row, then marked the title unfixable for 24 hours. Ten minutes of Torrentio 502s with the other two scrapers off was enough to take out every already-broken title in one run. Callers that destroy something on an empty result now opt into `ScrapersUnavailable`, which separates "searched and found nothing" from "could not search at all"; the same confusion was putting web playback into a six-hour backoff that outlived the outage by hours.
- A lapsed Debridio subscription reported itself healthy. Both health probes treated any status below 500 as up, but Debridio is the only scraper that authenticates: 401/403 for an expired subscription and 404 for a garbled config token all sailed through, so the admin Health card showed ok and every search kept paying a pointless round trip instead of falling through to the other scrapers.
- With `LOG_LEVEL=DEBUG`, urllib3 logged each request's full path - for Debridio, the base64 config segment holding both API keys - into the log buffer and the admin Logs tab. It logs below every redaction call site, so nothing in Mycelium's own code could have caught it; the urllib3 logger is now pinned at WARNING regardless of log level.
- The web player never saw Debridio: it still queried Zilean and Torrentio directly, because it deliberately does not want the house ranking (it orders by browser compatibility instead). It now shares the orchestrator's fetch, merge and dedup and applies its own scoring to the result.
- The dashboard's Quality card split unrecognised qualities across two labels, because the shared parser called them `""` and Torrentio's called them `"unknown"`.
- The outage guard above almost never fired. It counted a scraper as failed only when its adapter raised, but Debridio and Zilean both document "never raises, returns `[]` on failure" - only Torrentio propagates. With all three active, at most one could ever count as failed, so `failed == len(active)` could never be true and a real all-scrapers-down outage still read as "searched, found nothing". Debridio and Zilean now take a `raise_on_error` flag that `scrapers.py` sets so their failures are counted too, and the guard itself is now "something failed AND nothing was found" - evaluated after the merge, so a partial failure that still turned up candidates proceeds normally instead of blocking a legitimate repair. The flag defaults to `False` everywhere else, so both adapters' documented never-raises contract is unchanged for other callers.
- Catbox's per-title search cache stored the outage sentinel for 6 hours instead of not caching it. `_search_cached_release` only shortened the TTL for a truthy result, so the sentinel fell through to the same 6-hour "nothing cached" backoff as a real miss - the token's own retry cooldown was the intended 30s, but the title itself would not be re-searched again until long after any real outage had ended. The sentinel is no longer written to the cache at all; the next request re-searches.

## [0.6.4] - 2026-08-29

### Fixed

- Catch-up requests were recorded under their raw IMDB id (`tt2017109` instead of the actual title), which then propagated into the library folder name on disk. Seerr's `Media` entity has no title column - it carries only `mediaType`, `tmdbId`, `tvdbId`, `imdbId` and `status` - so `media.get("title")` in `catchup._build_request` was always `None` and the raw id fallback fired every time. Titles now resolve through `tmdb.display_title()`, the same fallback `webhook_parser` uses for a payload with no subject. This was always broken but only surfaced at scale in 0.6.3: while the webhook was rejecting requests for want of an IMDB id, approved requests piled up unprocessed in Seerr, and the first restart after that fix let catch-up replay the whole backlog at once. **Existing rows and folders can be repaired with the "Fix IMDB titles" button in Admin > Maintenance**, which renames on disk and updates the database and strm paths.

### Internal

- `test_strm_generator` swapped mocks into `sys.modules` and then imported `strm_generator`, which is silently order-dependent: if any earlier test module had already imported `strm_generator` for real, the swap bound nothing and every test relying on a mocked `settings`/`db` ran against the real module instead - failing on an unrelated assertion with no indication why. Dependencies are now patched as module attributes in an autouse fixture, so the file passes regardless of import order, and the mocks are function-scoped so direct assignments no longer leak between tests.

## [0.6.3] - 2026-08-28

### Fixed

- Seerr/Jellyseerr requests were intermittently rejected with `400 No IMDB id found in webhook payload or Seerr API`, most often for TV and anime. Three things combined to make this the common case rather than an edge case: Seerr's shipped default webhook template emits only `media_type`/`tmdbId`/`tvdbId`/`status` and has no `{{media_imdbid}}`, so no id ever arrives in the payload; Seerr creates its own `Media` row without an `imdbId`, and only ever backfills one for movies its Jellyfin scanner has already found on disk (never for TV); and the TMDB fallback that should have covered both was called from *inside* the Seerr API branch, so any failure of that round-trip - unreachable, 404, 401, or a payload with no `request_id` - skipped it entirely, despite the `tmdbId` sitting in the payload the whole time. The fallback is now hoisted out of that branch and runs off whichever `tmdbId` is available, choosing TMDB's movie or tv `external_ids` endpoint from the payload's own `media_type`. Requires `TMDB_API_KEY` to be a v4 Read Access Token (the long `ey...` string) - the API is called with bearer auth, so a v3 key returns 401 on every call and resolution silently fails.
- The webhook handler no longer contacts Seerr at all when `SEERR_URL` is unset - it previously made a guaranteed-to-fail request and logged a misleading `Seerr API lookup failed` warning on every single request.
- A webhook template rendering an unsubstituted `{{media_tmdbid}}` raised `ValueError` out of `int()`, which is not a `WebhookError` and so escaped as an HTTP 500 with a traceback instead of a clean 400.
- The "no IMDB id" error now names the subject, the `tmdb_id` it tried, and whether `TMDB_API_KEY` is set at all, instead of reporting the same opaque string for four unrelated causes.

## [0.6.2] - 2026-07-11

### Added

- **spore-nfs** and **spore-smb**: read-only NFSv3 and SMB2/3 servers exposing the virtual library as real files, backed by the existing `/spore-stream/<token>` endpoint (no new materialization logic). Server-side tricks to block Direct Play on Shield/Android TV (stub channel count, forced PGS subtitle burn-in) stopped working reliably - Android's local-network fast path bypasses the profile negotiation Linux/desktop clients respect, turning the fake stub into a black screen instead of a transcode. With real size/bytes served, Direct Play becomes correct instead of catastrophic, on every client. Both protocols share a 3-window read-ahead LRU, background prefetch, resolved-CDN-URL caching, self-healing on a dead cached URL, and a token-bucket rate limiter with retry/backoff against TorBox/CDN 429s. spore-nfs later merged into the main image (one container instead of two).

### Fixed

- MKV/non-MP4 playback via `/stream` (Jellyfin's path, and any other client not building a moov-first cache) could be redirected straight to a CDN URL that had gone dead earlier than its 23h in-memory cache TTL, with no validation - Jellyfin/ffmpeg would follow the dead link into a TorBox error page and fail with `FFmpegException: FFmpeg exited with code 8` / `ffprobe failed`. Now HEAD-checked before redirecting, with an automatic re-resolve on a dead link.
- `play_count`/`last_played` were written to SQLite on every single byte-range request during playback, not just on play start - under concurrent playback these writes serialized against each other for no benefit. Debounced to once per token per 60s; the CDN liveness check above is similarly cached for 120s so repeated seeks in one session don't each pay a fresh CDN round trip.
- Stale `JELLYFIN_API_KEY` on deploy could leave `refresh_library()`, `merge_duplicate_versions()`, `refresh_missing_images()` and continue-watching sync silently failing with 401 - not a code fix, but worth a mention since it was masking as playback flakiness.

## [0.6.1] - 2026-07-05

A security- and correctness-focused release from a full multi-pass code review. No new features.

### Security

- OIDC and trusted-proxy logins no longer implicitly become admin - `auth.py` now resolves or creates a real per-user role (`user` by default; only the very first user ever provisioned this way becomes admin, and only during initial, incomplete setup)
- `AUTH_SESSION_SECRET` is no longer used to sign sessions when left at the well-known default value - a random secret is generated and persisted instead, same pattern as the existing `WEBHOOK_SECRET_AUTO`
- Added `is_admin()` checks to roughly 30 previously-unprotected `/ui/*` and `/ui/api/*` routes: settings save/reset, backup restore, DB vacuum/prune, cleanup/repair/migrate triggers, Zilean sync/import, wanted-recheck, NFO/strm regeneration, and several legacy `/api/*` aliases that had slipped through
- `/admin` itself now redirects non-admin users to login instead of only checking that setup is complete
- Web Player `/stream/<token>/*` playback routes now require an authenticated session with the Web Player feature enabled - previously reachable by anyone who obtained a token
- `TRUSTED_PROXY_NETWORKS` default narrowed from broad private-IP ranges to loopback only
- Webhook secret and internal token comparisons now use constant-time comparison throughout
- The Spore TCP server (port 8089, unauthenticated protocol) now binds to loopback by default instead of all interfaces

### Fixed

- A transient scraper/cache-check error on a single episode could mark an entire multi-season request "failed", discarding seasons that had already been added successfully
- The retry queue could silently drop a failed retry and abort the rest of that cycle's batch instead of continuing
- Cleanup/repair and canonical-name migration could leave orphaned database rows behind after deleting or merging `.strm` files, permanently blocking recreation of that title
- Folder rename/merge database updates could corrupt a sibling folder's paths when one folder name was a literal prefix of another (e.g. "Alien (1979)" vs. "Alien (1979) Directors Cut")
- Duplicate-folder merges could silently delete a file that was never actually copied over first
- Plex's fast-start MP4 cache could corrupt sample offsets for CDN files with a second data block after the `moov` atom (dual-mdat layout)
- HTTP suffix byte-ranges (`bytes=-N`) were parsed as the first N bytes instead of the last N
- CSRF protection was effectively disabled on roughly 27 internal API routes because the exemption predated the frontend actually sending the CSRF token
- Several background jobs (series monitor, retry queue) could abort an entire batch when a single item raised an unexpected error instead of continuing with the rest
- Assorted smaller fixes: SQLite `LIKE` wildcard characters in folder names could cause wrong-path matches during renames; a webplayer seek race could start two concurrent FFmpeg processes for the same session; two `/api/*` routes referenced an unimported module and would have raised on use

## [0.6.0] - 2026-07-04

### Credits

Several of the bugfixes in this release were discovered and/or confirmed through the work of [Ventrex](https://github.com/Ventrex/mycelium) in his fork ("VenFlix") and the accompanying [GitHub Discussions](https://github.com/corveck79/mycelium/discussions). Thanks for digging into these issues and sharing the fixes/ideas with the community.

Thanks also to [Damosso](https://github.com/Damosso) for the Seerr webhook secret tip in [#41](https://github.com/corveck79/mycelium/issues/41), which shaped a docs fix earlier in this cycle.

### Added

- **Trakt**: auto-request new watchlist items for download (not just watchlist sync), capped daily, built into the existing Trakt plugin
- **MDBList integration**: connect your own API key, pick lists to sync, capped auto-request
- **Auto-approve**: per-genre rules with year ranges, follow favorite actors (auto-requests their filmography, excludes talk shows/soaps), shared daily budget
- **Discover genre tabs**: admin-configurable browse rows per genre + year range
- **Language filter**: per-user include/exclude of content by original language in Discover
- **Clickable cast**: cast in the detail modal opens an actor page with bio + filmography + Follow button
- **TorBox library scan**: reads existing TorBox cache and creates `.strm` files for anything missing (e.g. after a DB reset)
- **Notification settings** in the React Settings page (Discord/Telegram)
- **Real topbar search bar** instead of just a link to the search page
- **React Admin dashboard finally routed**: `/admin` now shows a tab between the new dashboard (user management, Radarr/Sonarr import, Auto-approve, genre tabs, maintenance) and the existing Jinja page - this page already existed but was never wired to a route

### Fixed

- Settings-UI overrides were silently ignored in several places (Zilean, TMDB, RealDebrid, TorBox, OpenSubtitles, catbox) due to frozen `config.py` imports instead of `settings.get()`
- Mislabeled cams/trailers (e.g. "2160p" that's actually a cam) are now rejected based on physically plausible file size vs. TMDB runtime
- Unreleased titles could pull in fake/cam releases - now blocked via TMDB release date
- Multi-season series only got season 1 into the library
- Duplicate episode tokens/strms when title sanitizing landed differently
- `db.insert_request()` could update the wrong row on retry (SQLite `lastrowid` quirk), leaving requests permanently stuck on "rate_limited"
- TorBox timeouts were treated as success, writing a `.strm` before the torrent was actually ready
- Series could end up split across multiple folders due to varying release names
- Jellyfin library refresh had no debounce, could fire excessively during bulk operations
- Raw IMDb IDs (`tt1234567`) instead of titles shown in notifications/UI for requests without a title in the payload
- Toggle switches in the admin user panel rendered incorrectly (knob always on the right regardless of state)
- Clickable cast was invisible due to a z-index conflict between the detail and actor modals
- Removed a duplicate, colliding Trakt integration (a new build on top of an already-existing plugin) - including a database schema conflict that broke the existing plugin
- Web Player: `/ui/api/web-player/status/<job_id>` silently dropped `token`/`stream_type` from its JSON response, so the frontend always fell into the HLS.js branch (pointed at a raw MP4 redirect instead of a playlist) instead of direct-playing eligible files, causing an infinite retry/timeout loop

## [0.5.2] - 2026-06-12

### Added

- **Web Player VA-API**: hardware-accelerated HEVC transcoding via VA-API (`renderD128`); reduces CPU usage significantly on supported hardware
- **Web Player HEVC-always**: HEVC is always transcoded to HLS regardless of codec; direct serve only for H264 to avoid browser incompatibility
- Docker Compose: `videodriver` GID 937 added for VA-API `renderD128` access
- **Spore wrapper EAE detection**: also detects EAE need from output encoder args (e.g. Shield TV requesting `eac3_eae` output via eARC); skips injecting native decoder hint when output is `copy` to prevent EAE init failures on HTTP input

### Fixed

**Web Player**
- Black screen / corrupt green output on 10-bit HEVC with VA-API (Apollo Lake J3455)
- `scale_vaapi` failure on 10-bit HEVC sources
- Stale segments causing black screen after seek or restart
- Missing `/direct`, `/convert-hls`, `/hls-status` routes
- HLS buffer increased to prevent stalls on slow CDN
- Temp directory leak when HLS conversion crashes before session registration
- `ffmpeg.log` file handle not closed on `Popen` failure
- `shutil.rmtree` called before ffmpeg process exits (race condition)

**Security**
- Session fixation: `session.clear()` now called before writing new session keys on login
- `/torbox-webhook` and `/ui/api/repair-strms` now require authentication
- `/setup/save` now validates against a known-key allowlist (previously accepted arbitrary keys)
- `/health` no longer leaks internal exception details in the response body

**Data integrity**
- `cleanup.py`: new strm written via `process_torrent` before the old one is deleted
- `upgrader.py`: season-pack strms written before per-episode strms are removed
- `mp4_faststart.py`: `.fsh` cache written atomically via temp-file + rename; ftyp box fetched at actual size instead of hardcoded 64 bytes

**Logic**
- `torbox.py`: `metaDL_done` state never matched because `download_state` is lowercased before comparison — fixed to `metadl_done`
- `torbox.py`: createtorrent quota now recorded after HTTP success, not before (prevented quota inflation on network errors)
- `torrentio.py`: season-pack regex `s0?N` → `s0*N(?!\d)` to correctly match zero-padded season codes
- `catbox.py`: `release_idle()` no longer aborts on first network error — each torrent deletion is now wrapped in try/except
- `monitor.py`: aired episodes without a strm are now marked `wanted` in the DB (were silently left without status)
- `retry_queue.py`: startup crash on undefined `_CREATETORRENT_LIMIT` constant (should be `_CREATETORRENT_LIMIT_HOUR`)
- `db.py`: `_migrate()` ALTER TABLE loop now catches per-column errors instead of aborting remaining migrations

**Fresh install**
- Fixed crash `sqlite3.OperationalError: no such table: settings` on first boot when the DB is empty ([#34](https://github.com/corveck79/mycelium/issues/34))

---

## [0.5.1-dev] - 2026-05-29

### Added

- **Library poster grid**: movies tab now shows a paginated poster grid (24/page) with the same look as Discover and Watchlist
- **Library search and filters**: search box and All / Available / Wanted filter tabs in the movies view
- **Open in Jellyfin preference**: per-user toggle in Settings > Preferences; clicking a library poster opens the item directly in Jellyfin web instead of the detail modal
- **Jellyfin batch lookup**: Jellyfin item IDs are pre-fetched in one call so poster clicks are synchronous (no popup-blocker issues)
- **Lazy poster loading**: posters missing from the local cache are fetched on first render without blocking the page

### Fixed

- GitHub Actions arm64 build crash: removed dead `spore-builder` Dockerfile stage that compiled a C LD_PRELOAD library using `stat64`/`__xstat64` which do not exist on aarch64
- Jellyfin click mode not working after toggle: Settings now uses an optimistic session-cache update so Library reacts instantly without a page reload
- Detail modal not opening for older items that lack a stored `tmdb_id` (now resolved via `/ui/api/tmdb/find`)

---

## [0.5.0-dev] - 2026-05-28

### Added

- **Mycelium Spore** (experimental Plex integration): stream via stub MKV library + transcoder wrapper, no rclone or local storage required
- **Spore fast-start cache**: moov-first MP4 cache (`.fsh` files) built on first play so subsequent plays are instant
- **Spore track persistence**: audio/subtitle tracks and duration saved to DB after first ffprobe; stubs are regenerated with real tracks on container restart
- **Spore CDN preload**: fast-start cache and ffprobe run automatically when a CDN URL is first resolved, so first play is instant even before user interaction

### Fixed

- TorBox outage no longer causes a 6-hour retry delay for affected items
- HDR10+ no longer treated as a valid HDR10 fallback in the Dolby Vision P5 filter
- Bulk rename for items stored with raw IMDB codes as title (Admin > Maintenance > Fix IMDB titles)
- HEVC compatibility fix in the webplayer plugin for browser playback

---

## [0.4.2] - 2026-05-25

### Added

- `WEBHOOK_SECRET` auto-generation with copy button in admin Settings
- Metrics endpoint secured with optional Bearer token
- Rate limiting on authentication endpoints

### Fixed

- Setup wizard now closes after first run (re-open via Settings)
- WebDAV auth hardening and security headers

---

## [0.4.1] - 2026-05-25

### Added

- Docker Hub CI/CD pipeline on release tags (multi-arch images)
- Splash screen as login background

---

## [0.4.0] - 2026-05-25

### Added

- `LITE_MODE` for webhook-only deployments without heavy background schedulers
- Settings tab in admin dashboard (hot-reload quality filters and runtime config)

### Changed

- Setup wizard UI improved

---

## [0.3.0-beta] - 2026-05-24

### Added

- **Web Player plugin**: in-browser HLS player with subtitle picker
- **Trakt plugin**: watchlist sync and ratings integration
- **Plugin slot system**: plugins can inject components into the frontend (episode player, settings cards)
- Web Player: HDR detection and SDR-only release selection for browser compatibility
- Web Player: multi-audio HLS master playlist with separate audio streams

---

## [0.2.0-beta] - 2026-05-22

### Added

- **Multi-user authentication** with roles (admin/user) and pending approval flow
- **OIDC/SSO support** for single sign-on
- Users tab in admin with pending approval management
- Redesigned React SPA: Library status indicators, region picker

### Fixed

- Open redirect vulnerability on login
- `/setup` accessible without authentication

---

## [0.1.0-beta.1] - 2026-05-22

First public beta. Mycelium has been running in production for several
users; this release formalizes versioning and adds CI/CD.

### Added

- **React SPA** with Discover, Library, Watchlist, Search, Requests, and Wanted pages
- **Setup wizard** walks through TorBox, Jellyfin, TMDB, quality preferences, and Catbox config on first launch
- **Catbox mode** (lazy materialization): torrents added to TorBox on-demand at playback, removed after idle
- **Multi-user auth** with password and OIDC support, role-based access (admin/user)
- **Auto-upgrade**: background job upgrades existing releases when better quality becomes available
- **Season pack consolidation**: replaces individual episode files when a full season pack is found
- **Zilean + Torrentio combined search**: both sources queried and deduplicated for maximum coverage
- **Checkcached batching**: hashes sent in groups of 100 to avoid 414 URI Too Long errors
- **Language filtering**: exclude unwanted audio languages, prefer specific languages
- **Dolby Vision Profile 5 filter**: blocks DV releases without HDR10 fallback layer
- **Separate EXCLUDE_BLURAY option**: BluRay encodes allowed by default, remux filtered separately
- **Blacklist system**: failed info_hashes tracked and excluded from future attempts
- **Playability state tracking**: per-item failure reasons (TB_429, NO_RELEASE, TIMEOUT, etc.)
- **Discord and Telegram notifications** on success/failure
- **OpenSubtitles integration** for automatic subtitle downloads
- **WebDAV server** (optional) for Plex/Emby compatibility
- **RealDebrid support** as fallback debrid provider
- **Radarr/Sonarr bulk import** for migrating existing libraries
- **Community install guide** by Ventrex (EN/NL, Proxmox/NAS)
- **Admin dashboard** with Overview, Requests, Blacklist, Maintenance, Settings, and Logs tabs
- **Pagination** on admin tables (25/50/100/250 rows)
- **CI/CD**: GitHub Actions builds multi-arch Docker images on tag push to GHCR

### Fixed

- Startup crash when duplicate imdb_id rows exist in requests table
- Monitor loop continuing after checkcached 429 (now backs off 60s in catbox mode)
- Upgrader crash from renamed rate limit constant
- Source field showing first word of torrent name instead of torrentio/zilean
- REMUX filter blocking all BluRay encodes (now only blocks actual remux)

### Changed

- Admin page embeds seamlessly in SPA (no double topbar when accessed via sidebar)
- Admin colors matched to SPA palette
- Repair tab renamed to Maintenance with grouped action cards
- Quality preferences and filters are hot-reloadable via Settings (no restart needed)
