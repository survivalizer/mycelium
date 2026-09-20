"""Catbox-style lazy materialization for TorBox.

When CATBOX_MODE is enabled, .strm files contain a proxy URL pointing to
/stream/<token>. On playback the webhook ensures the torrent is in TorBox
(re-adding from the cached magnet if it has been released), fetches a fresh
CDN URL, and 307-redirects the client.

After CATBOX_IDLE_MINUTES of inactivity an item is removed from TorBox to
stay within TorBox's 30-day cache retention policy. The virtual entry stays
in the DB so playback works again on the next request.

Resolved CDN URLs are cached in-memory per token to avoid hammering TorBox's
60/hour createtorrent + 300/min general rate limits when Jellyfin sends
multiple probe/seek requests for the same item in quick succession.

Background jobs (release_idle, reconcile_torbox_ids) live in catbox_jobs.py
and season-pack reconciliation lives in catbox_packs.py; both import this
module for its private state. Re-exports (until 1.1): release_idle,
reconcile_torbox_ids, last_reconcile.
"""
import logging
import threading
import time
import uuid

import db
import settings as _settings
import torbox
from config import CATBOX_HOST

log = logging.getLogger(__name__)


# TorBox's requestdl opens a returned link for 3 hours; after that a new
# connection against it is refused, though a transfer already in flight
# continues. Cache inside that window with headroom, so a cached entry is
# never dead on arrival. The liveness check below stays as the backstop for
# links that die early.
_URL_CACHE_TTL_SEC = 9000  # 2.5 hours
ON_PLAY_READY_TIMEOUT_SEC = 45  # max wait on-play before giving up (cached = seconds)
_url_cache: dict[str, tuple[str, float]] = {}
_url_cache_lock = threading.Lock()


def _home(item: dict) -> int | None:
    """The account holding the item's torrent, or None when the item has
    no torrent on TorBox (or its home is disabled, which counts as none)."""
    import torbox_pool
    acct_id = item.get("torbox_account")
    if not acct_id or not item.get("torbox_id"):
        return None
    acct = torbox_pool.account(acct_id)
    if acct is None or not acct.enabled:
        return None
    return acct_id


def _adopt_or_choose(item: dict) -> tuple[int, dict | None]:
    """Where an unhomed torrent goes: the enabled account whose library
    already holds the hash and has it ready (no add needed); else the
    account that already holds the hash but is still downloading it (the
    add target, so a retry lands on the same account instead of spraying
    the magnet across the pool and spending a scarce uncached slot on a
    different account every play. TorBox answers DUPLICATE_ITEM for the
    re-add, releasing the reservation, and wait_until_ready polls there);
    else the pool's choice."""
    import torbox_pool
    h = item.get("info_hash") or ""
    unready_account = None
    if h:
        for acct in torbox_pool.accounts():
            try:
                existing = torbox.find_by_hash(acct.id, h)
            except torbox.AuthFailed:
                continue
            if not existing:
                continue
            if torbox._is_ready(existing):
                log.info("Catbox: %s found in %s's library (id=%s), adopting", item["title"], acct.label, existing["id"])
                return acct.id, existing
            if unready_account is None:
                log.info("Catbox: %s found unready on %s's library, using it as the add target", item["title"], acct.label)
                unready_account = acct.id
    if unready_account is not None:
        return unready_account, None
    return torbox_pool.choose_for_add().id, None

# Failure cooldown: after a failed materialize (429, timeout, no file found),
# block retries for a short window so Jellyfin's burst of probe requests doesn't
# hammer TorBox with repeated createtorrent calls.
_FAIL_COOLDOWN_SEC = 30        # standard failure (readd blocked, no file)
_FAIL_COOLDOWN_429_SEC = 120   # TorBox 429  -  back off longer
_fail_cache: dict[str, float] = {}  # token → expiry monotonic timestamp
_fail_cache_lock = threading.Lock()

# ── Reason codes (structured, for playability_state + admin UI) ───────────────
REASON_UNKNOWN_TOKEN    = "UNKNOWN_TOKEN"
REASON_NO_IMDB          = "NO_IMDB"
REASON_TORRENTIO_EMPTY  = "TORRENTIO_EMPTY"
REASON_NO_CACHED        = "NO_CACHED_RELEASE"
REASON_WAIT_TIMEOUT     = "WAIT_TIMEOUT"
REASON_NO_FILE          = "NO_FILE"
REASON_RD_429           = "RD_429"
REASON_TB_429           = "TB_429"
REASON_ADD_FAILED       = "ADD_FAILED"
REASON_SEARCH_ERROR     = "SEARCH_UNAVAILABLE"

def _fail_get(token: str) -> bool:
    with _fail_cache_lock:
        exp = _fail_cache.get(token)
        if exp is None:
            return False
        if exp > time.monotonic():
            return True
        del _fail_cache[token]
        return False

def _fail_put(token: str, ttl: int = _FAIL_COOLDOWN_SEC) -> None:
    with _fail_cache_lock:
        _fail_cache[token] = time.monotonic() + ttl


def _auth_failed(token: str, ckey: str | None, title: str) -> None:
    """A revoked/invalid TorBox key for the home account: same cooldown as
    the old 403-from-mylist case, so a bad key degrades to occasional
    retries instead of unwinding out of the play path as a 500."""
    log.warning("Catbox: TorBox account auth failed for %s  -  cooling down", title)
    _fail_put(token, _FAIL_COOLDOWN_429_SEC)
    if ckey:
        db.update_playability_fail(ckey, REASON_TB_429)


_token_locks: dict[str, threading.Lock] = {}

# Per-content search cache so Zilean/Torrentio are called at most once per hour
# for the same (imdb_id, season, episode) combo, regardless of how many tokens share it.
_search_cache: dict[tuple, tuple[float, object]] = {}  # key → (expiry, result)
_search_cache_lock = threading.Lock()
_SEARCH_HIT_TTL    = 300    # 5 min: re-check soon if a cached release was found
_SEARCH_MISS_TTL   = 21600  # 6 h:  nothing cached  -  back off (matches _fail_put below)
_token_locks_lock = threading.Lock()
_pack_locks: dict[str, threading.Lock] = {}

# ── scan/probe burst detection ────────────────────────────────────────────────
# A media-server library scan opens many DISTINCT .strm URLs in a short burst,
# whereas real playback touches a single token (plus seeks on that same token).
# When we see a burst of distinct tokens we treat the requests as scan probes and
# refuse to re-add idle-released torrents  -  re-materializing the whole library on
# every scan is slow and churns TorBox's createtorrent quota. Items already live
# in TorBox still resolve cheaply (mylist is cached), so they probe fine.
_SCAN_WINDOW_SEC = 25
# Deliberately above this deployment's realistic concurrent-user count (a
# handful of real users, see CLAUDE.md) so several people starting different
# titles within the same 25s window isn't misread as a library scan and
# denied re-add. A real scan opens far more than this many distinct items.
_SCAN_DISTINCT_THRESHOLD = 8
_recent_tokens: dict[str, float] = {}
_recent_lock = threading.Lock()


def _is_scan_burst(token: str) -> bool:
    """Record this token request and report whether we appear to be inside a
    library-scan burst (many distinct tokens within the recent window)."""
    now = time.monotonic()
    with _recent_lock:
        for t, ts in list(_recent_tokens.items()):
            if now - ts > _SCAN_WINDOW_SEC:
                del _recent_tokens[t]
        _recent_tokens[token] = now
        return len(_recent_tokens) >= _SCAN_DISTINCT_THRESHOLD


def _token_lock(token: str) -> threading.Lock:
    with _token_locks_lock:
        lock = _token_locks.get(token)
        if lock is None:
            lock = threading.Lock()
            _token_locks[token] = lock
        return lock


def _pack_lock(info_hash: str) -> threading.Lock:
    with _token_locks_lock:
        lock = _pack_locks.get(info_hash)
        if lock is None:
            lock = threading.Lock()
            _pack_locks[info_hash] = lock
        return lock


def _content_key(item: dict) -> str | None:
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        return None
    season, episode = item.get("season"), item.get("episode")
    if season and episode:
        return f"{imdb_id}:S{season:02d}E{episode:02d}"
    return imdb_id


_TOUCH_DEBOUNCE_SEC = 60  # last_played/play_count precision we actually need
_touch_cache: dict[str, float] = {}
_touch_cache_lock = threading.Lock()


def _touch_debounced(token: str) -> None:
    """db.touch_virtual_item() writes (UPDATE + commit) on every call. The two
    cache-hit call sites in materialize() run on every single byte-range
    request during active playback -- one player can trigger dozens of these
    a minute, each a synchronous SQLite write serializing against every other
    concurrent playback session's writes. play_count/last_played don't need
    per-chunk precision, so only actually write once per debounce window."""
    now = time.monotonic()
    with _touch_cache_lock:
        last = _touch_cache.get(token)
        if last is not None and now - last < _TOUCH_DEBOUNCE_SEC:
            return
        _touch_cache[token] = now
    db.touch_virtual_item(token)


def _cache_get(token: str) -> str | None:
    with _url_cache_lock:
        entry = _url_cache.get(token)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _url_cache[token]
    return None


def _cache_put(token: str, url: str) -> None:
    with _url_cache_lock:
        _url_cache[token] = (url, time.monotonic() + _URL_CACHE_TTL_SEC)


def cache_url(token: str, url: str) -> None:
    """Store a CDN URL in the in-memory cache (used by preload)."""
    _cache_put(token, url)


def cached_url(token: str) -> str | None:
    """The hot path of materialize() on its own: the cached CDN URL, or None
    without resolving anything. A hit counts as a play for the idle-release
    accounting, exactly as it does inside materialize, because a request
    answered from here never reaches /spore-stream."""
    cached = _cache_get(token)
    if cached:
        _touch_debounced(token)
    return cached


def invalidate_url_cache(token: str | None = None) -> None:
    with _url_cache_lock:
        if token is None:
            _url_cache.clear()
        else:
            _url_cache.pop(token, None)


def catbox_host() -> str:
    """Externally reachable host for the .strm proxy URL. Settings DB first,
    env/config fallback. Must be reachable from Jellyfin."""
    return (_settings.get("CATBOX_HOST", CATBOX_HOST) or "").strip()


def proxy_url(token: str) -> str:
    return f"{catbox_host().rstrip('/')}/stream/{token}"


def register(info_hash: str, magnet: str, title: str, media_type: str,
             strm_path: str | None = None, torbox_id: int | None = None,
             file_id: int | None = None, imdb_id: str | None = None,
             quality: str | None = None, source: str | None = None,
             size_gb: float | None = None, season: int | None = None,
             episode: int | None = None, year: int | None = None) -> str:
    token = uuid.uuid4().hex[:16]
    db.insert_virtual_item(token, info_hash, magnet, title, media_type,
                            strm_path=strm_path, torbox_id=torbox_id, file_id=file_id,
                            imdb_id=imdb_id, quality=quality, source=source,
                            size_gb=size_gb, season=season, episode=episode, year=year)
    return token


def materialize(token: str, allow_readd: bool | None = None) -> str | None:
    """Ensure the torrent is in TorBox and return a fresh stream URL.
    Cached URLs are served for up to _URL_CACHE_TTL_SEC (2.5 hours, inside
    TorBox's 3 hour link window) to absorb Jellyfin's probe/seek bursts
    without spending TorBox createtorrent rate-limit slots.

    allow_readd controls whether an idle-released torrent may be re-added (which
    can block ~45s waiting for it to become ready). When None (default), it is
    auto-decided: during a scan-burst we skip the re-add so the scan stays fast.
    """
    cached = _cache_get(token)
    if cached:
        _touch_debounced(token)
        return cached

    # Respect failure cooldown  -  don't spam TorBox after a recent failed attempt.
    if _fail_get(token):
        return None

    if allow_readd is None:
        allow_readd = not _is_scan_burst(token)

    with _token_lock(token):
        # Re-check inside the lock: another thread may have succeeded or set cooldown.
        cached = _cache_get(token)
        if cached:
            _touch_debounced(token)
            return cached
        if _fail_get(token):
            return None
        url = _materialize_locked(token, allow_readd=allow_readd)
        if url:
            _cache_put(token, url)
            _schedule_next_episode_preload(token)
        else:
            _fail_put(token)
        return url


def _rd_get_url(item: dict, rd_id: str) -> str | None:
    """Get a playable URL from RealDebrid for this virtual item."""
    import re as _re
    import realdebrid as _rd
    if item["media_type"] == "movie":
        return _rd.get_main_video_url(rd_id)
    # Episode: match SxxExx in filename
    pairs = _rd.get_video_files_with_urls(rd_id)
    if not pairs:
        return None
    s_num, e_num = item.get("season"), item.get("episode")
    if s_num and e_num:
        ep_re = _re.compile(rf'[Ss]0?{s_num}[Ee]0?{e_num}\b', _re.IGNORECASE)
        matched = [(f, u) for f, u in pairs if ep_re.search(f.get("path") or f.get("name") or "")]
        if matched:
            return matched[0][1]
    return max(pairs, key=lambda fu: fu[0].get("bytes") or 0)[1]


class _Stop:
    """A ladder step that has produced _materialize_locked's answer: `url`
    (None for a failure that already wrote its cooldown and playability row)
    is returned as-is, immediately."""
    __slots__ = ("url",)

    def __init__(self, url: str | None = None) -> None:
        self.url = url


def _materialize_locked(token: str, allow_readd: bool = True) -> str | None:
    item = db.get_virtual_item(token)
    if not item:
        log.warning("Catbox: unknown token %s", token)
        _metrics_inc("failed")
        return None

    ckey = _content_key(item)
    debrid_provider = (item.get("debrid_provider") or "torbox").lower()
    rematerialized = False

    if debrid_provider == "realdebrid":
        answer = _materialize_realdebrid(token, item, ckey, allow_readd)
        if answer is not None:
            return answer.url
        # The RD arm handed the item to TorBox: its search already counts
        # as a rematerialization.
        rematerialized = True

    acquired = _acquire_torbox(token, item, ckey, allow_readd, rematerialized)
    if isinstance(acquired, _Stop):
        return acquired.url
    torbox_id, account_id, rematerialized = acquired

    file_id = _resolve_file_id(token, item, ckey, account_id, torbox_id)
    if file_id is None:
        return None

    import strm_generator
    url = torbox.request_download_link(account_id, torbox_id, file_id)
    if url:
        db.touch_virtual_item(token)
        if ckey:
            db.update_playability_ok(ckey, "torbox")
        _metrics_inc("rematerialized" if rematerialized else "ok")
    else:
        _metrics_inc("failed")
    return url


def _materialize_realdebrid(token: str, item: dict, ckey: str | None,
                            allow_readd: bool) -> "_Stop | None":
    """The RealDebrid arm of _materialize_locked. Returns a _Stop holding
    the answer _materialize_locked owes its caller, or None when the search
    found a TorBox release instead and the caller must carry on with the
    TorBox ladder (`item` is updated in place for it)."""
    import realdebrid as _rd
    rd_id = item.get("rd_id")
    # Kept as a local so the metrics call below reads exactly as it did inside
    # _materialize_locked; the caller sets its own flag on the fall-through.
    rematerialized = False

    # Fast path: rd_id still live in RD library
    if rd_id:
        info = _rd.get_info(rd_id)
        if info and info.get("status") == "downloaded":
            url = _rd_get_url(item, rd_id)
            if url:
                db.touch_virtual_item(token)
                if ckey:
                    db.update_playability_ok(ckey, "realdebrid")
                _metrics_inc("ok" if not rematerialized else "rematerialized")
                return _Stop(url)
        log.info("Catbox/RD: %s no longer in RD library  -  will re-add", item["title"])
        db.update_virtual_rd_id(token, None)
        rd_id = None
        rematerialized = True

    if not allow_readd:
        log.debug("Catbox/RD: skipping re-add for %s during scan-burst probe", item["title"])
        return _Stop(None)

    rematerialized = True
    log.info("Catbox/RD: searching cached release for %s", item["title"])
    fresh = _search_cached_release(item)
    if fresh is _SEARCH_UNAVAILABLE:
        _fail_put(token, _FAIL_COOLDOWN_SEC)
        if ckey:
            db.update_playability_fail(ckey, REASON_SEARCH_ERROR)
        return _Stop(None)
    if not fresh:
        log.error("Catbox/RD: no cached release for %s  -  keeping .strm, retry in 6h",
                  item["title"])
        _fail_put(token, 21600)  # 6h  -  repair job will clean up if truly dead
        if ckey:
            db.update_playability_fail(ckey, REASON_NO_CACHED)
        return _Stop(None)

    new_hash, new_magnet, provider = fresh
    db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
    db.update_virtual_debrid_provider(token, provider)
    if provider == "torbox":
        # Search found TorBox  -  the caller carries on with the ladder
        item["debrid_provider"] = "torbox"
        item["info_hash"] = new_hash
        item["file_id"] = None
        return None
    else:
        try:
            result = _rd.add_magnet(new_magnet)
            rd_id = result["id"]
            rd_info = _rd.wait_until_ready(rd_id, timeout=ON_PLAY_READY_TIMEOUT_SEC)
            if not rd_info:
                log.error("Catbox/RD: wait_until_ready timed out for %s", item["title"])
                _fail_put(token, _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                return _Stop(None)
            db.update_virtual_rd_id(token, rd_id)
            url = _rd_get_url(item, rd_id)
            if url:
                db.touch_virtual_item(token)
                if ckey:
                    db.update_playability_ok(ckey, "realdebrid")
                _metrics_inc("rematerialized")
            return _Stop(url)
        except Exception as exc:
            is_429 = "429" in str(exc)
            log.error("Catbox/RD: add_magnet failed for %s: %s", item["title"], exc)
            _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_RD_429 if is_429 else REASON_ADD_FAILED)
            return _Stop(None)


def _acquire_torbox(token: str, item: dict, ckey: str | None, allow_readd: bool,
                    rematerialized: bool) -> "_Stop | tuple[int | None, int | None, bool]":
    """The TorBox acquisition ladder: the home still has it, adopt from a
    library that has it, re-add the stored magnet, the RealDebrid cache, a
    full search. Returns (torbox_id, account_id, rematerialized) once the
    torrent is ready, or a _Stop holding what _materialize_locked must
    return (a cooldown failure, or a URL the RealDebrid detour produced)."""
    torbox_id = item["torbox_id"]
    account_id = _home(item)
    if torbox_id and account_id is None:
        # Homed on an account that no longer exists or is disabled.
        log.info("Catbox: %s was on a disabled account, re-homing", item["title"])
        torbox_id = None
        rematerialized = True

    # Fast path: the home still has the torrent.
    if torbox_id:
        try:
            live = torbox.find_by_id(account_id, torbox_id)
        except torbox.AuthFailed:
            live = None
        if not live or not torbox._is_ready(live):
            torbox_id = None
            rematerialized = True

    # Unhomed: adopt from a library that has it, else add to the pool's choice.
    # `choose_for_add()` raises when no account is enabled; caught here (same
    # cooldown as a failed add below) rather than surfacing as a 500.
    if not torbox_id and item.get("info_hash"):
        try:
            account_id, existing = _adopt_or_choose(item)
        except Exception as exc:
            log.warning("Catbox: could not choose a TorBox account for %s: %s", item["title"], exc)
            _fail_put(token, _FAIL_COOLDOWN_429_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_TB_429)
            return _Stop(None)
        if existing:
            torbox_id = existing["id"]
            db.set_virtual_torbox(token, torbox_id, account_id)

    # Third chance: use the stored magnet to add directly  -  covers both items that
    # previously had a torbox_id (fell out of mylist top-1000) and freshly lazy-
    # registered items (torbox_id=NULL, magnet already selected at request time).
    if not torbox_id and item.get("magnet") and allow_readd:
        try:
            log.info("Catbox: %s adding stored magnet to account %s", item["title"], account_id)
            added = torbox.add_magnet(account_id, item["magnet"], reason="catbox-readd")
            _tid = added.get("id") or added.get("torrent_id")
            existing = added if _tid and torbox._is_ready(added) else (
                torbox.find_by_id(account_id, _tid) if _tid else
                torbox.find_by_hash(account_id, item["info_hash"], force_refresh=True)
            )
            if not (existing and torbox._is_ready(existing)) and _tid:
                existing = torbox.wait_until_ready(
                    account_id, item["info_hash"], timeout=ON_PLAY_READY_TIMEOUT_SEC, torrent_id=_tid)
            if existing and torbox._is_ready(existing):
                torbox_id = existing["id"]
                db.set_virtual_torbox(token, torbox_id, account_id)
                log.info("Catbox: %s added via stored magnet (id=%s, account %s)", item["title"], torbox_id, account_id)
        except torbox.AuthFailed:
            _auth_failed(token, ckey, item["title"])
            return _Stop(None)
        except Exception as exc:
            exc_str = str(exc)
            is_rate_limited = isinstance(exc, torbox.RateLimited) or "429" in exc_str or "403" in exc_str
            log.warning("Catbox: stored-magnet re-add failed for %s on account %s: %s", item["title"], account_id, exc)
            if is_rate_limited:
                # 429 = rate limited; 403 = API key/plan issue  -  either way
                # there is no point continuing to checkcached, it will also fail.
                _fail_put(token, _FAIL_COOLDOWN_429_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_TB_429)
                return _Stop(None)

    # Fourth chance: known hash may be cached on RD even if TorBox doesn't have it.
    # This avoids a full Torrentio search for items where Torrentio returns 0 results.
    if not torbox_id and item.get("info_hash") and allow_readd:
        try:
            import realdebrid as _rd
            if _rd.is_configured():
                known_hash = item["info_hash"].lower()
                rd_instant = _rd.check_cached([known_hash])
                if known_hash in {h.lower() for h in rd_instant}:
                    log.info("Catbox: known hash cached on RD for %s  -  switching to RD path",
                             item["title"])
                    db.update_virtual_debrid_provider(token, "realdebrid")
                    magnet = item.get("magnet") or f"magnet:?xt=urn:btih:{known_hash}"
                    rd_result = _rd.add_magnet(magnet)
                    rd_id = rd_result["id"]
                    rd_info = _rd.wait_until_ready(rd_id, timeout=ON_PLAY_READY_TIMEOUT_SEC)
                    if rd_info:
                        db.update_virtual_rd_id(token, rd_id)
                        url = _rd_get_url(item, rd_id)
                        if url:
                            db.touch_virtual_item(token)
                            if ckey:
                                db.update_playability_ok(ckey, "realdebrid")
                            _metrics_inc("rematerialized")
                            return _Stop(url)
                    log.error("Catbox: RD wait_until_ready timed out for %s", item["title"])
                    _fail_put(token, _FAIL_COOLDOWN_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return _Stop(None)
        except Exception as exc:
            log.warning("Catbox: RD known-hash check failed for %s: %s", item["title"], exc)

    if not torbox_id and not allow_readd:
        log.debug("Catbox: skipping re-add for %s during scan-burst probe", item["title"])
        return _Stop(None)

    if not torbox_id:
        rematerialized = True
        log.info("Catbox: searching fresh cached release for %s", item["title"])
        fresh = _search_cached_release(item)
        if fresh is _SEARCH_UNAVAILABLE:
            _fail_put(token, _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_SEARCH_ERROR)
            return _Stop(None)
        if not fresh:
            log.error("Catbox: no cached release found for %s  -  keeping .strm, retry in 6h",
                      item["title"])
            _fail_put(token, 21600)  # 6h  -  repair job will clean up if truly dead
            if ckey:
                db.update_playability_fail(ckey, REASON_NO_CACHED)
            return _Stop(None)

        new_hash, new_magnet, provider = fresh
        db.update_virtual_debrid_provider(token, provider)
        if provider == "realdebrid":
            # Search found RD  -  switch provider and handle via RD
            import realdebrid as _rd
            db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
            try:
                result = _rd.add_magnet(new_magnet)
                rd_id = result["id"]
                rd_info = _rd.wait_until_ready(rd_id, timeout=ON_PLAY_READY_TIMEOUT_SEC)
                if not rd_info:
                    _fail_put(token, _FAIL_COOLDOWN_SEC)
                    if ckey:
                        db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                    return _Stop(None)
                db.update_virtual_rd_id(token, rd_id)
                item["rd_id"] = rd_id
                url = _rd_get_url(item, rd_id)
                if url:
                    db.touch_virtual_item(token)
                    if ckey:
                        db.update_playability_ok(ckey, "realdebrid")
                    _metrics_inc("rematerialized")
                return _Stop(url)
            except Exception as exc:
                is_429 = "429" in str(exc)
                log.error("Catbox: RD add_magnet failed for %s: %s", item["title"], exc)
                _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_RD_429 if is_429 else REASON_ADD_FAILED)
                return _Stop(None)

        if new_hash != (item.get("info_hash") or "").lower():
            log.info("Catbox: swapping hash %s → %s", item["title"], new_hash)
            db.update_virtual_item_upgrade(token, new_hash, new_magnet, None, None)
            item["info_hash"] = new_hash
            item["file_id"] = None

        try:
            added = torbox.add_magnet(account_id, new_magnet, reason="catbox-search", cached=True)
            # Use the ID from the add response to avoid a full mylist refresh.
            # TorBox returns "torrent_id" for cached adds, "id" for others.
            _tid = added.get("id") or added.get("torrent_id")
            live = added if _tid and torbox._is_ready(added) else None
            if not live:
                live = torbox.find_by_id(account_id, _tid) if _tid else None
            if not live or not torbox._is_ready(live):
                live = torbox.wait_until_ready(
                    account_id, new_hash, timeout=ON_PLAY_READY_TIMEOUT_SEC, torrent_id=_tid or None)
            if not live:
                log.error("Catbox: fresh release not ready for %s  -  keeping .strm, retry soon",
                          item["title"])
                _fail_put(token, _FAIL_COOLDOWN_SEC)
                if ckey:
                    db.update_playability_fail(ckey, REASON_WAIT_TIMEOUT)
                return _Stop(None)
            torbox_id = live["id"]
            db.set_virtual_torbox(token, torbox_id, account_id)
        except Exception as exc:
            is_429 = isinstance(exc, torbox.AuthFailed) or "429" in str(exc)
            log.error("Catbox: add_magnet failed for %s: %s", token, exc)
            _fail_put(token, _FAIL_COOLDOWN_429_SEC if is_429 else _FAIL_COOLDOWN_SEC)
            if ckey:
                db.update_playability_fail(ckey, REASON_TB_429 if is_429 else REASON_ADD_FAILED)
            return _Stop(None)

    return torbox_id, account_id, rematerialized


def _resolve_file_id(token: str, item: dict, ckey: str | None,
                     account_id: int | None, torbox_id: int | None) -> int | None:
    """Which file inside the torrent this token plays. None means no playable
    file was found: the cooldown and the playability row are already written
    and _materialize_locked returns None."""
    file_id = item["file_id"]
    is_episode = item["media_type"] != "movie" and item.get("season") and item.get("episode")
    if file_id is not None and is_episode and db.hash_has_duplicate_file_ids(item["info_hash"]):
        # Two episodes of this pack point at one file: the old largest-file
        # fallback. Match the pack's files again for the whole season.
        file_id = None
    # TorBox file ids start at 0, so a preset 0 is a known file, not "unknown":
    # `if not file_id` sent the first episode of every pack through the
    # listing again and failed the play when the ?id= endpoint omitted files.
    if file_id is None:
        try:
            live = torbox.find_by_id(account_id, torbox_id)
        except torbox.AuthFailed:
            _auth_failed(token, ckey, item["title"])
            return None
        if live:
            import strm_generator
            if item["media_type"] == "movie":
                main = strm_generator._pick_main_movie_file(live.get("files") or [])
                if main:
                    file_id = main["id"]
                    db.update_virtual_file_id(token, file_id)
                elif not (live.get("files")):
                    # TorBox returned the torrent without a files list (common for the
                    # ?id= single-item endpoint).  Use file_id=0 which tells TorBox to
                    # serve the largest file automatically  -  works for single-file movies.
                    log.info("Catbox: no files list for %s  -  using file_id=0 (auto)", item["title"])
                    file_id = 0
            elif is_episode:
                from catbox_packs import reconcile_pack_files
                file_id = reconcile_pack_files(token, item, live)
            else:
                videos = [f for f in (live.get("files") or [])
                          if strm_generator._is_video(f.get("name") or "")
                          and not strm_generator._is_trailer(f)]
                main = max(videos, key=lambda f: f.get("size") or 0) if videos else None
                if main:
                    file_id = main["id"]
                    db.update_virtual_file_id(token, file_id)

    if file_id is None or (not file_id and file_id != 0):
        log.error("Catbox: no playable file found for %s  -  keeping .strm, retry later", token)
        _fail_put(token, _FAIL_COOLDOWN_SEC)
        if ckey:
            db.update_playability_fail(ckey, REASON_NO_FILE)
        return None

    return file_id


def _metrics_inc(result: str) -> None:
    try:
        import metrics_prom
        metrics_prom.catbox_stream_total.labels(result=result).inc()
    except Exception:
        pass


_SEARCH_UNAVAILABLE = object()  # sentinel: search couldn't run (no imdb_id, network error)


def _search_cached_release(item: dict) -> object:
    """Thin cache layer around _search_best_cached_release.

    Deduplicates Zilean/Torrentio calls when multiple tokens share the same
    (imdb_id, season, episode).  A miss is cached for 6 h, a hit for 5 min
    (so a newly-cached release is picked up quickly on retry).
    """
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        # No imdb_id → _search_best_cached_release will handle + log the warning.
        return _search_best_cached_release(item)
    key = (imdb_id, item.get("season"), item.get("episode"))
    now = time.monotonic()
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry and entry[0] > now:
            result = entry[1]
            log.debug("Catbox search cache hit for %s %s  -  skipping Zilean/Torrentio",
                      imdb_id, key[1:])
            return result
    result = _search_best_cached_release(item)
    if result is _SEARCH_UNAVAILABLE:
        # Don't cache the outage sentinel: the token's own retry cooldown is
        # _FAIL_COOLDOWN_SEC (30s), but caching it here would fall through to
        # _SEARCH_MISS_TTL (6h) below, same as a real miss - the title would
        # not be re-searched again until long after any real outage ended.
        return result
    ttl = _SEARCH_HIT_TTL if result else _SEARCH_MISS_TTL
    with _search_cache_lock:
        _search_cache[key] = (now + ttl, result)
    return result


def _search_best_cached_release(item: dict) -> tuple[str, str] | None | object:
    """Search Torrentio for the best currently-cached release for this item.

    Returns:
      (info_hash, magnet)   -  found a cached release
      None                  -  searched OK, nothing cached right now
      _SEARCH_UNAVAILABLE   -  couldn't search (no imdb_id, network error)  -  do NOT remove .strm
    """
    imdb_id = item.get("imdb_id")
    if not imdb_id:
        # Try to resolve imdb_id from TMDB using title + year, then persist it.
        try:
            import tmdb as _tmdb
            kind = "movie" if item.get("media_type") == "movie" else "tv"
            title = item.get("title") or ""
            year = item.get("year")
            results = _tmdb._get("/search/" + ("movie" if kind == "movie" else "tv"),
                                  params={"query": title, "year": year or ""}) or {}
            hits = results.get("results") or []
            if hits:
                tmdb_id = hits[0]["id"]
                imdb_id = _tmdb.tmdb_to_imdb(tmdb_id, media_type=kind)
                if imdb_id:
                    db.update_virtual_item_imdb(item["token"], imdb_id)
                    log.info("Catbox search: resolved imdb_id %s for %s via TMDB",
                             imdb_id, title)
        except Exception as exc:
            log.warning("Catbox search: TMDB lookup failed for %s: %s", item.get("title"), exc)
    if not imdb_id:
        log.warning("Catbox search: no imdb_id for %s  -  keeping .strm, will retry later",
                    item["title"])
        return _SEARCH_UNAVAILABLE
    try:
        import scrapers
        import debrid
        import blacklist
        media_type = item["media_type"]
        season = item.get("season")
        episode = item.get("episode")

        try:
            ranked = scrapers.fetch_candidates(
                "movie" if media_type == "movie" else "series",
                imdb_id, season=season, episode=episode,
                raise_if_inconclusive=True,
            )
        except scrapers.ScrapersUnavailable as exc:
            # "Could not search" is not "nothing is cached": the caller backs
            # off 6h on a real miss, which would outlive the outage by hours.
            log.warning("Catbox search: no scraper could be searched for %s (%s)"
                        "  -  keeping .strm", item.get("title"), exc)
            return _SEARCH_UNAVAILABLE
        if not ranked:
            return None
        ranked = blacklist.filter_candidates(ranked)
        if media_type != "movie":
            ranked = blacklist.filter_for_episode(ranked, imdb_id, season, episode)
        log.info("Catbox search: %d candidate(s) after ranking/filter for %s",
                 len(ranked), item.get("title"))
        if not ranked:
            return None
        hashes = [s.info_hash for s in ranked]
        cache_results = debrid.check_cached_multi(hashes)
        rd_cached = cache_results.get("realdebrid", set())
        tb_cached = cache_results.get("torbox", set())
        log.info("Catbox search: RD=%d TB=%d cached out of %d for %s",
                 len(rd_cached), len(tb_cached), len(ranked), item.get("title"))
        # RD first, TorBox fallback
        for s in ranked:
            if s.info_hash in rd_cached:
                return s.info_hash.lower(), s.magnet, "realdebrid"
        for s in ranked:
            if s.info_hash in tb_cached:
                return s.info_hash.lower(), s.magnet, "torbox"
        return None
    except Exception as exc:
        log.warning("Catbox search: failed for %s: %s  -  keeping .strm", item["title"], exc)
        return _SEARCH_UNAVAILABLE


def _schedule_next_episode_preload(token: str) -> None:
    """After a series episode materializes successfully, preload the next episode
    in background so it is instant when the user gets there.

    Lookup order: same season episode+1, then season+1 episode 1.
    Only fires if CATBOX_PRELOAD is enabled and the next episode has a
    registered virtual_item with info_hash + magnet."""
    try:
        import settings as _s
        import config as _cfg
        if not _s.get("CATBOX_PRELOAD", _cfg.CATBOX_PRELOAD):
            return
        item = db.get_virtual_item(token)
        if not item or item.get("media_type") != "series":
            return
        imdb_id = item.get("imdb_id")
        season = item.get("season")
        episode = item.get("episode")
        if not (imdb_id and season and episode):
            return
        # Try next episode in same season, then first episode of the next season
        nxt = db.get_virtual_item_by_episode(imdb_id, season, episode + 1)
        if not nxt:
            nxt = db.get_virtual_item_by_episode(imdb_id, season + 1, 1)
        if not nxt:
            return
        next_hash = nxt.get("info_hash")
        next_magnet = nxt.get("magnet")
        next_title = nxt.get("title") or ""
        if not (next_hash and next_magnet):
            return
        import strm_generator as _sg
        import threading as _t
        _t.Thread(
            target=_sg._preload_torrent,
            args=(next_hash, next_magnet, next_title),
            daemon=True,
        ).start()
        log.debug("Catbox: scheduled preload for next episode %s", next_title)
    except Exception as exc:
        log.debug("Catbox: next-episode preload scheduling failed: %s", exc)


def _sweep_caches() -> None:
    """Prune expired entries from the in-memory caches that are only cleaned
    on read (_url_cache, _fail_cache, _search_cache) or on next scan-burst
    check (_recent_tokens). A token that's cached once and never queried
    again would otherwise sit in memory forever on a long-running instance.

    _token_locks is deliberately NOT swept here: a lock object can be handed
    out to a caller and acquired moments after this check finds it free,
    so deleting it here could let two callers end up serialized on two
    different Lock objects for the same token instead of one - a real
    correctness bug, not just a leak. Left as a known, harmless memory growth."""
    now_mono = time.monotonic()

    with _url_cache_lock:
        for t in [t for t, (_, exp) in _url_cache.items() if exp <= now_mono]:
            del _url_cache[t]

    with _fail_cache_lock:
        for t in [t for t, exp in _fail_cache.items() if exp <= now_mono]:
            del _fail_cache[t]

    with _touch_cache_lock:
        for t in [t for t, ts in _touch_cache.items() if now_mono - ts > _TOUCH_DEBOUNCE_SEC]:
            del _touch_cache[t]

    with _search_cache_lock:
        for k in [k for k, (exp, _) in _search_cache.items() if exp <= now_mono]:
            del _search_cache[k]

    with _recent_lock:
        for t in [t for t, ts in _recent_tokens.items() if now_mono - ts > _SCAN_WINDOW_SEC]:
            del _recent_tokens[t]


def release_idle() -> int:
    """Re-export, removed in 1.1: use catbox_jobs.release_idle."""
    import catbox_jobs
    return catbox_jobs.release_idle()


def reconcile_torbox_ids() -> dict:
    """Re-export, removed in 1.1: use catbox_jobs.reconcile_torbox_ids."""
    import catbox_jobs
    return catbox_jobs.reconcile_torbox_ids()


def last_reconcile() -> dict | None:
    """Re-export, removed in 1.1: use catbox_jobs.last_reconcile."""
    import catbox_jobs
    return catbox_jobs.last_reconcile()
