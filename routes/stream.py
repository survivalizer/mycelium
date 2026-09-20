"""The play path: the catbox redirect, the moov-first proxy, the resolve and
report endpoints the Go front calls, and the WebDAV share."""
import logging
import threading

from flask import (Blueprint, Response, abort, jsonify, redirect, request,
                   stream_with_context, url_for)

import auth
import backup
import catbox
import config as cfg
import db
import egress_estimate
import settings as _settings_mod
import strm_generator
import stream_decisions
import torbox
from appcore import _csrf

log = logging.getLogger("mycelium")

bp = Blueprint("stream", __name__)


def _from_loopback() -> bool:
    """True when the request reached this process over the container's own
    loopback interface.

    The only honest signal available here: the Go front and the two share
    helpers all talk to gunicorn over 127.0.0.1, while anything arriving
    from the outside carries the proxy's address (or the client's own).
    X-Forwarded-For is not consulted on purpose, since a caller can set it.
    """
    return request.remote_addr in ("127.0.0.1", "::1")


# ── Catbox lazy materialization ───────────────────────────────────────────────

def _parse_byte_range(range_hdr: str, file_size: int) -> tuple[int, int]:
    """Parse a single-range RFC 7233 'Range: bytes=...' header.

    Handles all three forms: 'start-end', 'start-' (to EOF), and the suffix
    form '-N' (last N bytes) - the suffix form was previously parsed as
    start=0,end=N (first N bytes) instead of the last N, since split("-", 1)
    on "-500" yields ("", "500") same as the "start-" case would if start
    were empty. Raises ValueError on anything unparsable."""
    _, ranges_str = range_hdr.split("=", 1)
    r_start_s, r_end_s = ranges_str.split("-", 1)
    if not r_start_s:
        if not r_end_s:
            raise ValueError(f"empty range: {range_hdr!r}")
        suffix_len = int(r_end_s)
        r_start = max(0, file_size - suffix_len)
        r_end = file_size - 1
    else:
        r_start = int(r_start_s)
        r_end = int(r_end_s) if r_end_s else file_size - 1
    if r_start < 0 or r_start >= file_size or r_start > r_end:
        # e.g. bytes=999999999- on a smaller file: without this, callers would
        # clamp r_end to file_size-1 and compute a negative Content-Length
        # instead of the correct 416 Range Not Satisfiable.
        raise ValueError(f"range {range_hdr!r} not satisfiable for file_size={file_size}")
    return r_start, r_end


@bp.get("/stream/<token>")
def stream_redirect(token: str):
    """Catbox endpoint: 302 to the CDN when the address is warm, else 302
    to /spore-stream/<token> (moov-first proxy), which decides for real.

    A media server that relays for its client (Jellyfin does, for every
    Range request the client makes) follows both redirects on every request
    of the play, so once the address is known and alive this hop answers
    with it directly and the relay saves a round trip through the proxy
    per request. Anything else (a first play, an MP4 served moov-first, an
    expired liveness entry) still goes through spore-stream, which handles
    catbox materialization itself.
    """
    ua  = request.headers.get("User-Agent", "?")[:80]
    rng = request.headers.get("Range", "-")
    cdn_url = _warm_redirect_url(token)
    if cdn_url:
        log.info("stream: token=%s → CDN (warm) ua=%r range=%s", token, ua, rng)
        return redirect(cdn_url, code=302)
    log.info("stream: token=%s → /spore-stream/ ua=%r range=%s", token, ua, rng)
    return redirect(f"/spore-stream/{token}", code=302)


def _warm_redirect_url(token: str) -> str | None:
    """The CDN address /stream/<token> may hand out itself, or None when
    /spore-stream has to decide. Reads only what is already known (the URL
    cache, the .fsh record's fixed fields, the liveness cache): this hop
    never resolves, never probes inline, never touches TorBox. A stale
    liveness entry is used while a background probe refreshes it."""
    import time as _t
    import mp4_faststart

    cdn_url = catbox.cached_url(token)
    if not cdn_url:
        return None
    info = mp4_faststart.load_meta(token)
    state = stream_decisions.warm_link_state(
        info, cdn_url, _spore_alive_cache, _t.monotonic(), _ALIVE_STALE_GRACE_SEC)
    if state is None:
        return None
    if state == stream_decisions.STALE:
        _refresh_alive_async(cdn_url)
    # No bytes pass through us on this branch either: same estimate as the
    # redirect branch of _prepare_fast (see egress_estimate.py).
    egress_estimate.note_redirect(token, info["cdn_size"])
    return cdn_url


_spore_cold_sizes: dict = {}  # token -> file_size, avoids repeated HEAD on CDN
_spore_probing: set  = set()  # tokens currently running a background probe

# Short-lived "confirmed alive" cache for the MKV-sentinel CDN liveness check
# below, keyed by url. A single playback session can reopen the source (seek,
# HLS-transcode restart, client reconnect) several times a minute; without
# this, each reopen pays a fresh round trip to the CDN just to confirm a link
# it already confirmed moments ago is still alive. Past the TTL the entry is
# still handed out for the grace period while a background probe refreshes
# it, so a steady play never pays the round trip on the request thread; the
# price is that a link that died inside the grace is handed out until the
# probe returns (a few seconds) and forgets it; the next request re-resolves.
_ALIVE_CHECK_TTL_SEC = 120
_ALIVE_STALE_GRACE_SEC = 480
_spore_alive_cache: dict = {}  # cdn_url -> expiry monotonic timestamp
_spore_alive_refreshing: set = set()  # urls with a background probe running


def _refresh_alive_async(cdn_url: str) -> None:
    """Re-probe a stale liveness entry off the request thread, one probe
    per url at a time."""
    if cdn_url in _spore_alive_refreshing:
        return
    _spore_alive_refreshing.add(cdn_url)

    def _run():
        import time as _t
        try:
            stream_decisions.refresh_link(
                cdn_url, _head_status, _spore_alive_cache, _t.monotonic(),
                _ALIVE_CHECK_TTL_SEC)
        finally:
            _spore_alive_refreshing.discard(cdn_url)

    try:
        threading.Thread(target=_run, daemon=True, name="alive-refresh").start()
    except Exception:
        # Never leave the url marked as refreshing with no probe running.
        _spore_alive_refreshing.discard(cdn_url)
        raise


@bp.get("/spore-nfs/tree")
def spore_nfs_tree():
    """Virtual directory tree for the spore-nfs server: one entry per playable
    virtual item, reusing the same movies/series folder layout as the Jellyfin
    .strm tree (strm_path), just with the extension swapped for the real
    media container instead of .strm. spore-nfs polls this to build its
    in-memory filesystem; it does not touch the filesystem itself.

    Loopback-only, like the /internal/* endpoints: the listing names every
    playable token, and a token is an unauthenticated capability link, so
    an outside caller must not be able to enumerate the library. spore-nfs
    runs in this container and reaches gunicorn over 127.0.0.1; the Go
    front refuses to proxy /spore-nfs/ from outside as a second line."""
    if not _from_loopback():
        abort(404)
    from pathlib import Path
    entries = []
    for item in db.get_all_virtual_items():
        strm_path_str = item.get("strm_path")
        if not strm_path_str:
            continue
        parts = Path(strm_path_str).parts
        # Take everything from the last "movies"/"series" segment onward,
        # regardless of what absolute prefix precedes it: strm_path in the
        # DB isn't guaranteed to have been written with today's MEDIA_PATH
        # (older rows can predate an env change or come from a different
        # mount context), so relative_to() against the current MEDIA_PATH
        # silently drops anything that doesn't match exactly.
        rel_idx = None
        for i in range(len(parts) - 1, -1, -1):
            if parts[i] in ("movies", "series"):
                rel_idx = i
                break
        if rel_idx is None:
            continue
        rel = Path(*parts[rel_idx:])
        entries.append({
            "token": item["token"],
            # Real container is unknown until first probe; .mkv is a safe
            # default since our proxy transparently redirects/remuxes
            # regardless of the client-visible extension.
            "path": str(rel.with_suffix(".mkv")),
        })
    return jsonify({"entries": entries})


@bp.get("/spore-nfs/size/<token>")
def spore_nfs_size(token: str):
    """Cheap file-size lookup for spore-nfs's Attr()/ReadDir(): a TorBox
    checkcached call, which reports cached files without adding anything to
    the account. Used for library scans; actual playback still goes through
    /spore-stream/<token>, which materializes for real.

    Loopback-only for the same reason as the tree endpoint above: it would
    otherwise confirm to any caller which tokens exist and how large their
    files are."""
    if not _from_loopback():
        abort(404)
    item = db.get_virtual_item(token)
    if not item:
        abort(404)
    by_hash = torbox.check_cached_files([item["info_hash"]])
    entry = by_hash.get(item["info_hash"].lower())
    if not entry:
        # Not cached (or a torrent Mycelium has never checked yet): no size
        # to report without materializing, which a bulk scan must not do.
        return jsonify({"size": 0})
    files = entry.get("files") or []
    main = strm_generator._pick_main_movie_file(files) if files else None
    # Single-file torrents (the common case for movies) have no "files"
    # list at all: size/name live directly on the entry.
    size = main.get("size") if main else entry.get("size")
    return jsonify({"size": int(size or 0)})


def _build_then_probe(cdn_url_: str, tok: str) -> None:
    """Build the .fsh moov-first cache, then ffprobe the CDN file once to
    learn its real tracks. Runs on a background thread; never on the
    serving path."""
    import json as _json, subprocess as _sp
    import strm_generator as _sg, db as _db, mp4_faststart as _fs
    try:
        ok = _fs.build_and_cache(cdn_url_, tok)
        if not ok:
            return

        # Skip if already probed and preferred_audio detection is done
        existing = _db.load_spore_tracks(tok)
        if existing and "preferred_audio_idx" in existing:
            return

        cp = _fs.extract_codec_private(tok)
        v_extra_hex = cp.hex() if cp else ""

        res = _sp.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", cdn_url_],
            capture_output=True, timeout=60,
        )
        if res.returncode != 0:
            return
        data    = _json.loads(res.stdout)
        streams = data.get("streams", [])
        audio   = [s for s in streams if s.get("codec_type") == "audio"]
        subs    = [s for s in streams if s.get("codec_type") == "subtitle"]
        dur     = float(data.get("format", {}).get("duration", 0) or 0)
        preferred_idx = _sg._preferred_audio_index(audio)
        _db.save_spore_tracks(tok, {
            "audio": audio, "subs": subs, "duration_s": dur,
            "video_extradata_hex": v_extra_hex,
            "preferred_audio_idx": preferred_idx,
        })
        if audio or subs or dur or v_extra_hex:
            _sg.update_stub_from_probe(tok, audio, subs, duration_s=dur or None)
        if preferred_idx > 0:
            _sg.update_minfo_preferred_audio(tok, preferred_idx)
            log.info("spore-stream: preferred_audio=%d for token=%s (TrueHD -> fallback)",
                     preferred_idx, tok)
    except Exception as exc:
        log.warning("spore-stream: post-build probe failed for %s: %s", tok, exc)
    finally:
        _spore_probing.discard(tok)


def _prepare_stream(token: str) -> dict:
    """Everything /spore-stream decides BEFORE any byte is served, shared by
    the Flask route below and by /internal/stream-resolve, which the Go
    streaming front calls so it can do the byte-shoveling itself. All side
    effects live here too: materialize, background .fsh builds and probes,
    the CDN liveness check for the MKV redirect.

    Returns one of:
      {"error": 404 | 502 | 503, "reason": str}   503 = CDN rate limited
      {"mode": "redirect", "url": cdn_url}                        MKV/other
      {"mode": "cold", "cdn_url": ..., "size": n}                 passthrough
      {"mode": "warm", "cdn_url": ..., "cdn_size": n,
       "fsh_path": ..., "info": <mp4_faststart.load dict>}        moov-first
    """
    import mp4_faststart

    url = catbox.materialize(token)
    if not url:
        return {"error": 404, "reason": "materialize failed"}

    info = mp4_faststart.load(token)
    cdn_url = url

    if info is None:
        return _prepare_cold(token, cdn_url)

    # CDN file is already moov-first (or MKV redirect sentinel).
    # MKV files (ftyp_size == 0): redirect to CDN - FFmpeg reads MKV from byte 0,
    #   no seeking needed, and CDN redirect avoids unnecessary proxy bandwidth.
    # Already fast-start MP4 (ftyp_size > 0): proxy bytes through our server so
    #   Plex Server cannot cache the raw CDN URL. Plex stores our /spore-stream/
    #   URL instead; when any client (MiTV, Shield, etc.) plays, they always hit
    #   our server which resolves a fresh CDN URL - expired URLs never reach clients.
    if info.get("already_fast"):
        return _prepare_fast(token, cdn_url, info)

    return _prepare_warm(token, cdn_url, info)


def _prepare_cold(token: str, cdn_url: str) -> dict:
    """No .fsh cache yet: HEAD the CDN for a size, start the background
    build, and let the caller serve Range passthrough meanwhile. Returns
    the cold mode dict, or an error dict when the HEAD is not usable."""
    import time as _t
    import mp4_faststart

    # Cold cache: build .fsh in background, serve Range passthrough
    # meanwhile. _spore_cold_sizes caches file_size so repeated Range
    # requests (FFmpeg seeks) skip the HEAD round-trip, and a cached size is
    # how we know an earlier request already started the build. There is no
    # .fsh here by construction: _prepare_stream only calls this when
    # mp4_faststart.load() came back None.
    if stream_decisions.should_start_build(False, token in _spore_cold_sizes):
        threading.Thread(
            target=_build_then_probe,
            args=(cdn_url, token),
            daemon=True,
            name=f"fsh-{token[:8]}",
        ).start()
        import requests as _req
        status = None
        size = 0
        try:
            for attempt in (1, 2):
                head = _req.head(cdn_url, timeout=10, allow_redirects=True)
                status = head.status_code
                if status == 429 and attempt == 1:
                    # Transient CDN rate limit: one short backoff, then
                    # give up honestly rather than serve garbage.
                    _t.sleep(0.5)
                    continue
                if status < 400:
                    size = int(head.headers.get("Content-Length", 0) or 0)
                break
        except Exception as exc:
            log.warning("spore-stream: HEAD failed for cold token=%s: %s", token, exc)
            return {"error": 502, "reason": "HEAD failed"}
        if not size:
            # An error response's Content-Length is the size of its error
            # page, not of the file. Caching it here once served clients a
            # 162-byte "movie" with a 206 status (found by the stream load
            # test under a CDN 429 storm). Do not cache failures: the next
            # request retries the HEAD.
            log.warning("spore-stream: cold HEAD status=%s size=%s for token=%s",
                        status, size, token)
            return {"error": 503 if status == 429 else 502,
                    "reason": f"HEAD status {status}"}
        _spore_cold_sizes[token] = size
    size = _spore_cold_sizes.get(token, 0)
    if not size:
        return {"error": 502, "reason": "no file size"}
    # Remove the cold size once .fsh is ready so the next request warms up
    if mp4_faststart.load(token) is not None:
        _spore_cold_sizes.pop(token, None)
    return {"mode": "cold", "cdn_url": cdn_url, "size": size}


def _head_status(url: str) -> int:
    """The CDN liveness probe stream_decisions.link_is_alive calls."""
    import requests as _req

    return _req.head(url, timeout=5, allow_redirects=True).status_code


def _prepare_fast(token: str, cdn_url: str, info: dict) -> dict:
    """The CDN file needs no moov rewrite: redirect an MKV (after checking
    the cached link is still alive) or proxy an already fast-start MP4, and
    trigger the background probe the stub update needs."""
    import time as _t

    existing = db.load_spore_tracks(token)
    if (not existing or "preferred_audio_idx" not in existing) and token not in _spore_probing:
        _spore_probing.add(token)
        threading.Thread(
            target=_build_then_probe,
            args=(cdn_url, token),
            daemon=True,
            name=f"probe-{token[:8]}",
        ).start()
        log.info("spore-stream: token=%s triggering background probe", token)
    if info["ftyp_size"] == 0:
        # Non-MP4 sentinel (MKV/other): 302 to CDN, no moov seeking required.
        # catbox's URL cache holds a resolved link for up to 23h, but TorBox's
        # CDN links can go dead sooner than that. Sending a stale one straight
        # to the client (rather than proxying through mp4_faststart, which does
        # validate) left Jellyfin/ffmpeg following a redirect into a 400 error
        # page with no recovery. Cheaply confirm it's alive first and re-resolve
        # once if not. A short local cache avoids re-checking with the CDN on
        # every reopen/seek within the same playback session, and a stale
        # entry is refreshed behind the request rather than on it.
        if not stream_decisions.link_is_alive(
                cdn_url, _head_status, _spore_alive_cache, _t.monotonic(),
                _ALIVE_CHECK_TTL_SEC, refresh=_refresh_alive_async,
                grace=_ALIVE_STALE_GRACE_SEC):
            log.warning("spore-stream: cached CDN url dead for token=%s, re-resolving", token)
            catbox.invalidate_url_cache(token)
            _spore_alive_cache.pop(cdn_url, None)
            fresh = catbox.materialize(token)
            if not fresh:
                return {"error": 502, "reason": "re-resolve failed"}
            cdn_url = fresh
        _spore_cold_sizes.pop(token, None)
        # No bytes pass through us on this branch, so count the file once
        # per play as an estimate (see egress_estimate.py).
        egress_estimate.note_redirect(token, info["cdn_size"])
        return {"mode": "redirect", "url": cdn_url}
    # Already fast-start MP4: proxy bytes; Plex stores our URL not the CDN URL.
    log.info("spore-stream: token=%s already fast-start MP4, proxying bytes", token)
    _spore_cold_sizes[token] = info["cdn_size"]
    return {"mode": "cold", "cdn_url": cdn_url, "size": info["cdn_size"]}


def _prepare_warm(token: str, cdn_url: str, info: dict) -> dict:
    """The .fsh cache is built: the caller serves moov-first bytes."""
    import mp4_faststart

    return {"mode": "warm", "cdn_url": cdn_url, "cdn_size": info["cdn_size"],
            "fsh_path": str(mp4_faststart._cache_path(token)), "info": info}


def _cold_proxy_response(file_size: int, cdn_url: str, token: str, ua: str):
    """Range-passthrough straight to the CDN. Used both while the .fsh
    cache is still building and for the already-fast-start case, where we
    still proxy (rather than 302) so Plex never caches the raw CDN URL."""
    import mp4_faststart

    if not file_size:
        abort(502)

    range_hdr = request.headers.get("Range")
    if range_hdr:
        try:
            r_start, r_end = _parse_byte_range(range_hdr, file_size)
        except Exception:
            abort(416)
        r_end  = min(r_end, file_size - 1)
        status = 206
    else:
        r_start, r_end, status = 0, file_size - 1, 200

    length = r_end - r_start + 1

    def _gen_passthrough():
        CHUNK = 2 << 20
        pos = r_start
        while pos <= r_end:
            end = min(pos + CHUNK - 1, r_end)
            try:
                # Route through mp4_faststart's _get() instead of a raw
                # requests.get(): it already validates the CDN status
                # code (retries on 429, raises on anything else) before
                # returning bytes. A bare requests.get() here previously
                # had no status check at all, so a CDN error body would
                # get piped to the client as if it were video data.
                # This runs inline on a live gunicorn request thread
                # (small, fixed pool, see Dockerfile), so it gets the
                # same reduced retry budget as mp4_faststart.serve_bytes.
                data = mp4_faststart._get(
                    cdn_url, pos, end,
                    max_retries=mp4_faststart._LIVE_REQUEST_MAX_429_RETRIES,
                )
                yield data
                pos = end + 1
            except Exception as exc:
                log.warning("spore-stream cold proxy: error pos=%d token=%s: %s",
                            pos, token, exc)
                break

    resp = Response(
        stream_with_context(_gen_passthrough()),
        status=status,
        mimetype="video/mp4",
        direct_passthrough=True,
    )
    resp.headers["Accept-Ranges"]  = "bytes"
    resp.headers["Content-Length"] = str(length)
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {r_start}-{r_end}/{file_size}"
    log.info("spore-stream: token=%s cold-proxy bytes=%d-%d/%d ua=%r",
             token, r_start, r_end, file_size, ua)
    return resp


@bp.get("/spore-stream/<token>")
def spore_stream_proxy(token: str):
    """Serves moov-first MP4 with Range support (all clients).

    In the default deployment the Go streaming front (spore-stream/) owns
    this path and only calls /internal/stream-resolve below; this Flask
    route is the complete fallback for STREAM_FRONT_ENABLED=false.

    Cold cache: pass-through Range proxy to CDN while building .fsh in background.
    Warm cache: serve virtual moov-first layout so FFmpeg never seeks 15GB.
    """
    import time as _t
    import mp4_faststart

    started = _t.monotonic()
    ua  = request.headers.get("User-Agent", "?")[:80]
    rng = request.headers.get("Range", "-")

    res = _prepare_stream(token)
    if "error" in res:
        if res["error"] == 404:
            log.warning("spore-stream: materialize FAILED token=%s ua=%r range=%s (%.1fs)",
                        token, ua, rng, _t.monotonic() - started)
        abort(res["error"])

    if res["mode"] == "redirect":
        log.info("spore-stream: token=%s non-MP4 sentinel, 302 to CDN", token)
        return redirect(res["url"], code=302)

    cdn_url = res["cdn_url"]
    if res["mode"] == "cold":
        return _cold_proxy_response(res["size"], cdn_url, token, ua)

    info = res["info"]
    file_size = info["cdn_size"]
    range_hdr = request.headers.get("Range")

    if range_hdr:
        try:
            v_start, v_end = _parse_byte_range(range_hdr, file_size)
        except Exception:
            abort(416)
        v_end  = min(v_end, file_size - 1)
        status = 206
    else:
        v_start, v_end, status = 0, file_size - 1, 200

    length = v_end - v_start + 1

    def _generate():
        CHUNK = 2 << 20
        pos = v_start
        while pos <= v_end:
            end = min(pos + CHUNK - 1, v_end)
            try:
                data = mp4_faststart.serve_bytes(info, cdn_url, pos, end)
            except Exception as exc:
                log.warning("spore-stream proxy: error v=%d token=%s: %s", pos, token, exc)
                break
            if not data:
                break
            yield data
            pos += len(data)

    resp = Response(
        stream_with_context(_generate()),
        status=status,
        mimetype="video/mp4",
        direct_passthrough=True,
    )
    resp.headers["Accept-Ranges"]  = "bytes"
    resp.headers["Content-Length"] = str(length)
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {v_start}-{v_end}/{file_size}"
    log.info("spore-stream: token=%s bytes=%d-%d/%d (%.1fs) ua=%r",
             token, v_start, v_end, file_size, _t.monotonic() - started, ua)
    return resp


@bp.get("/internal/stream-resolve/<token>")
def internal_stream_resolve(token: str):
    """Decision endpoint for the Go streaming front (same container). The
    front owns the byte transfer; Python keeps every decision: materialize,
    the TorBox budget, liveness checks, background .fsh builds.

    Loopback-only: the front talks to gunicorn over 127.0.0.1, and when the
    front is disabled gunicorn is exposed directly, where this must not be
    reachable (the CDN URLs it returns are unauthenticated capability links).
    The Go front additionally refuses to proxy /internal/* at all."""
    if not _from_loopback():
        abort(403)
    res = _prepare_stream(token)
    if "error" in res:
        return jsonify(error=res.get("reason", "")), res["error"]
    res.pop("info", None)  # bytes payload; the front reads the .fsh itself
    return jsonify(res)


@bp.post("/internal/stream-report/<token>")
# Machine caller over loopback, like the webhooks: no CSRF token to present.
@_csrf.exempt
def internal_stream_report(token: str):
    """Byte count for one finished stream, reported by the Go front.

    Loopback-only for the same reason as the resolve endpoint: the front
    talks to gunicorn over 127.0.0.1, and when the front is disabled
    gunicorn is exposed directly, where this must not be reachable."""
    if not _from_loopback():
        abort(403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # Covers a falsy non-dict too (null, 0, [], ""), which `or {}` would
        # have coerced into a silent zero-byte report.
        return jsonify(error="body must be a JSON object"), 400
    try:
        db.record_egress(token, int(payload.get("bytes", 0)))
    except (TypeError, ValueError):
        return jsonify(error="bytes must be an integer"), 400
    return jsonify(ok=True)


@bp.get("/ui/api/virtual-items")
def ui_api_virtual_items():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    items = db.get_all_virtual_items()
    return jsonify(items=[{
        "id": i["id"], "token": i["token"], "title": i["title"], "media_type": i["media_type"],
        "torbox_id": i["torbox_id"], "in_torbox": bool(i["torbox_id"]),
        "play_count": i["play_count"], "last_played": i["last_played"],
        "created_at": i["created_at"], "info_hash": i["info_hash"],
    } for i in items])


@bp.post("/ui/api/virtual-items/<token>/re-resolve")
def ui_api_re_resolve(token: str):
    """Clear fail state for a token and trigger a fresh materialize attempt."""
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    item = db.get_virtual_item(token)
    if not item:
        return jsonify(error="unknown token"), 404
    # Clear in-memory caches
    catbox.invalidate_url_cache(token)
    with catbox._fail_cache_lock:
        catbox._fail_cache.pop(token, None)
    # Reset persistent state
    import catbox as _catbox
    ckey = _catbox._content_key(item)
    if ckey:
        db.reset_playability_state(ckey)
    # Attempt fresh materialize in background, return immediately
    result: dict = {}
    import threading as _threading
    def _try():
        url = catbox.materialize(token, allow_readd=True)
        result["url"] = url
    t = _threading.Thread(target=_try, daemon=True)
    t.start()
    t.join(timeout=50)
    if result.get("url"):
        return jsonify(ok=True, resolved=True, title=item["title"])
    return jsonify(ok=True, resolved=False, title=item["title"],
                   hint="check logs  -  re-resolve attempted but no URL returned")


@bp.get("/ui/api/playability-state")
def ui_api_playability_state():
    """Return degraded items with 3+ consecutive failures."""
    items = db.get_degraded_items(min_failures=3)
    return jsonify(items=items)


@bp.get("/ui/api/integrity")
def ui_api_integrity():
    """Read-only data-integrity scan: surfaces empty/malformed imdb_id,
    missing hashes, duplicate content and orphan playability rows."""
    return jsonify(db.integrity_report())


@bp.get("/ui/api/zilean/status")
def ui_api_zilean_status():
    """Status of the native Zilean index (mode, hash count, last sync/import)."""
    mode = _settings_mod.get("ZILEAN_MODE", cfg.ZILEAN_MODE)
    status = {"mode": mode}
    if mode == "native":
        import zilean_index
        status.update(zilean_index.get_status())
    return jsonify(status)


@bp.post("/ui/api/zilean/sync")
def ui_api_zilean_sync():
    """Trigger an immediate native Zilean hashlist sync in the background."""
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    import threading as _threading
    import zilean_index
    force = bool(request.get_json(silent=True) and request.get_json(silent=True).get("force"))
    _threading.Thread(target=zilean_index.sync, kwargs={"force": force}, daemon=True).start()
    return jsonify(ok=True, started=True)


@bp.post("/ui/api/zilean/import")
def ui_api_zilean_import():
    """One-time bulk import from an existing external Zilean's Postgres database
    into the native index. Connection settings come from the Zilean native
    settings group (Postgres host/port/db/user/password)."""
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    host = _settings_mod.get("ZILEAN_PG_HOST", cfg.ZILEAN_PG_HOST)
    if not host:
        return jsonify(error="ZILEAN_PG_HOST not configured"), 400
    import threading as _threading
    import zilean_index
    kwargs = dict(
        host=host,
        port=_settings_mod.get("ZILEAN_PG_PORT", cfg.ZILEAN_PG_PORT),
        dbname=_settings_mod.get("ZILEAN_PG_DB", cfg.ZILEAN_PG_DB),
        user=_settings_mod.get("ZILEAN_PG_USER", cfg.ZILEAN_PG_USER),
        password=_settings_mod.get("ZILEAN_PG_PASSWORD", cfg.ZILEAN_PG_PASSWORD),
    )
    _threading.Thread(target=zilean_index.import_from_postgres, kwargs=kwargs, daemon=True).start()
    return jsonify(ok=True, started=True)


@bp.get("/ui/api/blacklist")
def ui_api_blacklist():
    items = db.get_all_failed_hashes()
    titles = db.titles_for_hashes([item["info_hash"] for item in items])
    for item in items:
        item["titles"] = titles.get(item["info_hash"], [])
    return jsonify(items=items)


@bp.post("/ui/blacklist-clear/<info_hash>")
def ui_blacklist_clear(info_hash: str):
    if not auth.is_admin():
        abort(403)
    db.clear_failed_hash(info_hash)
    return redirect(url_for("admin_library.ui_dashboard") + "#blacklist")


@bp.get("/ui/api/backups")
def ui_api_backups():
    return jsonify(backups=backup.list_backups())


@bp.post("/ui/backup-restore")
def ui_backup_restore():
    """Restore a named backup over the live database.

    Answers 400 on failure rather than redirecting either way: a restore that
    silently reports success is worse than none, because the operator walks
    away believing their data is back. The caller still has to restart
    Mycelium, since db.py keeps one SQLite handle per thread for the life of
    the process and those handles still point at the replaced file."""
    if not auth.is_admin():
        abort(403)
    name = request.form.get("name", "").strip()
    if not backup.restore(name):
        log.error("Restore failed for %r; the live database is unchanged", name)
        return jsonify(error="restore failed, see the logs. The live database "
                             "is unchanged"), 400
    return jsonify(ok=True, restored=name,
                   message="Restored. Restart Mycelium for it to take effect.")


# ── WebDAV (opt-in, mount via davfs2 from DSM host) ───────────────────────────

import webdav

_WEBDAV_METHODS = ["OPTIONS", "GET", "HEAD", "PROPFIND"]


@bp.route(
    f"{cfg.WEBDAV_PATH_PREFIX}/",
    defaults={"path_suffix": ""},
    methods=_WEBDAV_METHODS,
)
@bp.route(
    f"{cfg.WEBDAV_PATH_PREFIX}/<path:path_suffix>",
    methods=_WEBDAV_METHODS,
)
def webdav_handler(path_suffix: str):
    return webdav.dispatch(path_suffix)
