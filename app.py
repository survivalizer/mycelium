import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

import auto_approve
import backup
import catbox
import nfo_generator
import catchup
import cleanup
import config as cfg
import db
import jellyfin
import library_sync
import log_buffer
import monitor
import retry_queue
import health_cache
import scrapers
import strm_generator
import tmdb
import torbox
import trending
import upgrader
import watchdog
from version import APP_VERSION
from config import (
    AUTO_APPROVE_INTERVAL_HOURS,
    AUTO_UPGRADE_ENABLED,
    AUTO_UPGRADE_INTERVAL_HOURS,
    BACKUP_INTERVAL_HOURS,
    CATBOX_GC_INTERVAL_MINUTES,
    CATBOX_MODE,
    CATCHUP_ENABLED,
    CLEANUP_INTERVAL_HOURS,
    LISTEN_HOST,
    LISTEN_PORT,
    MERGE_VERSIONS_INTERVAL_HOURS,
    MONITOR_INTERVAL_HOURS,
    MOVIE_SYNC_INTERVAL_MINUTES,
    QUOTA_CHECK_INTERVAL_HOURS,
    QUOTA_WARN_SIZE_GB,
    QUOTA_WARN_TORRENT_COUNT,
    RETRY_QUEUE_INTERVAL_MINUTES,
    SEASON_PACK_CHECK_INTERVAL_HOURS,
    SEASON_PACK_CONSOLIDATION_ENABLED,
    STRM_GENERATOR_INTERVAL_HOURS,
    TRENDING_CHECK_INTERVAL_HOURS,
    TRENDING_PRECACHE_COUNT,
    configure_logging,
)

configure_logging()
log_buffer.install()
log = logging.getLogger("mycelium")


_previous_schema_version = db.ensure_schema_version(APP_VERSION)
db.init()
db.record_schema_version(APP_VERSION)
log.info("Mycelium %s, database schema from %s", APP_VERSION,
         _previous_schema_version or "fresh")

import settings as _settings_mod
import migrate_filters
try:
    migrate_filters.migrate()
    migrate_filters.warn_stale_env()
except Exception:
    log.exception("Filter migration failed; leaving the new rule settings "
                  "untouched. The service starts with defaults and the rules "
                  "can be set in the admin UI.")

import deprecations
deprecations.warn_deprecated_env()
import migrate_source
try:
    migrate_source.migrate()
except Exception:
    log.exception("Source label migration failed; leaving virtual_items.source "
                  "and requests.source untouched. Existing rows keep whatever "
                  "value they already had.")


# appcore builds the Flask app, the CSRF guard, the rate limiter and
# LITE_MODE. Imported here rather than at the top of the file because
# LITE_MODE reads a setting and settings need db.init() above.
from appcore import LITE_MODE, app


import plugin_loader
if not LITE_MODE:
    plugin_loader.load_all(app)

import auth
import oidc
auth.install_before_request(app)
oidc.install(app)


# Blueprints last: every route in routes/ registers behind the
# before_request gate installed above, exactly where the first route
# used to be defined.
import routes
routes.register_all(app)


def _start_scheduler() -> BackgroundScheduler:
    # job_defaults: every interval job gets +/-60s jitter to avoid stampede when
    # multiple long-running jobs hit the same minute mark.
    scheduler = BackgroundScheduler(
        daemon=True,
        job_defaults={"jitter": 60, "coalesce": True, "max_instances": 1},
    )

    if MONITOR_INTERVAL_HOURS > 0:
        scheduler.add_job(
            monitor.run_series_check,
            trigger="interval", hours=MONITOR_INTERVAL_HOURS,
            id="series_monitor", next_run_time=None,
        )
        log.info("Scheduled series monitor every %dh", MONITOR_INTERVAL_HOURS)

    import seerr as _seerr
    if MOVIE_SYNC_INTERVAL_MINUTES > 0 and _seerr.is_configured():
        scheduler.add_job(
            monitor.sync_movies,
            trigger="interval", minutes=MOVIE_SYNC_INTERVAL_MINUTES,
            id="movie_sync", next_run_time=None,
        )
        scheduler.add_job(
            monitor.sync_series,
            trigger="interval", minutes=MOVIE_SYNC_INTERVAL_MINUTES,
            id="series_sync", next_run_time=None,
        )
        log.info("Scheduled Seerr movie+series sync every %dm", MOVIE_SYNC_INTERVAL_MINUTES)
    elif MOVIE_SYNC_INTERVAL_MINUTES > 0:
        log.info("Seerr sync skipped  -  SEERR_URL not configured (using SPA discovery instead)")

    if STRM_GENERATOR_INTERVAL_HOURS > 0:
        scheduler.add_job(
            # import_unknown=False: this is a timer, not a request. Importing
            # the whole TorBox account is something a person asks for with the
            # "Import TorBox library" button.
            lambda: strm_generator.run_and_refresh(import_unknown=False),
            trigger="interval", hours=STRM_GENERATOR_INTERVAL_HOURS,
            id="strm_generator", next_run_time=None,
        )
        log.info("Scheduled strm generator every %dh", STRM_GENERATOR_INTERVAL_HOURS)

    if CLEANUP_INTERVAL_HOURS > 0:
        scheduler.add_job(
            cleanup.run_cleanup,
            trigger="interval", hours=CLEANUP_INTERVAL_HOURS,
            id="strm_cleanup", next_run_time=None,
        )
        log.info("Scheduled strm cleanup every %dh", CLEANUP_INTERVAL_HOURS)

    import arr_sync
    ARR_SYNC_INTERVAL_MINUTES = int(_settings_mod.get("ARR_SYNC_INTERVAL_MINUTES", cfg.ARR_SYNC_INTERVAL_MINUTES) or 0)
    if ARR_SYNC_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            arr_sync.reconcile,
            trigger="interval", minutes=ARR_SYNC_INTERVAL_MINUTES,
            id="arr_sync", next_run_time=None,
        )
        log.info("Scheduled Radarr/Sonarr mirror reconcile every %dm (active when ARR_SYNC_ENABLED)",
                 ARR_SYNC_INTERVAL_MINUTES)
    else:
        log.info("Radarr/Sonarr mirror reconcile not scheduled (ARR_SYNC_INTERVAL_MINUTES=0)")

    if CATBOX_MODE:
        scheduler.add_job(
            strm_generator.repair_all_strms,
            trigger="interval", hours=6,
            id="strm_repair", next_run_time=None,
        )
        log.info("Scheduled automatic .strm repair (movies and series) every 6h")

    import disk_sync
    DISK_SYNC_INTERVAL_MINUTES = int(_settings_mod.get("DISK_SYNC_INTERVAL_MINUTES", cfg.DISK_SYNC_INTERVAL_MINUTES) or 0)
    if CATBOX_MODE and DISK_SYNC_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            disk_sync.reconcile,
            trigger="interval", minutes=DISK_SYNC_INTERVAL_MINUTES,
            id="disk_sync", next_run_time=None,
        )
        log.info("Scheduled on-disk deletion check every %dm", DISK_SYNC_INTERVAL_MINUTES)

    if CATBOX_MODE and CATBOX_GC_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            catbox.release_idle,
            trigger="interval", minutes=CATBOX_GC_INTERVAL_MINUTES,
            id="catbox_gc", next_run_time=None,
        )
        log.info("Scheduled Catbox GC every %dm (idle threshold %dm)",
                 CATBOX_GC_INTERVAL_MINUTES, cfg.CATBOX_IDLE_MINUTES)

    if CATBOX_MODE:
        scheduler.add_job(
            catbox.reconcile_torbox_ids,
            trigger="interval", minutes=60,
            id="torbox_reconcile", next_run_time=None,
        )
        log.info("Scheduled TorBox id reconcile every 60m")

    if BACKUP_INTERVAL_HOURS > 0:
        scheduler.add_job(
            backup.run,
            trigger="interval", hours=BACKUP_INTERVAL_HOURS,
            id="db_backup", next_run_time=None,
        )
        log.info("Scheduled DB backup every %dh", BACKUP_INTERVAL_HOURS)

    if RETRY_QUEUE_INTERVAL_MINUTES > 0:
        scheduler.add_job(
            retry_queue.run_due,
            trigger="interval", minutes=RETRY_QUEUE_INTERVAL_MINUTES,
            id="retry_queue", next_run_time=None,
        )
        log.info("Scheduled retry queue every %dm", RETRY_QUEUE_INTERVAL_MINUTES)

    # Probe CDN files for Plex stubs that have no track info yet (duration, audio, subs).
    # Runs every 30 min in a background thread to avoid blocking the scheduler.
    # build_fsh=False: only ffprobe, no 32MB download per file.
    if cfg.SPORE_ENABLED:
        def _run_probe_pending():
            import threading as _t
            _t.Thread(
                target=strm_generator.probe_pending_stubs,
                daemon=True,
                name="probe-pending-stubs",
            ).start()

        scheduler.add_job(
            _run_probe_pending,
            trigger="interval", minutes=30,
            id="probe_pending_stubs",
            next_run_time=None,
        )
        log.info("Scheduled probe_pending_stubs every 30m")

    if not LITE_MODE:
        if AUTO_UPGRADE_ENABLED and AUTO_UPGRADE_INTERVAL_HOURS > 0:
            scheduler.add_job(
                upgrader.run_auto_upgrade,
                trigger="interval", hours=AUTO_UPGRADE_INTERVAL_HOURS,
                id="auto_upgrade", next_run_time=None,
            )
            log.info("Scheduled auto-upgrade every %dh", AUTO_UPGRADE_INTERVAL_HOURS)

        if SEASON_PACK_CONSOLIDATION_ENABLED and SEASON_PACK_CHECK_INTERVAL_HOURS > 0:
            scheduler.add_job(
                upgrader.run_pack_consolidation,
                trigger="interval", hours=SEASON_PACK_CHECK_INTERVAL_HOURS,
                id="pack_consolidation", next_run_time=None,
            )
            log.info("Scheduled season-pack consolidation every %dh", SEASON_PACK_CHECK_INTERVAL_HOURS)

        if getattr(cfg, "WANTED_RECHECK_INTERVAL_HOURS", 0) > 0:
            scheduler.add_job(
                upgrader.recheck_wanted,
                trigger="interval", hours=cfg.WANTED_RECHECK_INTERVAL_HOURS,
                id="wanted_recheck", next_run_time=None,
            )
            log.info("Scheduled wanted-movie recheck every %dh", cfg.WANTED_RECHECK_INTERVAL_HOURS)

        _auto_add_total = (
            TRENDING_PRECACHE_COUNT
            + getattr(cfg, "TRENDING_TV_COUNT", 0)
            + getattr(cfg, "POPULAR_MOVIE_COUNT", 0)
            + getattr(cfg, "POPULAR_TV_COUNT", 0)
            + getattr(cfg, "NETFLIX_NL_TOP_COUNT", 0)
            + getattr(cfg, "PRIME_NL_TOP_COUNT", 0)
            + getattr(cfg, "DISNEY_NL_TOP_COUNT", 0)
        )
        if _auto_add_total > 0 and TRENDING_CHECK_INTERVAL_HOURS > 0:
            scheduler.add_job(
                trending.run,
                trigger="interval", hours=TRENDING_CHECK_INTERVAL_HOURS,
                id="trending_precache", next_run_time=None,
            )
            log.info("Scheduled auto-add every %dh (total slots: %d)",
                     TRENDING_CHECK_INTERVAL_HOURS, _auto_add_total)

        if AUTO_APPROVE_INTERVAL_HOURS > 0:
            scheduler.add_job(
                auto_approve.run,
                trigger="interval", hours=AUTO_APPROVE_INTERVAL_HOURS,
                id="auto_approve", next_run_time=None,
            )
            log.info("Scheduled auto-approve (genres + favorite actors) every %dh",
                     AUTO_APPROVE_INTERVAL_HOURS)

        if MERGE_VERSIONS_INTERVAL_HOURS > 0:
            scheduler.add_job(
                jellyfin.merge_duplicate_versions,
                trigger="interval", hours=MERGE_VERSIONS_INTERVAL_HOURS,
                id="merge_versions", next_run_time=None,
            )
            log.info("Scheduled MergeVersions every %dh", MERGE_VERSIONS_INTERVAL_HOURS)

    if QUOTA_CHECK_INTERVAL_HOURS > 0:
        scheduler.add_job(
            lambda: torbox.check_quota_and_warn(QUOTA_WARN_TORRENT_COUNT, QUOTA_WARN_SIZE_GB),
            trigger="interval", hours=QUOTA_CHECK_INTERVAL_HOURS,
            id="quota_warn", next_run_time=None,
        )
        log.info("Scheduled TorBox quota check every %dh", QUOTA_CHECK_INTERVAL_HOURS)

    def _zilean_native_sync():
        if _settings_mod.get("ZILEAN_MODE", cfg.ZILEAN_MODE) != "native":
            return
        import zilean_index
        zilean_index.sync()

    scheduler.add_job(_zilean_native_sync, trigger="interval", hours=6,
                       id="zilean_native_sync", next_run_time=None, max_instances=1)
    log.info("Scheduled Zilean native index sync every 6h (no-op unless ZILEAN_MODE=native)")

    # Watchdogs + maintenance
    scheduler.add_job(watchdog.deadman_check, trigger="interval", hours=2,
                       id="deadman", next_run_time=None, max_instances=1)
    scheduler.add_job(watchdog.disk_check, trigger="interval", hours=1,
                       id="disk_check", next_run_time=None, max_instances=1)
    # Aggressive pruning so volatile tables don't grow unbounded between scrapes.
    scheduler.add_job(lambda: db.prune_old(14), trigger="interval", hours=6,
                       id="prune_old", next_run_time=None, max_instances=1)
    # A retry still queued after a week is not going to succeed. Rows can sit
    # there indefinitely when run_due() keeps bailing on an exhausted TorBox
    # budget, because a row that is never processed never increments its
    # attempt and so never reaches the give-up threshold.
    scheduler.add_job(lambda: db.prune_retry_queue(7), trigger="interval", hours=12,
                      id="retry_queue_prune", next_run_time=None)
    scheduler.add_job(lambda: db.prune_webhook_events(24), trigger="interval", hours=6,
                       id="prune_webhooks", next_run_time=None, max_instances=1)
    scheduler.add_job(db.vacuum, trigger="interval", hours=24 * 7,
                       id="vacuum", next_run_time=None, max_instances=1)
    log.info("Scheduled watchdogs: deadman/2h, disk/1h, prune/24h, vacuum/weekly")

    # Apply max_instances=1 to all overlap-sensitive jobs already added
    for jid in ("strm_generator", "strm_cleanup", "series_monitor", "movie_sync",
                 "retry_queue", "auto_upgrade", "pack_consolidation",
                 "trending_precache", "db_backup",
                 "catbox_gc", "torbox_reconcile", "merge_versions", "quota_warn"):
        try:
            scheduler.modify_job(jid, max_instances=1)
        except Exception as exc:
            # Job may not exist if a feature is disabled  -  that's expected.
            log.debug("modify_job(%s): %s", jid, exc)

    scheduler.start()
    return scheduler


scheduler = _start_scheduler()
if not LITE_MODE:
    plugin_loader.register_jobs(scheduler)

if cfg.SPORE_ENABLED:
    try:
        import spore_server
        spore_server.start(port=cfg.SPORE_PORT)
    except Exception as _spore_exc:
        log.warning("Mycelium Spore server failed to start: %s", _spore_exc)

# Fast-start cache in dedicated subdir so media root stays clean
_fsh_cache_dir = cfg.SPORE_MEDIA_PATH + "/.fsh"
try:
    import mp4_faststart
    mp4_faststart.init(_fsh_cache_dir)
    log.info("MP4 fast-start cache dir: %s", _fsh_cache_dir)
except Exception as _fsh_exc:
    log.warning("MP4 fast-start init failed: %s", _fsh_exc)

if CATCHUP_ENABLED:
    catchup.schedule()

def _backfill_tmdb_ids() -> None:
    """Resolve tmdb_id for requests that only have imdb_id (e.g. Seerr imports)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, imdb_id, media_type FROM requests WHERE tmdb_id IS NULL AND imdb_id IS NOT NULL"
        ).fetchall()
    if not rows:
        return
    log.info("Backfilling tmdb_id for %d requests", len(rows))
    filled = 0
    for row in rows:
        kind = "tv" if row["media_type"] == "tv" else "movie"
        data = tmdb._get(f"/find/{row['imdb_id']}", params={"external_source": "imdb_id"})
        if not data:
            continue
        results = data.get(f"{kind}_results") or []
        if not results:
            other = "movie" if kind == "tv" else "tv"
            results = data.get(f"{other}_results") or []
        if results:
            tid = results[0].get("id")
            if tid:
                with db._connect() as conn:
                    conn.execute("UPDATE requests SET tmdb_id = ? WHERE id = ?", (tid, row["id"]))
                    conn.commit()
                filled += 1
    log.info("Backfilled tmdb_id for %d/%d requests", filled, len(rows))


# Kick off initial movie sync + strm scan ~10s after startup so /health
# answers fast on cold start and the scheduler isn't elbow-to-elbow with
# the wizard's first request.
def _delayed(seconds: float, target, name: str) -> None:
    def _run():
        import time as _t
        _t.sleep(seconds)
        try:
            target()
        except Exception:
            log.exception("startup task %s failed", name)
    threading.Thread(target=_run, name=name, daemon=True).start()


# Warm the scraper health cache so the first Overview after a boot shows
# real states instead of "unknown"; the Overview itself never probes inline.
_delayed(10.0, lambda: health_cache.refresh_async(scrapers.enabled_names()), "scraper-probe-warmup")
_delayed(15.0, monitor.sync_movies, "movie-sync-init")
_delayed(20.0, monitor.sync_series, "series-sync-init")
# import_unknown=False: booting is not a request to adopt the TorBox account.
_delayed(30.0, lambda: strm_generator.run_and_refresh(import_unknown=False), "strm-init")
_delayed(60.0, library_sync.resolve_unknowns, "resolve-unknowns-init")
_delayed(90.0, library_sync.import_series_to_monitored, "series-monitored-init")
_delayed(120.0, nfo_generator.generate_all, "nfo-init")
_delayed(150.0, nfo_generator.fetch_local_images, "images-init")
_delayed(45.0, _backfill_tmdb_ids, "tmdb-id-backfill")


if __name__ == "__main__":
    log.info("Starting Mycelium on %s:%d", LISTEN_HOST, LISTEN_PORT)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
