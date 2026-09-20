import { useState, type FormEvent, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api';
import type { RepairCounts, RepairItem } from '../../api';
import { Card, DataTable, StatTile } from '../../components/primitives';
import type { Column } from '../../components/primitives';

/** One maintenance action button: pending state, optional confirm() guard,
 * and an inline result line in place of the flash message the old
 * full-page-reload flow showed on the reloaded dashboard. */
function ActionButton({
  label,
  desc,
  confirmText,
  run,
  variant = 'default',
}: {
  label: string;
  desc: string;
  confirmText?: string;
  run: () => Promise<string | void>;
  variant?: 'default' | 'primary';
}) {
  const [state, setState] = useState<{ kind: 'idle' | 'pending' | 'ok' | 'err'; msg?: string }>({ kind: 'idle' });

  const onClick = async () => {
    if (confirmText && !confirm(confirmText)) return;
    setState({ kind: 'pending' });
    try {
      const msg = await run();
      setState({ kind: 'ok', msg: msg || undefined });
    } catch (e) {
      setState({ kind: 'err', msg: e instanceof Error ? e.message : String(e) });
    }
  };

  const busy = state.kind === 'pending';

  return (
    <div className="flex flex-col gap-1 border-b border-border py-2.5 last:border-0">
      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={onClick}
          disabled={busy}
          className={`rounded px-3 py-1.5 text-sm font-semibold disabled:opacity-50 ${
            variant === 'primary' ? 'bg-accent' : 'border border-border hover:bg-bg'
          }`}
        >
          {busy ? 'Working...' : label}
        </button>
        <span className="text-right text-xs text-muted">{desc}</span>
      </div>
      {state.kind === 'ok' && state.msg && <div className="font-mono text-xs text-ok">{state.msg}</div>}
      {state.kind === 'err' && <div className="font-mono text-xs text-danger">Error: {state.msg}</div>}
    </div>
  );
}

function GroupCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <div className="mb-1 text-sm font-semibold">{title}</div>
      <div>{children}</div>
    </Card>
  );
}

function LibraryGroup() {
  return (
    <GroupCard title="Library">
      <ActionButton
        label="Repair strm files"
        desc="Fix broken or expired strm files"
        variant="primary"
        run={async () => {
          await api.maintenanceRepairAll();
          return 'Repair All started - check Repair tab for results';
        }}
      />
      <ActionButton
        label="Run cleanup"
        desc="Remove dead or duplicate strm files"
        run={async () => {
          await api.maintenanceRunCleanup();
          return 'Cleanup scan started - check Repair tab for results';
        }}
      />
      <ActionButton
        label="Auto-upgrade"
        desc="Find better quality for existing items"
        run={async () => {
          await api.maintenanceAutoUpgrade();
          return 'Auto-upgrade scan started';
        }}
      />
      <ActionButton
        label="Consolidate packs"
        desc="Replace loose episodes with season packs"
        run={async () => {
          await api.maintenancePackConsolidate();
          return 'Season-pack consolidation started';
        }}
      />
      <ActionButton
        label="Merge series"
        desc="Combine duplicate series folders"
        confirmText="Merge duplicate series folders? Episodes will be moved to the canonical folder and duplicates removed."
        run={async () => {
          await api.maintenanceMergeSeries();
          return 'Series merge started - duplicate folders will be consolidated';
        }}
      />
      <ActionButton
        label="Fix IMDB titles"
        desc="Rename items still using tt-codes as title"
        run={async () => {
          const d = await api.fixImdbTitles();
          if (d.total === 0) return 'No items with IMDB-code titles found';
          const parts = [`Fixed ${d.fixed_count}/${d.total}`];
          if (d.failed.length) parts.push(`${d.failed.length} failed`);
          return parts.join(' - ');
        }}
      />
      <ActionButton
        label="Clear retry queue"
        desc="Drop every pending retry (they re-queue if they fail again)"
        confirmText={'Drop every pending retry?\n\nAnything still wanted will be re-queued the next time it fails.'}
        run={async () => {
          const d = await api.clearRetryQueue();
          return d.removed
            ? `Removed ${d.removed} pending retr${d.removed === 1 ? 'y' : 'ies'}`
            : 'The queue was already empty';
        }}
      />
      <ActionButton
        label="Fix library titles"
        desc='Rewrite NFO files that say "Season 01" instead of the real title'
        run={async () => {
          const d = await api.repairTvshowTitles();
          if (!d.fixed && !d.skipped) return 'Every tvshow.nfo already has the right title';
          return (
            `Fixed ${d.fixed}` +
            (d.skipped ? ` - ${d.skipped} skipped (no IMDB id in the nfo)` : '') +
            '. Run a Jellyfin scan to see the change.'
          );
        }}
      />
      <ActionButton
        label="Force strm rescan"
        desc="Rebuild strm files from the current library state"
        run={async () => {
          await api.maintenanceStrmRescan();
          return 'strm rescan started - check Logs';
        }}
      />
    </GroupCard>
  );
}

function ImportSyncGroup() {
  return (
    <GroupCard title="Import & Sync">
      <ActionButton
        label="Sync Seerr"
        desc="Pull pending requests from Seerr"
        run={async () => {
          await api.maintenanceSyncSeerr();
          return 'Movie sync started';
        }}
      />
      <ActionButton
        label="Import TorBox library"
        desc="Import existing TorBox torrents"
        run={async () => {
          await api.maintenanceLibraryImport();
          return 'Library import started - check Logs for progress';
        }}
      />
    </GroupCard>
  );
}

function JellyfinGroup() {
  return (
    <GroupCard title="Jellyfin">
      <ActionButton
        label="Fix covers"
        desc="Fetch missing posters from Jellyfin"
        run={async () => {
          await api.maintenanceFixCovers();
          return 'Jellyfin image refresh started - missing posters will be fetched';
        }}
      />
      <ActionButton
        label="Generate NFOs"
        desc="Write NFO files for Jellyfin matching"
        run={async () => {
          await api.maintenanceGenerateNfos();
          return 'NFO + image download started - Jellyfin will pick up metadata on next scan';
        }}
      />
    </GroupCard>
  );
}

function SystemGroup() {
  return (
    <GroupCard title="System">
      <ActionButton
        label="Vacuum DB"
        desc="Compact the SQLite database"
        confirmText="Run VACUUM on the DB? Reads are blocked briefly."
        run={async () => {
          await api.maintenanceDbVacuum();
          return 'DB vacuum started';
        }}
      />
      <ActionButton
        label="Recovery wizard"
        desc="Full pipeline: integrity + cleanup + import + scan"
        variant="primary"
        confirmText="Run the full recovery pipeline? Integrity check + cleanup + library import + strm scan. Safe but takes a minute or two."
        run={async () => {
          await api.maintenanceRecovery();
          return 'Recovery wizard started - runs integrity check + cleanup + import + strm scan';
        }}
      />
    </GroupCard>
  );
}

function RepairHistory() {
  const [query, setQuery] = useState('');
  const { data } = useQuery({ queryKey: ['repair-overview'], queryFn: api.repairOverview, refetchInterval: 120_000 });
  const items = data?.items ?? [];
  const lastCleanup = data?.last_cleanup ?? null;

  const q = query.trim().toLowerCase();
  const rows = q
    ? items.filter(
        (i) =>
          (i.title || i.path || '').toLowerCase().includes(q) ||
          (i.reason || '').toLowerCase().includes(q) ||
          (i.media_type || '').toLowerCase().includes(q),
      )
    : items;

  const columns: Column<RepairItem>[] = [
    {
      key: 'title',
      header: 'Title',
      render: (i) => (
        <span
          className="block max-w-[220px] overflow-hidden text-ellipsis whitespace-nowrap"
          title={i.path}
        >
          {i.title || i.path}
        </span>
      ),
    },
    { key: 'type', header: 'Type', render: (i) => <span className="text-muted">{i.media_type || '-'}</span> },
    { key: 'status', header: 'Status', render: (i) => <span>{i.status}</span> },
    {
      key: 'old',
      header: 'Old ID',
      render: (i) => <span className="font-mono text-xs text-muted">{i.old_torrent_id ?? '-'}</span>,
    },
    {
      key: 'hash',
      header: 'New Hash',
      render: (i) => (
        <span className="font-mono text-xs text-muted">
          {i.new_info_hash ? `${i.new_info_hash.slice(0, 16)}...` : '-'}
        </span>
      ),
    },
    {
      key: 'reason',
      header: 'Reason',
      render: (i) => (
        <span className="block max-w-[240px] overflow-hidden text-ellipsis whitespace-nowrap text-muted">
          {i.reason || '-'}
        </span>
      ),
    },
    {
      key: 'time',
      header: 'Time',
      render: (i) => <span className="text-muted">{(i.created_at || '').slice(0, 16)}</span>,
    },
  ];

  return (
    <section>
      <h2 className="mb-3 text-lg font-bold">Repair history</h2>
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <StatTile value={String(lastCleanup?.scanned ?? 0)} label="Scanned" />
        <StatTile value={String(lastCleanup?.repaired ?? 0)} label="Repaired" glow="ok" />
        <StatTile value={String(lastCleanup?.deleted ?? 0)} label="Deleted" />
        <StatTile value={String(lastCleanup?.unfixable ?? 0)} label="Unfixable" glow="danger" />
        <StatTile value={lastCleanup ? (lastCleanup.ran_at || '').slice(0, 16) : '-'} label="Last Run" />
      </div>
      {!lastCleanup && <p className="mb-3 text-sm text-muted">No cleanup run yet.</p>}
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search repairs..."
        className="mb-3 w-full max-w-sm rounded border border-border bg-bg px-3 py-2 text-sm"
      />
      <DataTable columns={columns} rows={rows} empty="No repair history yet" />
    </section>
  );
}

/** Ported from the pre-native-admin frontend/src/pages/Admin.tsx `ArrImportPanel`. */
function ArrImportPanel() {
  const [msg, setMsg] = useState('');
  const { data: s } = useQuery({
    queryKey: ['arr-import-status'],
    queryFn: api.arrStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : 5000),
  });

  const test = async (kind: 'radarr' | 'sonarr') => {
    setMsg(`Testing ${kind}...`);
    try {
      const r = await api.arrTest(kind);
      setMsg(r.ok ? `Reachable: ${kind}` : `Unreachable: ${kind}${r.error ? ' - ' + r.error : ''}`);
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    }
  };
  const run = async (kind: 'radarr' | 'sonarr') => {
    if (!confirm(`Start ${kind} import?`)) return;
    setMsg('');
    await api.arrRun(kind);
  };
  const backfill = async () => {
    if (
      !confirm(
        'Import all Sonarr series + search for all episodes and create .strm files. This runs in the background and may take a while. Continue?',
      )
    )
      return;
    setMsg('Series backfill started - runs in background, check logs for progress...');
    try {
      await api.seriesBackfill();
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const pct = s && s.total > 0 ? Math.round((s.done / s.total) * 100) : 0;

  return (
    <section>
      <h2 className="mb-3 text-lg font-bold">Radarr / Sonarr import</h2>
      <Card>
        <p className="mb-3 text-sm text-muted">
          Configure RADARR_URL/SONARR_URL + API keys in Settings, then test and run import here.
        </p>
        <div className="mb-3 flex flex-wrap gap-2">
          <button onClick={() => test('radarr')} className="rounded border border-border px-3 py-1.5 text-sm hover:bg-bg">
            Test Radarr
          </button>
          <button
            onClick={() => run('radarr')}
            disabled={s?.running}
            className="rounded bg-accent px-3 py-1.5 text-sm font-semibold disabled:opacity-50"
          >
            Import Radarr
          </button>
          <button onClick={() => test('sonarr')} className="rounded border border-border px-3 py-1.5 text-sm hover:bg-bg">
            Test Sonarr
          </button>
          <button
            onClick={() => run('sonarr')}
            disabled={s?.running}
            className="rounded bg-accent px-3 py-1.5 text-sm font-semibold disabled:opacity-50"
          >
            Import Sonarr
          </button>
          <button
            onClick={backfill}
            disabled={s?.running}
            className="rounded bg-accent px-3 py-1.5 text-sm font-semibold disabled:opacity-50"
          >
            Sync all series + episodes
          </button>
        </div>

        {s && (s.running || s.total > 0) && (
          <div className="mb-3">
            <div className="mb-1 flex justify-between text-xs text-muted">
              <span>
                {s.running ? `Importing ${s.kind}...` : `Finished ${s.kind || ''}`} - {s.message}
              </span>
              <span>
                {s.done}/{s.total} ({pct}%)
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded bg-bg">
              <div className={`h-full transition-all ${s.running ? 'bg-accent' : 'bg-ok'}`} style={{ width: `${pct}%` }} />
            </div>
            <div className="mt-1 flex gap-4 text-xs text-muted">
              <span className="text-ok">+{s.added} added</span>
              <span>{s.skipped} skipped</span>
              <span className="text-danger">{s.errors} errors</span>
            </div>
          </div>
        )}

        {msg && <div className="font-mono text-xs text-muted">{msg}</div>}
      </Card>
    </section>
  );
}

/** Ported from the pre-native-admin Admin.tsx `MaintenancePanel`: the four
 * filesystem-repair actions that only ever existed as JSON endpoints, never
 * as a form-encoded route. */
function FilesystemToolsPanel() {
  return (
    <section>
      <h2 className="mb-3 text-lg font-bold">Filesystem repair tools</h2>
      <Card>
        <ActionButton
          label="Migrate to canonical names"
          desc="Renames movie folders to TMDB canonical names and merges duplicates"
          confirmText="This renames movie folders to TMDB canonical names and removes duplicates. Jellyfin needs a full rescan afterwards. Continue?"
          run={async () => {
            const d = await api.migrateCanonical();
            return `scanned: ${d.scanned}, renamed: ${d.renamed}, duplicates removed: ${d.merged}, skipped: ${d.skipped}`;
          }}
        />
        <ActionButton
          label="Clean up duplicate strm files"
          desc="Removes extra .strm files from movie folders that have more than one. Series duplicates are handled by Repair strm files"
          run={async () => {
            const d = await api.cleanupDuplicateStrms();
            return `scanned: ${d.scanned}, cleaned: ${d.cleaned}`;
          }}
        />
        <ActionButton
          label="Repair broken strm files"
          desc="Rewrites .strm files that point at an expired address or a token Mycelium no longer knows, for movies and series"
          run={async () => {
            const d = await api.repairStrms();
            const sum = (k: keyof RepairCounts) => (d.movie?.[k] ?? 0) + (d.series?.[k] ?? 0);
            return `scanned: ${sum('scanned')}, ok: ${sum('ok')}, relinked: ${sum('relinked')}, requeued: ${sum('requeued')}, guarded: ${sum('guarded')}`;
          }}
        />
        <ActionButton
          label="Scan TorBox library"
          desc="Creates .strm files for anything already cached in TorBox that Mycelium has no record of"
          run={async () => {
            const d = await api.scanTorboxLibrary();
            return `scanned: ${d.scanned}, imported: ${d.imported}, skipped: ${d.skipped}`;
          }}
        />
      </Card>
    </section>
  );
}

/** A small manual-input card for an action whose live per-row UI (search
 * candidates, TorBox torrent list, backup list, show-override list) is out
 * of scope for this tab - it posts the same form-encoded admin route a
 * per-row form would. */
function QuickActionCard({
  title,
  placeholder,
  buttonLabel,
  confirmText,
  onSubmit,
}: {
  title: string;
  placeholder: string;
  buttonLabel: string;
  confirmText?: (value: string) => string;
  onSubmit: (value: string) => Promise<void>;
}) {
  const [value, setValue] = useState('');
  const [state, setState] = useState<{ kind: 'idle' | 'pending' | 'ok' | 'err'; msg?: string }>({ kind: 'idle' });

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const v = value.trim();
    if (!v) return;
    if (confirmText && !confirm(confirmText(v))) return;
    setState({ kind: 'pending' });
    try {
      await onSubmit(v);
      setState({ kind: 'ok' });
      setValue('');
    } catch (err) {
      setState({ kind: 'err', msg: err instanceof Error ? err.message : String(err) });
    }
  };

  const busy = state.kind === 'pending';

  return (
    <Card>
      <div className="mb-2 text-sm font-semibold">{title}</div>
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={placeholder}
          className="flex-1 rounded border border-border bg-bg px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-accent px-3 py-1.5 text-sm font-semibold disabled:opacity-50"
        >
          {busy ? 'Working...' : buttonLabel}
        </button>
      </form>
      {state.kind === 'ok' && <div className="mt-2 text-xs text-ok">Done.</div>}
      {state.kind === 'err' && <div className="mt-2 text-xs text-danger">Error: {state.msg}</div>}
    </Card>
  );
}

export default function Maintenance() {
  return (
    <div className="space-y-8">
      <div className="grid gap-4 md:grid-cols-2">
        <LibraryGroup />
        <ImportSyncGroup />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <JellyfinGroup />
        <SystemGroup />
      </div>

      <RepairHistory />

      <p className="text-sm text-muted">
        Per-title playability moved to the Library tab, in the title drawer.
      </p>

      <ArrImportPanel />

      <FilesystemToolsPanel />

      <section>
        <h2 className="mb-3 text-lg font-bold">Manual actions</h2>
        <div className="grid gap-4 md:grid-cols-2">
          <QuickActionCard
            title="Add magnet"
            placeholder="magnet:?xt=urn:btih:..."
            buttonLabel="Add"
            onSubmit={(v) => api.maintenanceAddMagnet(v)}
          />
          <QuickActionCard
            title="TorBox delete"
            placeholder="Torrent ID"
            buttonLabel="Delete"
            confirmText={() => 'Delete this torrent from TorBox?'}
            onSubmit={(v) => api.maintenanceTorboxDelete(v)}
          />
          <QuickActionCard
            title="Backup restore"
            placeholder="Backup filename"
            buttonLabel="Restore"
            confirmText={(v) => `Restore ${v}? The current DB is renamed to .pre-restore. Restart required after.`}
            onSubmit={(v) => api.maintenanceBackupRestore(v)}
          />
          <QuickActionCard
            title="Show override delete"
            placeholder="IMDB id (e.g. tt0111161)"
            buttonLabel="Clear"
            onSubmit={(v) => api.maintenanceShowOverrideDelete(v)}
          />
        </div>
      </section>
    </div>
  );
}
