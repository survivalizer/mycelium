# Compatibility

The 1.0 promise: which parts of Mycelium other deployments and other tools
can build on, and how a promised part changes over time. Two guard tests in
`tests/test_compatibility.py` keep this document equal to the code: a
variable in `config.py` that lands in no tier, a route missing from the
frozen list below, or a deprecated name with no replacement, fails the
suite until this file is fixed.

Everything under `/ui/*` (the admin API and the bundled SPA's own calls) is
**internal**. It may change shape, move, or disappear in any release
without notice, warning, or a changelog entry. Nothing outside Mycelium's
own frontend should call it. So is every other route that is not on the
frozen list below: the SPA shell, `/login` and the OIDC callback, `/dav/*`,
`/spore-nfs/*`, the `/api/*` maintenance triggers, and anything added after
this document was written.

## 1. What is frozen

The routes below are the integration surface: what Seerr, TorBox, Radarr,
Sonarr, Jellyfin, Plex, the setup wizard and the in-app manual talk to.
Their path, method and general request/response shape are promised; a
change to any of them is a breaking change (see section 4). The five
`/setup/*` routes are also rate limited per caller address; those limits
are part of the promise too, the same as everything else here: raising a
limit is a minor change, lowering one is breaking.

<!-- routes -->
```
GET /docs/<path:filename>
GET /health
GET /healthz
GET /internal/stream-resolve/<token>
GET /metrics
GET /setup
GET /setup/schema
GET /spore-stream/<token>
GET /stream/<token>
POST /internal/stream-report/<token>
POST /setup/picker/<name>
POST /setup/save
POST /setup/skip
POST /setup/test/<kind>
POST /torbox-webhook
POST /webhook
POST /webhook/arr
```

Every example below uses `https://mycelium.example` as a stand-in host.

### `GET /health`

The liveness probe Docker's `HEALTHCHECK` polls. No authentication, no
request body. Returns `{"status": "ok"}` when the database answers a
query, or HTTP 503 with `{"status": "degraded"}` when it does not.

```
$ curl https://mycelium.example/health
{"status": "ok"}
```

### `GET /healthz`

A deeper readiness probe: the database plus at least one scraper (Zilean
or Torrentio) reachable. No request body. Returns 200 with
`{"status": "ok", "failures": [], "zilean": true, "torrentio": true}`, or
503 with `"status": "down"` and each failing check named in `failures`.

```
$ curl https://mycelium.example/healthz
{"status": "ok", "failures": [], "zilean": true, "torrentio": true}
```

### `GET /metrics`

Prometheus scrape endpoint. The check is exclusive, not either/or: when
`METRICS_TOKEN` is set, only the header `X-Metrics-Token: <token>` or the
query parameter `?metrics_token=<token>` is checked, and an admin session
with no token still gets 401; when `METRICS_TOKEN` is unset, an admin
session is required and a token (there being none configured to match)
does nothing. Returns the Prometheus text exposition format on success, or
401 on failure either way. The route is exempt from the login gate, so a
scraper is answered by the check above rather than redirected to the login
form it could not complete.

```
$ curl -H "X-Metrics-Token: <token>" https://mycelium.example/metrics
# HELP mycelium_requests_total ...
# TYPE mycelium_requests_total counter
mycelium_requests_total 42
```

### `POST /webhook`

Seerr's (Overseerr's, Jellyseerr's) notification webhook. Always requires
the webhook secret, via the `X-Webhook-Secret` header (preferred) or a
`?secret=` query parameter (deprecated: it leaks into access logs); a
missing or wrong secret answers 401 (Flask's default error page, not
JSON). Leaving `WEBHOOK_SECRET` blank does not turn the check off:
Mycelium generates a secret at first start and stores it, shows it in
Settings, and serves it from `GET /ui/api/webhook-secret`. Either way the
caller has to send it. The body is Seerr's own JSON notification shape; a
body Mycelium cannot parse into a request (missing fields, an unknown
notification type it should act on) answers 400
`{"status": "error", "error": "<reason>"}`. A request seen before for the
same title, media type and seasons is answered as a duplicate and not
reprocessed; otherwise the request is queued in a background thread and
the endpoint answers immediately.

```
$ curl -X POST https://mycelium.example/webhook \
    -H "X-Webhook-Secret: <secret>" \
    -H "Content-Type: application/json" \
    -d '{"notification_type": "MEDIA_APPROVED", "media": {"tmdbId": 603, "media_type": "movie"}}'
{"status": "accepted", "imdb_id": "tt0133093", "title": "The Matrix"}
```

### `POST /torbox-webhook`

TorBox's own push notification for a finished cache. Same secret gate as
`/webhook`. The body is accepted as JSON but not otherwise inspected: any
call that passes the secret check triggers a background re-scan for newly
ready torrents and answers `{"status": "ok"}`, regardless of what the body
contains. Unlike `/webhook` and `/webhook/arr`, this endpoint never answers
`ignored` or `duplicate`; it is a poke, not a parsed event.

```
$ curl -X POST https://mycelium.example/torbox-webhook \
    -H "X-Webhook-Secret: <secret>" -d '{}'
{"status": "ok"}
```

### `POST /webhook/arr`

Delete notifications: Radarr's `MovieDelete`, Sonarr's `SeriesDelete`, and
the Jellyfin webhook plugin's `ItemDeleted`. Same secret gate as
`/webhook`, including the 401 on a missing or wrong secret. The body must
be a JSON object in the sender's own webhook shape; a body that is not a
JSON object, or one `arr_webhook.parse()` cannot make sense of, answers
400 `{"status": "error", "error": "<reason>"}`. A title Mycelium does not
recognise, or a Jellyfin event fired while the `.strm` files are still on
disk, is answered `ignored` and nothing is purged; a recognised deletion
purges the title in a background thread.

```
$ curl -X POST https://mycelium.example/webhook/arr \
    -H "X-Webhook-Secret: <secret>" \
    -H "Content-Type: application/json" \
    -d '{"eventType": "MovieDelete", "movie": {"imdbId": "tt0133093"}}'
{"status": "accepted", "imdb_id": "tt0133093", "source": "radarr"}
```

### `GET /stream/<token>`

The URL written into every `.strm` file. `<token>` is an unauthenticated
capability token, not a session; Jellyfin and Plex request it without
logging in. No request body. Always answers with a 302 redirect, to one
of two places: straight to the TorBox CDN URL when the address is already
known, the file is served by redirect (a non-MP4 file) and the link was
confirmed alive recently (since 1.1.0), otherwise to
`/spore-stream/<token>`, which resolves and decides. A client that follows
redirects sees no difference between the two; a client that inspects the
`Location` must accept either. This route never touches TorBox itself and
never probes the CDN on the request: a liveness confirmation older than
its two-minute window is still used for up to eight minutes more while a
background probe refreshes it.

```
$ curl -i https://mycelium.example/stream/1a2b3c4d5e6f7890
HTTP/1.1 302 FOUND
Location: /spore-stream/1a2b3c4d5e6f7890

$ curl -i https://mycelium.example/stream/1a2b3c4d5e6f7890   # warm address
HTTP/1.1 302 FOUND
Location: https://<torbox-cdn-host>/dl/...
```

### `GET /spore-stream/<token>`

The moov-first proxy that actually materialises playback: resolves the
token against the URL cache, the TorBox library, or a fresh scrape and
TorBox add, waits for TorBox to finish caching, and serves bytes. Same
unauthenticated token. Accepts a standard `Range` header. Answers one of:
a 302 to the TorBox CDN URL for a non-MP4 file (after confirming the link
is still alive); a streamed `video/mp4` body, 200 or 206 with
`Content-Range`, moov-first once the local fast-start cache is warm and a
Range pass-through to the CDN while it is still building; or a failure,
raised via Flask's `abort()` (its default error page, not JSON): 404 when
the token does not resolve to a release at all, 502 when TorBox or the
CDN fails outright (an unusable HEAD response while building the
fast-start cache), 503 when the CDN itself is rate limiting (a HEAD
returning 429), and 416 for a `Range` header that does not fit the file.
In the default deployment the Go streaming front (`spore-stream/`) serves
these bytes itself and calls `/internal/stream-resolve/<token>` instead; this
Flask route is the complete fallback when `STREAM_FRONT_ENABLED=false`.

```
$ curl -i -H "Range: bytes=0-1048575" \
    https://mycelium.example/spore-stream/1a2b3c4d5e6f7890
HTTP/1.1 206 Partial Content
Content-Type: video/mp4
Content-Range: bytes 0-1048575/734003200
Content-Length: 1048576
```

### `GET /internal/stream-resolve/<token>`

The decision endpoint the Go streaming front calls instead of asking Flask
to serve bytes itself: the same materialize/liveness logic as
`/spore-stream/<token>`, returned as JSON (`mode`, and depending on `mode` a
`cdn_url`, a `size`, or a `.fsh` cache path) so the Go process can transfer
the bytes on its own. Refuses any caller whose address is not
`127.0.0.1`/`::1` with 403; the CDN URLs it returns are unauthenticated
capability links, so this must never be reachable from outside the
container, and the Go front additionally refuses to proxy `/internal/*` at
all. Documented here because it is part of the frozen contract between the
two processes in this image, not because a third party should call it.
On success, answers the same `mode`-tagged JSON the route above acts on
(`redirect`, `cold` or `warm`, without the raw `.fsh` bytes). On the same
failures `/spore-stream/<token>` raises via `abort()`, this route instead answers
JSON with the matching status code: 404 `{"error": "materialize failed"}`,
502 `{"error": "HEAD failed"}` or `{"error": "HEAD status <code>"}`, and
503 `{"error": "HEAD status 429"}` when the CDN itself is rate limiting.

```
$ curl -s http://127.0.0.1:8090/internal/stream-resolve/1a2b3c4d5e6f7890
{"mode": "warm", "cdn_url": "https://...", "cdn_size": 734003200, "fsh_path": "/data/..."}

$ curl -si http://127.0.0.1:8090/internal/stream-resolve/deadtoken0000000
HTTP/1.1 404 NOT FOUND
{"error": "materialize failed"}
```

### `POST /internal/stream-report/<token>`

Loopback-only, for the same reason as `/internal/stream-resolve/<token>`: the Go
front reports how many bytes it actually sent for one finished stream, so
Mycelium's egress estimate stays accurate. Body: `{"bytes": <integer>}`.
Answers `{"ok": true}`, or 400 when the body is not a JSON object or
`bytes` is not an integer.

```
$ curl -X POST http://127.0.0.1:8090/internal/stream-report/1a2b3c4d5e6f7890 \
    -H "Content-Type: application/json" -d '{"bytes": 734003200}'
{"ok": true}
```

### `GET /docs/<path:filename>`

Serves a static file out of the repository's `docs/` folder, such as
`install-guide.html`, the source of the in-app manual. A plain file server
scoped to that one directory; not in `auth._PUBLIC_PATHS` and not
otherwise carved out of the auth gate, so its authentication follows
`AUTH_ENABLED` like every other page route: no authentication when auth
is disabled (the default), and any logged-in session (not admin
specifically) when it is enabled. An anonymous request while
`AUTH_ENABLED=true` is redirected to `/login?next=<path>` rather than
answered with 401, the same as any other non-API page route.

```
$ curl https://mycelium.example/docs/install-guide.html
<!doctype html>...
```

### `GET /setup`

The setup wizard page. Before any user account exists it answers for
anyone, no login required, so a fresh install can be configured. Once an
account exists it requires an admin session (redirecting to `/login`
otherwise), and redirects to the library dashboard if setup is already
complete unless called with `?rerun=1`. Unlike every other route on this
page, it answers with the HTML SPA shell, not JSON.

```
$ curl -i https://mycelium.example/setup
HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

### `GET /setup/schema`

The wizard's steps and pre-filled values. Gated by the same rule as every
other `/setup/*` action below: open while nothing can log in yet, admin
session required afterwards. No request body. Returns JSON: the step list
with each field's current value, plus `needs_first_admin`. Rate limited to
`"30 per minute"`.

```
$ curl https://mycelium.example/setup/schema
{"steps": [...], "needs_first_admin": false}
```

### `POST /setup/picker/<name>`

Runs one of the wizard's remote pickers (Radarr/Sonarr root folders and
quality profiles) against posted credentials, so the wizard can offer a
dropdown instead of free text. Same gate as `/setup/schema`. Body:
`{"values": {KEY: value, ...}}`, JSON. Returns the picker's own result
shape, or 404 `{"ok": false, "error": "unknown picker"}` for an
unregistered name. Rate limited to `"30 per minute"`.

```
$ curl -X POST https://mycelium.example/setup/picker/radarr_root_folder \
    -H "Content-Type: application/json" \
    -d '{"values": {"RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "..."}}'
{"ok": true, "options": ["/movies"]}
```

### `POST /setup/save`

Saves the settings posted from the wizard. Unlike every other `/setup/*`
and `/ui/api/*` route, the body here is a normal HTML form
(`application/x-www-form-urlencoded`), not JSON, because the wizard posts
a plain form. Only keys that exist in the settings schema are accepted; an
unknown key is dropped and logged, not rejected. Returns
`{"ok": true, "saved": <count>}`, with `needs_first_admin: true` added
when no admin account exists yet (settings are saved, but setup is not
marked complete until one does). Rate limited to `"10 per minute"`.

```
$ curl -X POST https://mycelium.example/setup/save \
    -d "setting_TORBOX_API_KEY=abc123" -d "setting_CATBOX_MODE=true"
{"ok": true, "saved": 2}
```

### `POST /setup/test/<kind>`

Tests one integration (Jellyfin, TorBox, Seerr, and so on) against posted
values, without saving them. Same gate as `/setup/schema`. Accepts either
a JSON body `{"values": {...}}` (answered as `{"ok": <bool>, "message": <str>}`)
or a plain form body (answered as `{"ok": true, "detail": <str>}` on
success or `{"ok": false, "error": <str>}` on failure) - the response
shape depends on how the body was sent, matching the wizard's own two
call sites. 404 `{"ok": false, "error": "unknown integration"}` for an
unregistered `kind`. Rate limited to `"20 per minute"`.

```
$ curl -X POST https://mycelium.example/setup/test/jellyfin \
    -H "Content-Type: application/json" \
    -d '{"values": {"JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_API_KEY": "..."}}'
{"ok": true, "message": "Connected as admin"}
```

### `POST /setup/skip`

Marks setup complete without saving any settings, for the wizard's "skip"
button. Same gate as `/setup/schema`. No request body. Returns
`{"ok": true}`, or `{"ok": true, "needs_first_admin": true}` (without
marking setup complete) when no admin account exists yet. Rate limited to
`"10 per minute"`.

```
$ curl -X POST https://mycelium.example/setup/skip
{"ok": true}
```

## 2. Environment variables by tier

Every variable Mycelium reads falls into exactly one of four tiers,
derived mechanically from `settings.SECTIONS` (the Settings UI schema),
`settings.SETTING_GROUPS`'s `filter_rules` group (the seven-category,
four-state filter-rule keys and their strict toggles, which the admin
Filtering rules tab reads from its own structure rather than SECTIONS)
and `config.py`: a variable listed in the schema or the filter-rule group
and not flagged `advanced` is **supported**; listed in the schema and
flagged `advanced` is **advanced**; named in `settings._UNLISTED_KEYS`
(no UI field at all) is **internal**; everything else `config.py` reads
from the environment, through any `_env(...)`/`_env_int(...)` call
regardless of how the result is assigned, is **deployment**. The guard
test computes the same four sets from the code and fails if this
document disagrees.

**Supported** and **advanced** variables carry the 1.0 promise: removing
one, or changing its meaning, follows the deprecation rule in section 3.
**Deployment** variables are promised too, since they are how the
container itself is run (paths, ports, secrets, and a few process-level
knobs the entrypoint shell or the Go streaming front read directly).
**Internal** variables are not promised: they exist only for a
one-time migration marker or a retired setting kept so a stray `.env`
value from before the rule model is not silently misread as something
else, and may be removed without a deprecation cycle.

### Supported (118)

Listed in the Settings UI and not flagged advanced, or one of the 35
filter-rule keys the Filtering rules tab edits (its own admin tab, not a
Settings section). Everything else here is shown in Settings without
opening "Advanced".

<!-- tier: supported -->
```
ARR_STUBS_ENABLED
ARR_STUB_PATH
ARR_SYNC_ENABLED
ARR_SYNC_PURGE_ENABLED
AUDIO_CHANNELS_EXCLUDED
AUDIO_CHANNELS_INCLUDED
AUDIO_CHANNELS_PREFERRED
AUDIO_CHANNELS_REQUIRED
AUDIO_CHANNELS_STRICT
AUDIO_TAG_EXCLUDED
AUDIO_TAG_INCLUDED
AUDIO_TAG_PREFERRED
AUDIO_TAG_REQUIRED
AUDIO_TAG_STRICT
AUTH_ENABLED
AUTH_USERNAME
AUTO_ADD_MIN_RATING
AUTO_ADD_MIN_VOTES
AUTO_ADD_REGION
AUTO_APPROVE_ACTOR_DAILY_LIMIT
AUTO_APPROVE_DAILY_LIMIT
AUTO_UPGRADE_ENABLED
CATBOX_HOST
CATBOX_LAZY_ADD
CATBOX_MODE
CATBOX_PRELOAD
COMET_ENABLED
COMET_URL
DEBRIDIO_API_KEY
DEBRIDIO_ENABLED
DISCORD_WEBHOOK_URL
DISK_SYNC_ENABLED
DISNEY_NL_TOP_COUNT
ENCODE_EXCLUDED
ENCODE_INCLUDED
ENCODE_PREFERRED
ENCODE_REQUIRED
ENCODE_STRICT
EXCLUDE_UNDERSIZED_RELEASES
JELLYFIN_API_KEY
JELLYFIN_MEDIA_PATH
JELLYFIN_URL
LANGUAGE_EXCLUDED
LANGUAGE_INCLUDED
LANGUAGE_PREFERRED
LANGUAGE_REQUIRED
LANGUAGE_STRICT
LITE_MODE
MAX_SIZE_GB
MAX_SIZE_GB_BY_RESOLUTION
MDBLIST_AUTO_REQUEST_CAP
MEDIAFUSION_ENABLED
MEDIAFUSION_URL
MIN_SEEDERS
MULTI_DEBRID_ENABLED
NETFLIX_NL_TOP_COUNT
NOTIFY_ON_FAILURE
NOTIFY_ON_SUCCESS
OIDC_CLIENT_ID
OIDC_CLIENT_SECRET
OIDC_ENABLED
OIDC_ISSUER_URL
OIDC_PROVIDER_NAME
OPENSUBTITLES_API_KEY
OPENSUBTITLES_LANGUAGES
POPULAR_MOVIE_COUNT
POPULAR_TV_COUNT
PREFER_SMALLER_FILES
PRIME_NL_TOP_COUNT
RADARR_API_KEY
RADARR_QUALITY_PROFILE
RADARR_ROOT_FOLDER
RADARR_URL
REALDEBRID_API_KEY
RESOLUTION_EXCLUDED
RESOLUTION_INCLUDED
RESOLUTION_PREFERRED
RESOLUTION_REQUIRED
RESOLUTION_STRICT
SEASON_PACK_CONSOLIDATION_ENABLED
SEERR_API_KEY
SEERR_DECLINE_WANTED_AFTER_DAYS
SEERR_REPORT_STATUS
SEERR_URL
SONARR_API_KEY
SONARR_QUALITY_PROFILE
SONARR_ROOT_FOLDER
SONARR_URL
SORT_ORDER
SOURCE_EXCLUDED
SOURCE_INCLUDED
SOURCE_PREFERRED
SOURCE_REQUIRED
SOURCE_STRICT
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TMDB_API_KEY
TORBOX_API_KEY
TRAKT_AUTO_REQUEST_CAP
TRAKT_CLIENT_ID
TRAKT_CLIENT_SECRET
TRENDING_PRECACHE_COUNT
TRENDING_TV_COUNT
TRUSTED_PROXY_AUTH
VISUAL_TAG_EXCLUDED
VISUAL_TAG_INCLUDED
VISUAL_TAG_PREFERRED
VISUAL_TAG_REQUIRED
VISUAL_TAG_STRICT
WEB_PLAYER_MAX_SIZE_GB
ZILEAN_ENABLED
ZILEAN_MODE
ZILEAN_PG_DB
ZILEAN_PG_HOST
ZILEAN_PG_PASSWORD
ZILEAN_PG_PORT
ZILEAN_PG_USER
ZILEAN_URL
```

| Name | Default | Meaning |
|---|---|---|
| `ARR_STUBS_ENABLED` | `false` | Put a tiny MKV per title in a folder the arrs mount as their root, so titles show as present instead of missing. Needs Catbox mode. |
| `ARR_STUB_PATH` | `/arr-stubs` | Folder inside this container where the stubs are written; mount it in Radarr and Sonarr as their root folders. |
| `ARR_SYNC_ENABLED` | `false` | Create every title in the arr as a monitored entry with search off, and remove it on purge. |
| `ARR_SYNC_PURGE_ENABLED` | `true` | Once an hour, a mirrored title the arr no longer holds is removed from Mycelium. Off re-adds it to the arr instead. |
| `AUDIO_CHANNELS_EXCLUDED` | `(empty)` | Drops a candidate whose audio channel count matches. Self-relaxes (with a log line) if it would empty the whole candidate pool, unless AUDIO_CHANNELS_STRICT is set. |
| `AUDIO_CHANNELS_INCLUDED` | `(empty)` | Rescues a matching candidate from every other rule, in every category, even `_REQUIRED`/`_EXCLUDED` on an unrelated category. The one setting that can seriously surprise you; use it deliberately. |
| `AUDIO_CHANNELS_PREFERRED` | `(empty)` | Tie-break only, ranks a matching audio channel count ahead of others. Never rescues a value another rule dropped. |
| `AUDIO_CHANNELS_REQUIRED` | `(empty)` | Drops every candidate whose audio channel count does not match. A release the scraper could not tag for this category ("unknown") always survives, it never counts as a mismatch. |
| `AUDIO_CHANNELS_STRICT` | `false` | Reject non-matching audio-channel candidates outright instead of relaxing the rule when it would empty the pool. |
| `AUDIO_TAG_EXCLUDED` | `(empty)` | Drops a candidate whose audio tag matches. Self-relaxes (with a log line) if it would empty the whole candidate pool, unless AUDIO_TAG_STRICT is set. |
| `AUDIO_TAG_INCLUDED` | `(empty)` | Rescues a matching candidate from every other rule, in every category, even `_REQUIRED`/`_EXCLUDED` on an unrelated category. The one setting that can seriously surprise you; use it deliberately. |
| `AUDIO_TAG_PREFERRED` | `(empty)` | Tie-break only, ranks a matching audio tag ahead of others. Never rescues a value another rule dropped. |
| `AUDIO_TAG_REQUIRED` | `(empty)` | Drops every candidate whose audio tag does not match. A release the scraper could not tag for this category ("unknown") always survives, it never counts as a mismatch. |
| `AUDIO_TAG_STRICT` | `false` | Same as AUDIO_CHANNELS_STRICT, for the audio tag category. |
| `AUTH_ENABLED` | `false` | Ask for a login on every page. Off is only safe behind another login. |
| `AUTH_USERNAME` | `admin` | Username for the built-in admin login. |
| `AUTO_ADD_MIN_RATING` | `6.0` | Skip titles rated below this on TMDB. |
| `AUTO_ADD_MIN_VOTES` | `100` | Skip titles with fewer TMDB votes, which filters out obscure entries. |
| `AUTO_ADD_REGION` | `NL` | Country the streaming top lists are taken for. |
| `AUTO_APPROVE_ACTOR_DAILY_LIMIT` | `5` | Most titles the favourite-actor rule may approve per day. |
| `AUTO_APPROVE_DAILY_LIMIT` | `5` | Most titles the genre rules may approve per day. |
| `AUTO_UPGRADE_ENABLED` | `true` | Replace a title when a better release matching your rules appears. |
| `CATBOX_HOST` | `(empty)` | Address Jellyfin and other players can reach Mycelium on, used inside every .strm file. |
| `CATBOX_LAZY_ADD` | `false` | Skip the TorBox add at request time and do it on first play only. Saves TorBox adds, costs a longer first start. |
| `CATBOX_MODE` | `false` | Add a torrent to TorBox only when someone presses play, and drop it again after idling. Needed for the on-demand library and the arr stub files. Restart after changing. |
| `CATBOX_PRELOAD` | `true` | Add the torrent to TorBox as soon as a request succeeds so the first play starts fast. Each request spends one of the 60 hourly adds. |
| `COMET_ENABLED` | `false` | Search a Comet instance's Torznab feed. Self-hosted only: the public instance refuses this path. |
| `COMET_URL` | `(empty)` | Address of your Comet instance, with its access token path if protected. |
| `DEBRIDIO_API_KEY` | `(empty)` | From your Debridio account. |
| `DEBRIDIO_ENABLED` | `false` | Search the Debridio addon as well. Needs its own API key. |
| `DISCORD_WEBHOOK_URL` | `(empty)` | A webhook from a Discord channel's integrations page. Treated as a secret. |
| `DISK_SYNC_ENABLED` | `true` | Once an hour, a title whose .strm files are gone (a Jellyfin delete) is removed from Mycelium too. Off keeps the files coming back. |
| `DISNEY_NL_TOP_COUNT` | `0` | How many from the Disney+ top list. 0 is off. |
| `ENCODE_EXCLUDED` | `(empty)` | Drops a candidate whose encode matches. Self-relaxes (with a log line) if it would empty the whole candidate pool, unless ENCODE_STRICT is set. |
| `ENCODE_INCLUDED` | `(empty)` | Rescues a matching candidate from every other rule, in every category, even `_REQUIRED`/`_EXCLUDED` on an unrelated category. The one setting that can seriously surprise you; use it deliberately. |
| `ENCODE_PREFERRED` | `hevc` | Tie-break only, ranks a matching encode ahead of others. Never rescues a value another rule dropped. |
| `ENCODE_REQUIRED` | `(empty)` | Drops every candidate whose encode does not match. A release the scraper could not tag for this category ("unknown") always survives, it never counts as a mismatch. |
| `ENCODE_STRICT` | `false` | Reject non-matching encode candidates outright instead of relaxing. |
| `EXCLUDE_UNDERSIZED_RELEASES` | `true` | Drop releases far smaller than expected for their resolution; they are usually re-encodes or fakes. |
| `JELLYFIN_API_KEY` | `(empty)` | Dashboard, API Keys. Lets Mycelium trigger library refreshes. |
| `JELLYFIN_MEDIA_PATH` | `(empty)` | Only when Jellyfin mounts the media folder at a different path than Mycelium does. Blank means the same path. |
| `JELLYFIN_URL` | `(empty)` | Address Mycelium can reach Jellyfin on, from inside Docker if both run there. |
| `LANGUAGE_EXCLUDED` | `(empty)` | Drops a candidate whose language matches. Self-relaxes (with a log line) if it would empty the whole candidate pool, unless LANGUAGE_STRICT is set. |
| `LANGUAGE_INCLUDED` | `(empty)` | Rescues a matching candidate from every other rule, in every category, even `_REQUIRED`/`_EXCLUDED` on an unrelated category. The one setting that can seriously surprise you; use it deliberately. |
| `LANGUAGE_PREFERRED` | `(empty)` | Tie-break only, ranks a matching language ahead of others. Never rescues a value another rule dropped. |
| `LANGUAGE_REQUIRED` | `(empty)` | Drops every candidate whose language does not match. A release the scraper could not tag for this category ("unknown") always survives, it never counts as a mismatch. |
| `LANGUAGE_STRICT` | `false` | Reject non-matching language candidates outright instead of relaxing. |
| `LITE_MODE` | `false` | Run only the webhook, the processor and this admin: no Discover, no schedulers, no plugins. For Seerr and Jellyfin-only setups. Restart after changing. |
| `MAX_SIZE_GB` | `0` | Largest release to accept, in GB. 0 means no limit. |
| `MAX_SIZE_GB_BY_RESOLUTION` | `(empty)` | Caps per resolution as resolution:GB pairs, for example 2160p:40,1080p:12. Blank uses the single maximum above. |
| `MDBLIST_AUTO_REQUEST_CAP` | `10` | Most titles an MDBList sync may request per run. |
| `MEDIAFUSION_ENABLED` | `false` | Search MediaFusion's Torznab feed. The public ElfHosted instance works without a key. |
| `MEDIAFUSION_URL` | `https://mediafusion.elfhosted.com` | Leave the default for the public instance or point at your own. |
| `MIN_SEEDERS` | `3` | Releases with fewer seeders are ignored. 3 is a safe floor for cached content. |
| `MULTI_DEBRID_ENABLED` | `false` | Try RealDebrid when a release is not cached on TorBox. |
| `NETFLIX_NL_TOP_COUNT` | `0` | How many from the Netflix top list for your region. 0 is off. |
| `NOTIFY_ON_FAILURE` | `true` | Send a message when a request fails for good. |
| `NOTIFY_ON_SUCCESS` | `true` | Send a message when a request lands. |
| `OIDC_CLIENT_ID` | `(empty)` | Client id registered for Mycelium at the provider. |
| `OIDC_CLIENT_SECRET` | `(empty)` | Client secret from the provider. |
| `OIDC_ENABLED` | `false` | Log in through an OpenID Connect provider such as Authentik or Keycloak. Restart after changing. |
| `OIDC_ISSUER_URL` | `(empty)` | The provider's issuer; its discovery document lives at /.well-known/openid-configuration below it. |
| `OIDC_PROVIDER_NAME` | `SSO` | Shown on the login button. |
| `OPENSUBTITLES_API_KEY` | `(empty)` | From opensubtitles.com, API consumers. |
| `OPENSUBTITLES_LANGUAGES` | `(empty)` | Languages to fetch subtitles for. |
| `POPULAR_MOVIE_COUNT` | `0` | How many popular movies to add each run. 0 is off. |
| `POPULAR_TV_COUNT` | `0` | How many popular series to add each run. 0 is off. |
| `PREFER_SMALLER_FILES` | `true` | When two releases tie, take the smaller one. |
| `PRIME_NL_TOP_COUNT` | `0` | How many from the Prime Video top list. 0 is off. |
| `RADARR_API_KEY` | `(empty)` | Settings, General, API Key in Radarr. |
| `RADARR_QUALITY_PROFILE` | `(empty)` | Profile new entries get. Blank uses the first profile. |
| `RADARR_ROOT_FOLDER` | `(empty)` | Where mirrored movies are filed in Radarr. Blank uses its first root folder. |
| `RADARR_URL` | `(empty)` | Address Mycelium can reach Radarr on. |
| `REALDEBRID_API_KEY` | `(empty)` | From real-debrid.com, My account, API token. |
| `RESOLUTION_EXCLUDED` | `(empty)` | Drops a candidate whose resolution matches. Self-relaxes (with a log line) if it would empty the whole candidate pool, unless RESOLUTION_STRICT is set. |
| `RESOLUTION_INCLUDED` | `(empty)` | Rescues a matching candidate from every other rule, in every category, even `_REQUIRED`/`_EXCLUDED` on an unrelated category. The one setting that can seriously surprise you; use it deliberately. |
| `RESOLUTION_PREFERRED` | `1080p,2160p,720p` | Tie-break only, ranks a matching resolution ahead of others. Never rescues a value another rule dropped. |
| `RESOLUTION_REQUIRED` | `(empty)` | Drops every candidate whose resolution does not match. A release the scraper could not tag for this category ("unknown") always survives, it never counts as a mismatch. |
| `RESOLUTION_STRICT` | `false` | Reject non-matching resolution candidates outright instead of relaxing. |
| `SEASON_PACK_CONSOLIDATION_ENABLED` | `true` | Replace single episodes with a season pack once one is cached, so a season is one torrent. |
| `SEERR_API_KEY` | `(empty)` | Settings, General, API key in Seerr. |
| `SEERR_DECLINE_WANTED_AFTER_DAYS` | `30` | Days a released title may stay without a release before Seerr is told it was declined. 0 never declines. |
| `SEERR_REPORT_STATUS` | `true` | Mark a request available when its files exist, declined when it finally failed, and remove its media entry when purged. |
| `SEERR_URL` | `(empty)` | Address of Seerr, Overseerr or Jellyseerr. |
| `SONARR_API_KEY` | `(empty)` | Settings, General, API Key in Sonarr. |
| `SONARR_QUALITY_PROFILE` | `(empty)` | Profile new entries get. Blank uses the first profile. |
| `SONARR_ROOT_FOLDER` | `(empty)` | Where mirrored series are filed in Sonarr. Blank uses its first root folder. |
| `SONARR_URL` | `(empty)` | Address Mycelium can reach Sonarr on. |
| `SORT_ORDER` | `season_pack,resolution,language,source,encode,seeders,size` | Which properties decide between surviving releases, most important first. |
| `SOURCE_EXCLUDED` | `remux,cam,ts,tc,scr,r5,ppvrip,workprint` | Drops a candidate whose source matches. Self-relaxes (with a log line) if it would empty the whole candidate pool, unless SOURCE_STRICT is set. |
| `SOURCE_INCLUDED` | `(empty)` | Rescues a matching candidate from every other rule, in every category, even `_REQUIRED`/`_EXCLUDED` on an unrelated category. The one setting that can seriously surprise you; use it deliberately. |
| `SOURCE_PREFERRED` | `webdl,webrip,web` | Tie-break only, ranks a matching source ahead of others. Never rescues a value another rule dropped. |
| `SOURCE_REQUIRED` | `(empty)` | Drops every candidate whose source does not match. A release the scraper could not tag for this category ("unknown") always survives, it never counts as a mismatch. |
| `SOURCE_STRICT` | `false` | Reject non-matching source candidates outright instead of relaxing. |
| `TELEGRAM_BOT_TOKEN` | `(empty)` | Token from BotFather. |
| `TELEGRAM_CHAT_ID` | `(empty)` | The chat or channel the bot posts to. |
| `TMDB_API_KEY` | `(empty)` | A free key from themoviedb.org. Powers Discover, posters and the runtime in stub files. |
| `TORBOX_API_KEY` | `(empty)` | From TorBox, Settings, API. Required for everything. |
| `TRAKT_AUTO_REQUEST_CAP` | `10` | Most titles a Trakt watchlist sync may request per run. |
| `TRAKT_CLIENT_ID` | `(empty)` | From trakt.tv, Settings, Your API apps. |
| `TRAKT_CLIENT_SECRET` | `(empty)` | The secret of that API app. |
| `TRENDING_PRECACHE_COUNT` | `0` | How many trending movies to add each run. 0 is off. |
| `TRENDING_TV_COUNT` | `0` | How many trending series to add each run. 0 is off. |
| `TRUSTED_PROXY_AUTH` | `false` | Accept the user name from a header set by a reverse proxy such as Authelia. |
| `VISUAL_TAG_EXCLUDED` | `dv_only` | Drops a candidate whose visual tag matches. Self-relaxes (with a log line) if it would empty the whole candidate pool, unless VISUAL_TAG_STRICT is set. |
| `VISUAL_TAG_INCLUDED` | `(empty)` | Rescues a matching candidate from every other rule, in every category, even `_REQUIRED`/`_EXCLUDED` on an unrelated category. The one setting that can seriously surprise you; use it deliberately. |
| `VISUAL_TAG_PREFERRED` | `(empty)` | Tie-break only, ranks a matching visual tag ahead of others. Never rescues a value another rule dropped. |
| `VISUAL_TAG_REQUIRED` | `(empty)` | Drops every candidate whose visual tag does not match. A release the scraper could not tag for this category ("unknown") always survives, it never counts as a mismatch. |
| `VISUAL_TAG_STRICT` | `false` | Reject non-matching visual-tag candidates outright instead of relaxing. |
| `WEB_PLAYER_MAX_SIZE_GB` | `15` | Largest release the built-in web player will pick, in GB. |
| `ZILEAN_ENABLED` | `false` | Search a Zilean DMM index next to Torrentio for cached releases. |
| `ZILEAN_MODE` | `external` | External talks to a running Zilean service. Native imports its Postgres into a built-in index. |
| `ZILEAN_PG_DB` | `zilean` | Database name, usually zilean. |
| `ZILEAN_PG_HOST` | `(empty)` | Host name of Zilean's Postgres database. |
| `ZILEAN_PG_PASSWORD` | `(empty)` | Password for that user. |
| `ZILEAN_PG_PORT` | `5432` | Usually 5432. |
| `ZILEAN_PG_USER` | `postgres` | Database user with read access. |
| `ZILEAN_URL` | `(empty)` | Address of the Zilean service. |

### Advanced (35)

Listed in the Settings UI, behind "Advanced". Intervals, timeouts and
niche knobs most installs never touch.

<!-- tier: advanced -->
```
ARR_SYNC_INTERVAL_MINUTES
AUTO_APPROVE_INTERVAL_HOURS
AUTO_UPGRADE_INTERVAL_HOURS
BACKUP_INTERVAL_HOURS
BLACKLIST_FAIL_THRESHOLD
CATBOX_GC_INTERVAL_MINUTES
CATBOX_IDLE_MINUTES
CATCHUP_DELAY_SEC
CATCHUP_ENABLED
CATCHUP_TAKE
CLEANUP_INTERVAL_HOURS
DEBRIDIO_BASE_URL
DEBRIDIO_CONFIG_TOKEN
DEBRIDIO_MAX_RESULTS
DISK_SYNC_INTERVAL_MINUTES
EXCLUDE_UNDERSIZED_STRICT
HEALTH_CACHE_SECONDS
MAX_RETRY_ATTEMPTS
MEDIAFUSION_API_KEY
MERGE_VERSIONS_INTERVAL_HOURS
MONITOR_INTERVAL_HOURS
MOVIE_SYNC_INTERVAL_MINUTES
OIDC_SCOPES
OIDC_USER_CLAIM
OPENSUBTITLES_USER_AGENT
RETRY_QUEUE_INTERVAL_MINUTES
SEASON_PACK_CHECK_INTERVAL_HOURS
STRM_GENERATOR_INTERVAL_HOURS
TORBOX_BASE_URL
TORBOX_POLL_INTERVAL_SEC
TORBOX_POLL_TIMEOUT_SEC
TRENDING_CHECK_INTERVAL_HOURS
TRUSTED_PROXY_NETWORKS
TRUSTED_PROXY_USER_HEADER
WEBDAV_ENABLED
```

| Name | Default | Meaning |
|---|---|---|
| `ARR_SYNC_INTERVAL_MINUTES` | `60` | Adds what the arrs lack and purges what was deleted there. 0 disables it. |
| `AUTO_APPROVE_INTERVAL_HOURS` | `24` | Runs the genre and favourite-actor rules. |
| `AUTO_UPGRADE_INTERVAL_HOURS` | `24` | How often to look for upgrades. |
| `BACKUP_INTERVAL_HOURS` | `24` | Copies the database to the backups folder. |
| `BLACKLIST_FAIL_THRESHOLD` | `3` | A release that fails this many times is never tried again. |
| `CATBOX_GC_INTERVAL_MINUTES` | `10` | How often idle titles are looked for. Restart after changing. |
| `CATBOX_IDLE_MINUTES` | `1440` | How long a title may sit unplayed before its torrent is removed from TorBox again. 1440 is one day. |
| `CATCHUP_DELAY_SEC` | `30` | Seconds after boot before catch-up starts. |
| `CATCHUP_ENABLED` | `true` | After a restart, re-run requests that were interrupted. |
| `CATCHUP_TAKE` | `20` | Most requests to re-run in one catch-up. |
| `CLEANUP_INTERVAL_HOURS` | `24` | Removes dead and duplicate files. |
| `DEBRIDIO_BASE_URL` | `https://addon.debridio.com` | Leave the default unless you host the addon yourself. |
| `DEBRIDIO_CONFIG_TOKEN` | `(empty)` | Only if you built a config token in the addon yourself; blank lets Mycelium build one. |
| `DEBRIDIO_MAX_RESULTS` | `100` | Most results to take from one Debridio search. |
| `DISK_SYNC_INTERVAL_MINUTES` | `60` | Purges titles whose files were deleted. 0 disables it. |
| `EXCLUDE_UNDERSIZED_STRICT` | `false` | Keep dropping undersized releases even when nothing else is left. Off relaxes the rule rather than return nothing. |
| `HEALTH_CACHE_SECONDS` | `60` | How long the Overview health rows are cached. |
| `MAX_RETRY_ATTEMPTS` | `10` | Times a failed request is retried before it is declined for good. |
| `MEDIAFUSION_API_KEY` | `(empty)` | Only for a private instance: its API password. |
| `MERGE_VERSIONS_INTERVAL_HOURS` | `6` | Folds duplicate versions of a title together. |
| `MONITOR_INTERVAL_HOURS` | `6` | Looks for new episodes of monitored series. |
| `MOVIE_SYNC_INTERVAL_MINUTES` | `30` | Retries movies that were released but had no release yet. |
| `OIDC_SCOPES` | `openid email profile` | Scopes to request. The default covers a user name and email. |
| `OIDC_USER_CLAIM` | `preferred_username` | Which claim becomes the Mycelium user name. |
| `OPENSUBTITLES_USER_AGENT` | `Mycelium v1.0` | Sent with every OpenSubtitles request; they ask for an app name and version. |
| `RETRY_QUEUE_INTERVAL_MINUTES` | `15` | Retries requests that hit a temporary error. |
| `SEASON_PACK_CHECK_INTERVAL_HOURS` | `12` | How often to look for packs. |
| `STRM_GENERATOR_INTERVAL_HOURS` | `1` | Regenerates missing or expired .strm files. |
| `TORBOX_BASE_URL` | `https://api.torbox.app/v1/api` | Leave the default unless TorBox publishes a new API address. |
| `TORBOX_POLL_INTERVAL_SEC` | `2` | Seconds between checks while waiting for TorBox to finish caching a torrent. |
| `TORBOX_POLL_TIMEOUT_SEC` | `600` | Give up waiting for TorBox after this many seconds and report the title as failed for now. |
| `TRENDING_CHECK_INTERVAL_HOURS` | `24` | How often auto-add runs. |
| `TRUSTED_PROXY_NETWORKS` | `127.0.0.1/32` | Only requests from these networks (CIDR, comma separated) may carry that header; also decides which forwarded address the login rate limiter keys on. |
| `TRUSTED_PROXY_USER_HEADER` | `X-Forwarded-User` | Header carrying the user name. |
| `WEBDAV_ENABLED` | `false` | Serve the library over WebDAV as well. |

### Deployment (25)

Read only from the environment, with no field anywhere in the admin UI
(not in Settings, not in the Filtering rules tab): paths, network
binding, and secrets. Promised because they are how the container is run.

<!-- tier: deployment -->
```
AUTH_PASSWORD
AUTH_SESSION_SECRET
COOKIE_SECURE
DB_PATH
DEBRIDIO_SEND_TORBOX_KEY
LISTEN_HOST
LISTEN_PORT
LOG_LEVEL
MEDIA_PATH
METRICS_TOKEN
QUOTA_CHECK_INTERVAL_HOURS
QUOTA_WARN_SIZE_GB
QUOTA_WARN_TORRENT_COUNT
REALDEBRID_BASE_URL
RETRY_BACKOFF_MINUTES
SPORE_ENABLED
SPORE_MEDIA_PATH
SPORE_PORT
TORRENTIO_BASE_URL
TORRENTIO_OPTS
WANTED_RECHECK_INTERVAL_HOURS
WEBDAV_PATH_PREFIX
WEBDAV_URL_CACHE_TTL_SECONDS
WEBHOOK_SECRET
ZILEAN_DB_PATH
```

| Name | Default | Meaning |
|---|---|---|
| `AUTH_PASSWORD` | `(empty)` | Plain admin password read on first login and immediately upgraded to a scrypt hash; blank after that. |
| `AUTH_SESSION_SECRET` | `mycelium-please-change-me` | Signs the session cookie. Change it in production. |
| `COOKIE_SECURE` | `false` | Mark the session cookie Secure; needs HTTPS in front of Mycelium. |
| `DB_PATH` | `/data/requests.db` | Path to the SQLite database file. |
| `DEBRIDIO_SEND_TORBOX_KEY` | `false` | Send the TorBox key inside Debridio's config segment. Off by default: the addon does not validate it. |
| `LISTEN_HOST` | `0.0.0.0` | Interface the app (or the Go streaming front) binds to. |
| `LISTEN_PORT` | `8088` | Port the app (or the Go streaming front) listens on. |
| `LOG_LEVEL` | `INFO` | Root log level. |
| `MEDIA_PATH` | `/data/media` | Where .strm files and Spore stubs are written. |
| `METRICS_TOKEN` | `(empty)` | Token required to scrape /metrics, sent as the `X-Metrics-Token` header or a `metrics_token` query parameter; blank falls back to requiring an admin session. |
| `QUOTA_CHECK_INTERVAL_HOURS` | `0` | How often to check the TorBox quota warning. 0 disables it (TorBox paid plans have no hard storage limit). |
| `QUOTA_WARN_SIZE_GB` | `999999` | Library size that triggers a quota warning. |
| `QUOTA_WARN_TORRENT_COUNT` | `999999` | Torrent count that triggers a quota warning. |
| `REALDEBRID_BASE_URL` | `https://api.real-debrid.com/rest/1.0` | RealDebrid API base URL, used only when MULTI_DEBRID_ENABLED. |
| `RETRY_BACKOFF_MINUTES` | `60,360,1440` | Minutes to wait before each retry of a failed request, comma separated: the Nth retry waits the Nth value, the last value repeats after that. |
| `SPORE_ENABLED` | `false` | Turn on the Plex stub-MKV library and the spore-stream proxy. Not rolled out on the current VPS. |
| `SPORE_MEDIA_PATH` | `/data/plex-media` | Where Spore stub MKVs and the .fsh fast-start cache are written. |
| `SPORE_PORT` | `8089` | Port for the Spore TCP range server. |
| `TORRENTIO_BASE_URL` | `https://torrentio.strem.fun` | Torrentio scraper base URL. |
| `TORRENTIO_OPTS` | `(empty)` | Extra options appended to the Torrentio manifest path, as pasted from Torrentio's own configure page. |
| `WANTED_RECHECK_INTERVAL_HOURS` | `12` | How often a wanted movie with no acceptable release yet is re-searched. |
| `WEBDAV_PATH_PREFIX` | `/dav` | URL prefix the WebDAV server mounts the library under. |
| `WEBDAV_URL_CACHE_TTL_SECONDS` | `3600` | How long a resolved WebDAV CDN URL is cached. |
| `WEBHOOK_SECRET` | `(empty)` | Shared secret required on /webhook, /torbox-webhook and /webhook/arr. Blank means Mycelium generates one at first start, shown in Settings and served by `GET /ui/api/webhook-secret`; callers must send it either way. |
| `ZILEAN_DB_PATH` | `/data/zilean_native.db` | Path to the native Zilean SQLite index (ZILEAN_MODE=native). |

### Internal (13)

No field in the Settings UI at all (`settings._UNLISTED_KEYS`): retired
booleans from before the four-state filter-rule model, and one-time
migration markers. Not promised; may be removed without a deprecation
cycle.

<!-- tier: internal -->
```
ALLOW_4K
AUDIO_LANGUAGE_PREFERENCE
EXCLUDE_BLURAY
EXCLUDE_CAM
EXCLUDE_LANGUAGES
EXCLUDE_REMUX
FILTER_RULES_MIGRATED
PREFER_HEVC
PREFER_WEBDL
QUALITY_PREFERENCE
SCHEMA_VERSION
SOURCE_LABELS_MIGRATED
STRICT_NO_CAM
```

| Name | Default | Meaning |
|---|---|---|
| `ALLOW_4K` | `true` | Retired: replaced by RESOLUTION_EXCLUDED/RESOLUTION_REQUIRED. Kept so a stray .env value is not silently misread. |
| `AUDIO_LANGUAGE_PREFERENCE` | `(empty)` | Retired: replaced by LANGUAGE_PREFERRED. |
| `EXCLUDE_BLURAY` | `false` | Retired: replaced by SOURCE_EXCLUDED. |
| `EXCLUDE_CAM` | `true` | Retired: replaced by SOURCE_EXCLUDED. |
| `EXCLUDE_LANGUAGES` | `(empty)` | Retired: replaced by LANGUAGE_EXCLUDED. |
| `EXCLUDE_REMUX` | `true` | Retired: replaced by SOURCE_EXCLUDED. |
| `FILTER_RULES_MIGRATED` | `(unset)` | Internal marker; set once the one-time filter-rule migration has run. |
| `PREFER_HEVC` | `true` | Retired: replaced by ENCODE_PREFERRED. |
| `PREFER_WEBDL` | `true` | Retired: replaced by SOURCE_PREFERRED. |
| `QUALITY_PREFERENCE` | `1080p,2160p,720p` | Retired: replaced by RESOLUTION_PREFERRED. |
| `SCHEMA_VERSION` | `(unset)` | Internal marker; the database schema version last written after migrations, shown in the startup log (section 5, and docs/RECOVERY.md). |
| `SOURCE_LABELS_MIGRATED` | `(unset)` | Internal marker; set once the one-time source-label migration has run. |
| `STRICT_NO_CAM` | `false` | Retired: replaced by the SOURCE rule category's excluded/included/strict states. |

## 3. The deprecation rule

A promised variable or route (supported, advanced, deployment, or a frozen
route above) is removed only after one minor release in which it still
works and the startup log warns once, naming its replacement.
`deprecations.DEPRECATED` is the map (empty at 1.0); `app.py` calls
`deprecations.warn_deprecated_env()` right after the filter-rule migration
warning, once at startup. The changelog lists the name under
`### Deprecated` in the release that starts warning, and under
`### Removed` in the release that actually removes it. A guard test
(`tests/test_compatibility.py`) checks that every entry in the map names a
real replacement and has a line in `CHANGELOG.md`.

## 4. Breaking changes

Anything that breaks a promised surface (a frozen route above, or a
supported/advanced/deployment variable, changing shape or meaning without
going through the deprecation rule) gets a `### Breaking` heading in the
changelog entry and a major version bump. A deprecation followed by its
scheduled removal is not a breaking change; skipping the deprecation cycle
is.

## 5. Database

Forward migrations are idempotent: running the same migration twice, or
starting a container against a database a later version already migrated,
changes nothing further. Every 1.x database opens cleanly on any later 1.x
release. Rolling back one minor version leaves that release's new columns
and tables in place, unused, and otherwise changes nothing; there is
nothing else a rollback needs to undo. `docs/RECOVERY.md` verifies this
per release and lists exactly which columns and tables each release since
0.17 added. A `schema_version` setting, written once migrations finish,
drives a startup log line naming the schema version the database was last
migrated from.

## 6. Versioning

Semantic versioning: patch releases are fixes only, minor releases add
features and start a deprecation, major releases contain a breaking
change. `APP_VERSION` in `version.py` and the changelog's release heading
always agree; `releases.json` mirrors both, checked by a guard test.
