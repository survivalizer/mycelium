"""The decisions the play path makes before a byte is served: whether
/stream/<token> may send the client straight to the CDN, whether a cached
CDN link is still worth handing out, and whether a .fsh build gets started.

They live here rather than in routes/stream.py because that module imports
the Flask blueprint (and appcore with it), which no test may import. Nothing
here touches the network, Flask or the clock: the caller passes in the probe,
the cache and the current time, so every decision is testable at runtime.
"""

FRESH = "fresh"      # confirmed alive inside the TTL
STALE = "stale"      # past the TTL but inside the grace: hand out, refresh behind
EXPIRED = "expired"  # past the grace too, or never confirmed: probe inline


def cached_link_state(cdn_url: str, cache: dict, now: float, grace: float) -> str:
    """How much the liveness cache still vouches for `cdn_url`. The cache
    maps a url to the moment its confirmation expires; `grace` is how long
    past that moment the old answer is still usable while a background
    probe refreshes it."""
    cached_until = cache.get(cdn_url)
    if not cached_until:
        return EXPIRED
    if cached_until > now:
        return FRESH
    if cached_until + grace > now:
        return STALE
    return EXPIRED


def link_is_alive(cdn_url: str, head_status, cache: dict, now: float,
                  ttl: float, refresh=None, grace: float = 0.0) -> bool:
    """Is the cached CDN link still worth handing to a client?

    A cache hit inside the TTL is trusted. A hit inside the grace past the
    TTL is trusted too when the caller passes `refresh(url)`: the stale
    answer is handed out and the refresh runs off the request thread, so no
    play pays the round trip. Otherwise probe once through
    `head_status(url) -> int`. A probe that raises counts as dead. Only a
    live answer refreshes the cache, so a dead link is probed again next
    time instead of being remembered as dead.
    """
    state = cached_link_state(cdn_url, cache, now, grace)
    if state == FRESH:
        return True
    if state == STALE and refresh is not None:
        refresh(cdn_url)
        return True
    return refresh_link(cdn_url, head_status, cache, now, ttl)


def refresh_link(cdn_url: str, head_status, cache: dict, now: float,
                 ttl: float) -> bool:
    """Probe `cdn_url` once and bring the cache in line: a live link is
    confirmed for another `ttl`, a dead one is forgotten so the next request
    probes inline and re-resolves."""
    try:
        alive = head_status(cdn_url) < 400
    except Exception:
        alive = False
    if alive:
        cache[cdn_url] = now + ttl
    else:
        cache.pop(cdn_url, None)
    return alive


def warm_link_state(info: dict | None, cdn_url: str | None, cache: dict,
                    now: float, grace: float) -> str | None:
    """May /stream/<token> skip /spore-stream and redirect to the CDN itself?

    Returns FRESH or STALE when it may (the caller starts a background
    refresh for STALE), or None when /spore-stream has to decide: no cached
    address, no fast-start record yet, a file served by the moov-first
    proxy rather than by redirect (an MP4), or a liveness entry that is
    expired or missing. Nothing here resolves or probes, so the first hop
    never touches TorBox or the CDN.
    """
    if not cdn_url or not info:
        return None
    if not info.get("already_fast") or info.get("ftyp_size") != 0:
        return None
    state = cached_link_state(cdn_url, cache, now, grace)
    return state if state in (FRESH, STALE) else None


def should_start_build(fsh_exists: bool, building_now: bool) -> bool:
    """Start the background .fsh build only when there is no cache yet and no
    earlier request already started one for this token."""
    return not fsh_exists and not building_now
