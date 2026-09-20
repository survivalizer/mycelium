"""The setup wizard and the one-off maintenance actions the admin UI
triggers by hand (rescans, repairs, backfills)."""
import logging
import threading

from flask import Blueprint, abort, jsonify, redirect, request, url_for

import auth
import cleanup
import db
import jellyfin
import log_buffer
import monitor
import nfo_generator
import strm_generator
from appcore import limiter
from routes._common import _needs_first_admin, _spa_index

log = logging.getLogger("mycelium")

bp = Blueprint("setup", __name__)


# ── Setup wizard ──────────────────────────────────────────────────────────────

@bp.get("/setup")
def setup_wizard():
    import settings as _settings
    # First run: no users yet - always allow
    if db.user_count() == 0:
        return _spa_index()
    # After first run: require admin login
    if not auth.is_admin():
        return redirect(url_for("auth.login_view", next="/setup?rerun=1"))
    if _settings.get("SETUP_COMPLETE", False) and request.args.get("rerun") != "1":
        return redirect(url_for("admin_library.ui_dashboard"))
    return _spa_index()


def _setup_gate():
    """None when the caller may use the setup surface (see
    auth.may_use_setup): nothing can log in yet, or an admin. Otherwise 401."""
    if not auth.may_use_setup():
        return jsonify(error="unauthorized"), 401
    return None


@bp.post("/setup/skip")
@limiter.limit("10 per minute")
def setup_skip():
    import settings as _settings
    denied = _setup_gate()
    if denied:
        return denied
    if _needs_first_admin():
        return jsonify(ok=True, needs_first_admin=True)
    _settings.set("SETUP_COMPLETE", True)
    return jsonify(ok=True)


@bp.get("/setup/schema")
@limiter.limit("30 per minute")
def setup_schema():
    """Steps and pre-filled fields for the wizard."""
    denied = _setup_gate()
    if denied:
        return denied
    import settings as _settings
    payload = _settings.wizard_schema_for_ui()
    payload["needs_first_admin"] = _needs_first_admin()
    return jsonify(**payload)


@bp.post("/setup/picker/<name>")
@limiter.limit("30 per minute")
def setup_picker(name: str):
    """Options for a wizard field filled from a service (arr root folders,
    quality profiles). Body: {"values": {KEY: value}}. Same gate as save.

    Only reachable without an admin session while the setup wizard itself is
    still incomplete - once SETUP_COMPLETE is set this is a real admin-only
    action (it makes the server issue requests to an attacker-chosen host),
    independent of auth.is_admin()'s "auth disabled = full access" shortcut.
    A blank posted value falls back to the saved credential."""
    denied = _setup_gate()
    if denied:
        return denied
    import service_tests
    if name not in service_tests.PICKERS:
        return jsonify(ok=False, error="unknown picker"), 404
    p = request.get_json(silent=True) or {}
    return jsonify(**service_tests.pick(name, p.get("values") or {}))


@bp.post("/setup/save")
@limiter.limit("10 per minute")
def setup_save():
    import settings as _settings
    denied = _setup_gate()
    if denied:
        return denied
    _allowed_keys = {k for k, f in _settings.fields_by_key().items() if f["kind"] != "custom"} | {"SETUP_COMPLETE"}
    saved = 0
    for key, value in request.form.items():
        if key not in _allowed_keys:
            log.warning("setup_save: rejected unknown key %r", key)
            continue
        try:
            # Treat empty strings as "clear override"
            if value == "":
                _settings.set(key, None)
            elif key in _settings._BOOL_KEYS:
                _settings.set(key, str(value).lower() in ("1", "true", "yes", "on"))
            else:
                _settings.set(key, value)
            saved += 1
        except ValueError as exc:
            log.warning("setup_save: rejected invalid value for %s: %s", key, exc)
    if _needs_first_admin():
        # Settings are saved; completion waits for the admin account, or this
        # install would finish setup with no way to log into it.
        log.info("Setup wizard saved %d settings, awaiting the first admin", saved)
        return jsonify(ok=True, saved=saved, needs_first_admin=True)
    _settings.set("SETUP_COMPLETE", True)
    log.info("Setup wizard saved %d settings", saved)
    return jsonify(ok=True, saved=saved)


@bp.post("/setup/test/<kind>")
@limiter.limit("20 per minute")
def setup_test(kind: str):
    """Test a single integration using values posted from the wizard form.

    Only reachable without an admin session while the setup wizard itself is
    still incomplete - once SETUP_COMPLETE is set this is a real admin-only
    action (it makes the server issue requests to an attacker-chosen host),
    independent of auth.is_admin()'s "auth disabled = full access" shortcut."""
    denied = _setup_gate()
    if denied:
        return denied
    import service_tests
    if kind not in service_tests.TESTS:
        return jsonify(ok=False, error="unknown integration"), 404
    p = request.get_json(silent=True)
    if isinstance(p, dict):
        # The schema-driven wizard posts JSON like the Settings page and reads {ok, message}.
        return jsonify(**service_tests.run(kind, p.get("values") or {}))
    r = service_tests.run(kind, dict(request.form))
    if r["ok"]:
        return jsonify(ok=True, detail=r["message"])
    return jsonify(ok=False, error=r["message"])


@bp.post("/ui/sync-movies")
def ui_sync_movies():
    if not auth.is_admin():
        abort(403)
    threading.Thread(target=monitor.sync_movies, name="movie-sync-manual", daemon=True).start()
    return redirect(url_for("admin_library.ui_dashboard") + "#movies")


@bp.get("/ui/logs")
def ui_logs():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    return jsonify(lines=log_buffer.get_lines(100))


@bp.post("/ui/run-cleanup")
def ui_run_cleanup():
    if not auth.is_admin():
        abort(403)
    threading.Thread(target=cleanup.run_cleanup, name="cleanup-manual", daemon=True).start()
    return redirect(url_for("admin_library.ui_dashboard") + "#repair")


@bp.post("/ui/repair-all")
def ui_repair_all():
    if not auth.is_admin():
        abort(403)
    threading.Thread(target=cleanup.run_cleanup, name="repair-all-manual", daemon=True).start()
    return redirect(url_for("admin_library.ui_dashboard") + "#repair")


@bp.post("/ui/refresh-images")
def ui_refresh_images():
    if not auth.is_admin():
        abort(403)
    threading.Thread(target=jellyfin.refresh_missing_images, name="jf-images", daemon=True).start()
    return redirect(url_for("admin_library.ui_dashboard") + "#repair")


@bp.post("/ui/merge-series")
def ui_merge_series():
    if not auth.is_admin():
        abort(403)
    threading.Thread(target=cleanup.merge_series_duplicates, name="merge-series", daemon=True).start()
    return redirect(url_for("admin_library.ui_dashboard") + "#repair")


@bp.post("/ui/api/repair-tvshow-titles")
@auth.require_role("admin")
def ui_api_repair_tvshow_titles():
    """Rewrite tvshow.nfo files whose title is 'Season XX' instead of the real show name."""
    result = nfo_generator.repair_tvshow_titles()
    return jsonify(**result)


@bp.post("/ui/api/fix-imdb-titles")
@auth.require_role("admin")
def ui_api_fix_imdb_titles():
    """Find items whose title is still a raw IMDB code, fetch real title from TMDB,
    rename folders on disk and update DB + strm paths."""
    result = strm_generator.fix_imdb_titles()
    return jsonify(**result)


@bp.post("/ui/generate-nfos")
def ui_generate_nfos():
    if not auth.is_admin():
        abort(403)
    def _run():
        nfo_generator.generate_all()
        nfo_generator.fetch_local_images()
    threading.Thread(target=_run, name="nfo-manual", daemon=True).start()
    return redirect(url_for("admin_library.ui_dashboard") + "#repair")


@bp.post("/api/run-cleanup")
def api_run_cleanup():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    threading.Thread(target=cleanup.run_cleanup, name="cleanup-api", daemon=True).start()
    return jsonify(ok=True, started="run_cleanup")


@bp.post("/api/generate-nfos")
def api_generate_nfos():
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    def _run():
        nfo_generator.generate_all()
        nfo_generator.fetch_local_images()
    threading.Thread(target=_run, name="nfo-api", daemon=True).start()
    return jsonify(ok=True, started="generate_nfos")


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


@bp.post("/ui/api/torbox/scan-library")
@auth.require_auth
def ui_api_torbox_scan_library():
    """Scan TorBox's own library for torrents we have no record of (e.g. after
    a DB reset) and materialize .strm files for them, resolving titles via
    TMDB where possible."""
    if not auth.is_admin():
        return jsonify(error="unauthorized"), 401
    result = strm_generator.scan_torbox_library()
    return jsonify(**result)


@bp.post("/ui/api/spore/backfill")
@auth.require_role("admin")
def ui_api_spore_backfill():
    """Generate missing Spore stub .mkv + .minfo files for all existing virtual_items."""
    result = strm_generator.backfill_spore_stubs()
    return jsonify(**result)


@bp.post("/ui/api/spore/regenerate")
@auth.require_role("admin")
def ui_api_spore_regenerate():
    """Force-regenerate stub MKVs with correct codec metadata.
    Pass ?token=<token> to regenerate a single item, or omit for all items."""
    token = request.args.get("token") or (request.json or {}).get("token")
    result = strm_generator.regenerate_spore_stubs(token=token)
    return jsonify(**result)


@bp.post("/ui/api/migrate-canonical")
def ui_api_migrate_canonical():
    """Rename all movie folders to TMDB canonical names and merge duplicates."""
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    result = strm_generator.migrate_to_canonical_names()
    return jsonify(**result)


@bp.post("/ui/api/cleanup-duplicate-strms")
def ui_api_cleanup_duplicate_strms():
    """Remove extra .strm files from folders that have more than one."""
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    result = strm_generator.cleanup_duplicate_strms()
    return jsonify(**result)


@bp.post("/ui/api/series-backfill")
def ui_api_series_backfill():
    """Import all Sonarr series + run series check to create .strm files for all episodes."""
    if not auth.is_admin():
        return jsonify(error="admin required"), 403
    threading.Thread(target=monitor.run_series_backfill, name="series-backfill", daemon=True).start()
    return jsonify(ok=True, started="series_backfill")
