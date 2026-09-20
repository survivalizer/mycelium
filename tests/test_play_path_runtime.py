"""Runtime cover for the play-path decisions the source-text tests cannot
reach: which arm of catbox.materialize answers (RealDebrid versus the TorBox
ladder), whether a cached CDN link is still worth handing out, and whether a
background .fsh build gets started.

routes/stream.py imports the Flask blueprint, so it is never imported here:
its two decisions live in stream_decisions.py and are exercised directly,
with a source-text check that the route still calls them."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import catbox  # noqa: E402
import realdebrid  # noqa: E402
import stream_decisions as sd  # noqa: E402
import torbox_pool as pool  # noqa: E402
from _helpers import _src  # noqa: E402

# catbox.py may already be cached with its own `torbox`/`db` references bound;
# alias to those exact objects so monkeypatching reaches the calls it makes.
torbox = catbox.torbox
db = catbox.db

H = "a" * 40
NEW_H = "b" * 40


def _drop_cached_conn():
    for m in (db, catbox.db, pool.db):
        conn = getattr(m._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            m._tls.conn = None


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    _drop_cached_conn()
    for m in (db, catbox.db, pool.db):
        monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TORBOX_API_KEY", "k1")
    _drop_cached_conn()
    db.init()
    pool.invalidate()
    pool._health.clear()
    catbox.invalidate_url_cache()
    with catbox._fail_cache_lock:
        catbox._fail_cache.clear()
    catbox._touch_cache.clear()
    catbox._recent_tokens.clear()
    monkeypatch.setattr(catbox._settings, "get", lambda k, d=None: {"CATBOX_MODE": True}.get(k, d))
    yield
    _drop_cached_conn()


def _rd_item(token, rd_id=None, info_hash=H):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO virtual_items (token, info_hash, magnet, title, media_type, "
            "file_id, imdb_id, debrid_provider, rd_id) "
            "VALUES (?, ?, ?, 'Heat', 'movie', 0, 'tt0113277', 'realdebrid', ?)",
            (token, info_hash, f"magnet:?xt=urn:btih:{info_hash}", rd_id))
        conn.commit()
    return db.get_virtual_item(token)


@pytest.fixture
def torbox_tripwire(monkeypatch):
    """Every TorBox door the ladder can open, wired to explode."""
    opened = []

    def boom(name):
        def _f(*a, **k):
            opened.append(name)
            raise AssertionError(f"the TorBox ladder was entered: {name}")
        return _f

    for name in ("find_by_id", "find_by_hash", "list_torrents", "add_magnet",
                 "wait_until_ready", "request_download_link"):
        monkeypatch.setattr(torbox, name, boom(name))
    monkeypatch.setattr(pool, "choose_for_add", boom("choose_for_add"))
    return opened


# 1. which arm answers

def test_a_live_rd_item_plays_from_realdebrid_and_never_enters_the_torbox_ladder(
        monkeypatch, torbox_tripwire):
    """The RealDebrid arm's fast path is the answer, not a fall-through: a
    _Stop(url) that leaked as a bare None would drop through to TorBox."""
    _rd_item("t-rd", rd_id="rd1")
    monkeypatch.setattr(realdebrid, "get_info", lambda rd_id: {"status": "downloaded"})
    monkeypatch.setattr(realdebrid, "get_main_video_url", lambda rd_id: f"https://rd/{rd_id}")

    assert catbox.materialize("t-rd", allow_readd=True) == "https://rd/rd1"
    assert torbox_tripwire == []
    ckey = catbox._content_key(db.get_virtual_item("t-rd"))
    state = db.get_playability_state(ckey)
    assert state and state["status"] == "playable"
    assert state["last_ok_provider"] == "realdebrid", "the RD arm recorded the play"


def test_an_rd_item_whose_url_is_gone_fails_without_entering_the_torbox_ladder(
        monkeypatch, torbox_tripwire):
    """_Stop(None) is an answer too: no URL from RD and no cached release
    must not fall through to TorBox."""
    _rd_item("t-rd2", rd_id="rd2")
    monkeypatch.setattr(realdebrid, "get_info", lambda rd_id: {"status": "dead"})
    monkeypatch.setattr(catbox, "_search_cached_release", lambda item: None)

    assert catbox.materialize("t-rd2", allow_readd=True) is None
    assert torbox_tripwire == []


def test_an_rd_item_whose_search_finds_a_torbox_release_falls_through_to_the_ladder(
        monkeypatch):
    """The other half of the contract: a bare None from the RealDebrid arm
    means carry on, and the ladder must pick the item up with the hash the
    search wrote into it."""
    _rd_item("t-fall")
    monkeypatch.setattr(realdebrid, "get_info",
                        lambda rd_id: pytest.fail("RD must not be polled without an rd_id"))
    monkeypatch.setattr(catbox, "_search_cached_release",
                        lambda item: (NEW_H, f"magnet:?xt=urn:btih:{NEW_H}", "torbox"))

    calls = []
    ready = {"id": 5, "hash": NEW_H, "download_finished": True,
             "files": [{"id": 0, "name": "Heat.mkv", "size": 10}]}
    monkeypatch.setattr(torbox, "find_by_hash", lambda acct, h, **k: calls.append(("find_by_hash", acct)))
    monkeypatch.setattr(torbox, "find_by_id", lambda acct, tid, **k: (calls.append(("find_by_id", acct, tid)), ready)[1])
    monkeypatch.setattr(torbox, "add_magnet", lambda acct, magnet, **k: (calls.append(("add", acct)), ready)[1])
    monkeypatch.setattr(torbox, "_is_ready", lambda t: bool(t and t.get("download_finished")))
    monkeypatch.setattr(torbox, "request_download_link",
                        lambda acct, tid, fid, **k: (calls.append(("link", acct, tid, fid)), f"https://cdn/{acct}/{tid}")[1])
    monkeypatch.setattr(pool, "choose_for_add", lambda: (calls.append(("choose_for_add",)), pool.account(1))[1])

    assert catbox.materialize("t-fall", allow_readd=True) == "https://cdn/1/5"
    assert ("choose_for_add",) in calls, "the TorBox ladder was never reached"
    assert ("link", 1, 5, 0) in calls
    assert db.get_virtual_item("t-fall")["info_hash"] == NEW_H


# 2. is the cached CDN link still good?

def test_a_live_probe_answers_alive_and_is_remembered():
    cache = {}
    assert sd.link_is_alive("u", lambda u: 200, cache, now=100.0, ttl=30.0) is True
    assert cache == {"u": 130.0}


def test_a_dead_probe_answers_dead_and_is_not_remembered():
    """Caching a dead answer would keep re-resolving a link that came back."""
    cache = {}
    assert sd.link_is_alive("u", lambda u: 400, cache, now=100.0, ttl=30.0) is False
    assert cache == {}


def test_a_probe_that_raises_answers_dead():
    def boom(url):
        raise RuntimeError("connection reset")

    assert sd.link_is_alive("u", boom, {}, now=100.0, ttl=30.0) is False


def test_a_fresh_cache_entry_skips_the_probe_and_a_stale_one_does_not():
    probed = []

    def probe(url):
        probed.append(url)
        return 200

    cache = {"u": 130.0}
    assert sd.link_is_alive("u", probe, cache, now=100.0, ttl=30.0) is True
    assert probed == [], "a fresh entry must not cost a round-trip"
    assert sd.link_is_alive("u", probe, cache, now=131.0, ttl=30.0) is True
    assert probed == ["u"], "an expired entry must be re-probed"


def test_the_redirect_branch_still_asks_before_it_hands_out_a_cached_url():
    """Runtime cover for link_is_alive is worthless if the route stops
    calling it: pin the call and the re-resolve it guards."""
    body = _prepare_body("_prepare_fast")
    # The answer must BE the branch: calling it and then overriding the result
    # (alive = True) reads as a check and is not one.
    assert "if not stream_decisions.link_is_alive(" in body, \
        "the liveness answer does not decide the branch"
    assert "alive = " not in body, "the liveness answer is rebound before the branch"
    assert "_head_status" in body, "nothing probes the CDN"
    assert "catbox.invalidate_url_cache(token)" in body
    assert "fresh = catbox.materialize(token)" in body, "a dead link is not re-resolved"


def test_a_stale_entry_is_handed_out_and_refreshed_behind_the_request():
    """Inside the grace past the TTL, no play pays the round trip: the old
    answer is used and the refresh runs off the request thread."""
    probed, refreshed = [], []
    cache = {"u": 130.0}
    assert sd.link_is_alive("u", lambda u: probed.append(u) or 200, cache,
                            now=131.0, ttl=30.0, refresh=refreshed.append,
                            grace=480.0) is True
    assert probed == [], "a stale entry was probed on the request thread"
    assert refreshed == ["u"], "the stale entry was not handed to the refresher"
    assert cache == {"u": 130.0}, "only the refresher may move the expiry"


def test_a_stale_entry_without_a_refresher_is_probed_inline():
    probed = []
    cache = {"u": 130.0}
    assert sd.link_is_alive("u", lambda u: probed.append(u) or 200, cache,
                            now=131.0, ttl=30.0, grace=480.0) is True
    assert probed == ["u"]
    assert cache == {"u": 161.0}


def test_an_entry_past_the_grace_is_probed_inline_even_with_a_refresher():
    probed, refreshed = [], []
    cache = {"u": 130.0}
    assert sd.link_is_alive("u", lambda u: probed.append(u) or 200, cache,
                            now=611.0, ttl=30.0, refresh=refreshed.append,
                            grace=480.0) is True
    assert refreshed == [], "an expired entry must not be handed out unprobed"
    assert probed == ["u"]


def test_cached_link_state_names_the_three_windows():
    cache = {"u": 130.0}
    assert sd.cached_link_state("u", cache, now=100.0, grace=480.0) == sd.FRESH
    assert sd.cached_link_state("u", cache, now=130.0, grace=480.0) == sd.STALE
    assert sd.cached_link_state("u", cache, now=609.9, grace=480.0) == sd.STALE
    assert sd.cached_link_state("u", cache, now=610.0, grace=480.0) == sd.EXPIRED
    assert sd.cached_link_state("v", cache, now=100.0, grace=480.0) == sd.EXPIRED


def test_the_refresher_confirms_a_live_link_and_forgets_a_dead_one():
    cache = {"u": 130.0}
    assert sd.refresh_link("u", lambda u: 200, cache, now=140.0, ttl=30.0) is True
    assert cache == {"u": 170.0}
    assert sd.refresh_link("u", lambda u: 404, cache, now=150.0, ttl=30.0) is False
    assert cache == {}, "a dead link left in the cache would be handed out again"

    def boom(url):
        raise RuntimeError("connection reset")

    cache = {"u": 130.0}
    assert sd.refresh_link("u", boom, cache, now=150.0, ttl=30.0) is False
    assert cache == {}


def test_the_stale_refresh_is_started_once_per_url_off_the_request_thread():
    """_refresh_alive_async lives beside the route, so pin its shape: a
    single-flight set, a daemon thread, and refresh_link doing the work."""
    body = _prepare_body("_refresh_alive_async")
    assert "if cdn_url in _spore_alive_refreshing:" in body and "return" in body, \
        "two requests inside the grace would start two probes"
    assert "_spore_alive_refreshing.add(cdn_url)" in body
    assert "_spore_alive_refreshing.discard(cdn_url)" in body, \
        "a finished probe never lets the next one run"
    assert "stream_decisions.refresh_link(" in body and "_head_status" in body
    assert "threading.Thread(" in body and "daemon=True" in body and ".start()" in body
    fast = _prepare_body("_prepare_fast")
    assert "refresh=_refresh_alive_async" in fast and "grace=_ALIVE_STALE_GRACE_SEC" in fast, \
        "the redirect branch no longer refreshes stale entries behind the request"


# 2b. may /stream/<token> skip /spore-stream?

_MKV = {"already_fast": True, "ftyp_size": 0, "cdn_size": 10}
_MP4 = {"already_fast": True, "ftyp_size": 24, "cdn_size": 10}
_SLOW_MP4 = {"already_fast": False, "ftyp_size": 24, "cdn_size": 10}


def test_the_first_hop_redirects_only_a_redirect_sentinel_with_a_vouched_link():
    cache = {"u": 130.0}
    assert sd.warm_link_state(_MKV, "u", cache, now=100.0, grace=480.0) == sd.FRESH
    assert sd.warm_link_state(_MKV, "u", cache, now=200.0, grace=480.0) == sd.STALE
    assert sd.warm_link_state(_MKV, "u", cache, now=700.0, grace=480.0) is None
    assert sd.warm_link_state(_MKV, "v", cache, now=100.0, grace=480.0) is None, \
        "a link that was never confirmed alive must go through spore-stream"


def test_the_first_hop_leaves_proxied_and_unknown_files_to_spore_stream():
    cache = {"u": 130.0}
    assert sd.warm_link_state(_MP4, "u", cache, now=100.0, grace=480.0) is None, \
        "an already fast-start MP4 is proxied so the CDN url is never stored by Plex"
    assert sd.warm_link_state(_SLOW_MP4, "u", cache, now=100.0, grace=480.0) is None
    assert sd.warm_link_state(None, "u", cache, now=100.0, grace=480.0) is None, \
        "the first play has no .fsh record yet"
    assert sd.warm_link_state(_MKV, None, cache, now=100.0, grace=480.0) is None, \
        "no cached address means a resolve, which is not this hop's job"


def test_cached_url_answers_from_the_cache_alone_and_counts_the_play(monkeypatch, torbox_tripwire):
    """The first hop must never resolve: a miss is None with no TorBox door
    opened, a hit is the address plus the idle-release touch."""
    _rd_item("t-warm")
    touched = []
    monkeypatch.setattr(catbox, "_touch_debounced", touched.append)
    monkeypatch.setattr(catbox, "_is_scan_burst", lambda t: (_ for _ in ()).throw(AssertionError("resolving")))
    assert catbox.cached_url("t-warm") is None
    assert touched == []
    catbox.cache_url("t-warm", "https://cdn/1/5")
    assert catbox.cached_url("t-warm") == "https://cdn/1/5"
    assert touched == ["t-warm"], "a play answered by the first hop must count for idle release"


def test_the_first_hop_reads_only_what_it_already_knows():
    """/stream/<token> answers with the CDN url only through the pure
    decision, from the cache seams, and books the estimated egress; the
    fallthrough to /spore-stream stays."""
    body = _prepare_body("_warm_redirect_url")
    assert "catbox.cached_url(token)" in body, "the first hop resolves instead of peeking"
    assert "catbox.materialize" not in body and "_head_status" not in body, \
        "the first hop must never resolve or probe inline"
    assert "mp4_faststart.load_meta(token)" in body, \
        "the first hop must not read the whole .fsh (up to the moov) on every request"
    assert "stream_decisions.warm_link_state(" in body
    assert "_refresh_alive_async(cdn_url)" in body, "a stale entry is handed out without a refresh"
    assert "egress_estimate.note_redirect(token, info[\"cdn_size\"])" in body, \
        "a play answered here would vanish from the egress estimate"
    route = _prepare_body("stream_redirect")
    assert "cdn_url = _warm_redirect_url(token)" in route
    assert "return redirect(cdn_url, code=302)" in route
    assert 'return redirect(f"/spore-stream/{token}", code=302)' in route, \
        "the fallthrough to spore-stream is gone"


# 3. does the background build get started?

def test_the_build_starts_only_when_there_is_no_cache_and_no_build_running():
    assert sd.should_start_build(fsh_exists=False, building_now=False) is True
    assert sd.should_start_build(fsh_exists=False, building_now=True) is False
    assert sd.should_start_build(fsh_exists=True, building_now=False) is False
    assert sd.should_start_build(fsh_exists=True, building_now=True) is False


def test_the_cold_branch_still_starts_the_background_build_behind_that_gate():
    body = _prepare_body("_prepare_cold")
    assert "if stream_decisions.should_start_build(" in body, \
        "the build decision does not decide the branch"
    assert "if False" not in body, "the gate is dead code with the call kept beside it"
    gated = body.split("stream_decisions.should_start_build(", 1)[1]
    assert "threading.Thread(" in gated and "target=_build_then_probe" in gated, \
        "the cold path no longer starts the .fsh build"
    assert ".start()" in gated


def _prepare_body(name):
    src = _src("routes/stream.py")
    m = re.search(rf"def {name}\(.*?\n(.*?)\n\n\ndef ", src, re.S)
    assert m, f"{name} not found in routes/stream.py"
    return m.group(1)
