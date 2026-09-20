import type {
  TmdbItem,
  TmdbDetail,
  Provider,
  WatchlistItem,
  UserRecord,
  UserRequest,
  SessionInfo,
  MediaType,
  WantedMovie,
  WantedEpisode,
  PersonDetail,
} from './types';

export const csrfToken = (): string => {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || '';
};

const metaContent = (name: string): string =>
  document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`)?.content || '';

export interface LoginFlags {
  oidcEnabled: boolean;
  oidcProvider: string;
  passwordEnabled: boolean;
  appVersion: string;
  needsFirstAdmin: boolean;
}

/** GET /ui/api/webhook-secret; previous_valid_until is set during the rotation grace window. */
export type WebhookSecretStatus = { secret: string; source: 'env' | 'auto'; previous_valid_until: string | null };

/** GET /ui/api/stats, built by stats.py's _build_overview(). */
export type StatsOverview = {
  library: { movie_count: number; episode_count: number; series_count: number };
  requests: { total: number; succeeded_7d: number; failed_7d: number; success_rate_7d: number };
  wanted: { active: number; found: number; give_up: number };
  movies_pending: number;
  egress_bytes_month: number;
  egress_estimated_bytes_month: number;
  qualities: Record<string, number>;
};

/** GET /ui/api/overview, built by overview.py. Service pings and the TorBox list are separate calls. */
export type OverviewPayload = {
  status: {
    scrapers: { name: string; state: 'ok' | 'slow' | 'down' | 'unknown' | 'disabled'; latency_ms: number | null }[];
    torbox_adds: { uncached: number; cached: number; limit: number; resets_in_sec: number; over: string | null };
    failures_7d: number;
    queue: { retry: number; wanted: number };
    attention: number;
    approvals: { pending: number; oldest_age_sec: number | null };
  };
  activity: {
    plays: { today: number; week: number; titles_today: number; titles_week: number };
    requests_7d: { total: number; succeeded: number; failed: number; success_rate: number };
    egress: { proxied_bytes: number; estimated_bytes: number };
  };
  library: {
    movies: number; episodes: number; series: number; wanted: number; upcoming: number;
    qualities: Record<string, number>;
    consistency: {
      db_items: number; strm_without_db: number; db_without_strm: number;
      arr_mirrored: number; arr_total: number;
      last_cleanup: { ran_at: string; deleted: number } | null;
      /** Hourly check of stored TorBox ids against TorBox's list; null until the first run. */
      torbox_ids?: { ran_at: string; checked: number; cleared: number; repointed: number; skipped: string | null } | null;
    };
  };
  torbox: {
    recent_streams: number; last_429_at: string | null; idle_minutes?: number | null;
    accounts: { id: number; label: string;
      adds: { uncached: number; cached: number; limit: number; resets_in_sec: number };
      torrents: number; last_429_at: string | null }[];
  };
  /** Names of the blocks whose backend source failed and fell back to a
   * neutral default: base, scrapers, torbox_adds, torbox_accounts, attention,
   * approvals, consistency, plays, last_429. Cells that read from a failed
   * block show "unavailable" instead of the (fake) default value. */
  errors: string[];
};

/** Whether OIDC / password login are available, read from the meta tags
 * _spa_index() embeds in the served HTML (see app.py). GET /ui/api/session
 * cannot be the source here: it 401s for a logged-out visitor, which is
 * exactly who the login page is for. */
export const loginFlags = (): LoginFlags => ({
  oidcEnabled: metaContent('oidc-enabled') === 'true',
  oidcProvider: metaContent('oidc-provider'),
  passwordEnabled: metaContent('password-enabled') !== 'false',
  appVersion: metaContent('app-version'),
  needsFirstAdmin: metaContent('needs-first-admin') === 'true',
});

async function http<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || 'GET').toUpperCase();
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  };
  if (method !== 'GET' && method !== 'HEAD') {
    headers['X-CSRFToken'] = csrfToken();
    if (init.body && !(init.body instanceof FormData)) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    }
  }
  const resp = await fetch(url, { ...init, headers, credentials: 'same-origin' });
  if (resp.status === 401) {
    if (typeof window !== 'undefined' && !window.location.pathname.endsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('unauthorized');
  }
  if (!resp.ok) {
    let detail = '';
    try {
      const j = await resp.json();
      detail = j.error || j.detail || JSON.stringify(j);
    } catch {
      detail = await resp.text();
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return (await resp.json()) as T;
}

/** POST helper for the form-encoded admin routes (maintenance/blacklist):
 * the Flask handler flashes a message and responds with a redirect to the
 * dashboard, not JSON. fetch() follows that redirect on its own, so
 * success is just response.ok after the follow - there is no JSON
 * body to parse. */
async function formPost(url: string, body?: Record<string, string>): Promise<void> {
  const headers: Record<string, string> = { 'X-CSRFToken': csrfToken() };
  let payload: string | undefined;
  if (body) {
    payload = new URLSearchParams(body).toString();
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
  }
  const resp = await fetch(url, { method: 'POST', headers, body: payload, credentials: 'same-origin' });
  if (resp.status === 401) {
    if (typeof window !== 'undefined' && !window.location.pathname.endsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('unauthorized');
  }
  if (!resp.ok) throw new Error(`${resp.status}`);
}

export const api = {
  // Discovery
  search: (q: string) =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/search?q=${encodeURIComponent(q)}`),
  trending: (type: 'all' | 'movie' | 'tv' = 'all', window: 'day' | 'week' = 'week') =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/trending?type=${type}&window=${window}`),
  popular: (type: MediaType = 'movie') =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/popular?type=${type}`),
  topRated: (type: MediaType = 'movie') =>
    http<{ results: TmdbItem[] }>(`/ui/api/discover/top-rated?type=${type}`),
  nowPlaying: () => http<{ results: TmdbItem[] }>('/ui/api/discover/now-playing'),
  upcoming: () => http<{ results: TmdbItem[] }>('/ui/api/discover/upcoming'),
  onTheAir: () => http<{ results: TmdbItem[] }>('/ui/api/discover/on-the-air'),
  providers: (type: MediaType = 'movie') =>
    http<{ providers: Provider[] }>(`/ui/api/discover/providers?type=${type}`),
  byProvider: (type: MediaType, providerId: number, sortBy?: string) =>
    http<{ results: TmdbItem[] }>(
      `/ui/api/discover/by-provider?type=${type}&provider_id=${providerId}${sortBy ? `&sort_by=${sortBy}` : ''}`,
    ),
  byGenre: (type: MediaType, genreId: number, yearFrom?: number | null, yearTo?: number | null) =>
    http<{ results: TmdbItem[] }>(
      `/ui/api/discover/by-genre?type=${type}&genre_id=${genreId}` +
      (yearFrom ? `&year_from=${yearFrom}` : '') + (yearTo ? `&year_to=${yearTo}` : ''),
    ),
  genreTabs: () =>
    http<{ tabs: GenreRule[] }>('/ui/api/discover/genre-tabs'),
  genreTabsConfig: () =>
    http<{ tabs: GenreRule[] }>('/ui/api/discover/genre-tabs/config'),
  setGenreTabsConfig: (tabs: GenreRule[]) =>
    http<{ ok: boolean }>('/ui/api/discover/genre-tabs/config', {
      method: 'POST',
      body: JSON.stringify({ tabs }),
    }),
  details: (type: MediaType, id: number) =>
    http<TmdbDetail>(`/ui/api/discover/details?type=${type}&id=${id}`),
  person: (id: number) =>
    http<PersonDetail>(`/ui/api/person/${id}`),
  favoriteActors: () =>
    http<{ actors: Array<{ person_id: number; name: string; profile_path: string | null }> }>(
      '/ui/api/favorite-actors',
    ),
  followActor: (personId: number, name: string, profilePath: string | null) =>
    http<{ ok: boolean }>(`/ui/api/favorite-actors/${personId}`, {
      method: 'POST',
      body: JSON.stringify({ name, profile_path: profilePath }),
    }),
  unfollowActor: (personId: number) =>
    http<{ ok: boolean }>(`/ui/api/favorite-actors/${personId}/remove`, { method: 'POST' }),
  addToLibrary: (
    tmdb_id: number,
    media_type: MediaType,
    title: string,
    opts?: { monitor_mode?: 'all' | 'future' | 'selected'; seasons?: number[] },
  ) =>
    http<{ status: string; request_id?: number; imdb_id?: string; error?: string }>(
      '/ui/api/discover/add',
      {
        method: 'POST',
        body: JSON.stringify({ tmdb_id, media_type, title, ...opts }),
      },
    ),

  // Watchlist
  watchlist: () => http<{ items: WatchlistItem[] }>('/ui/api/watchlist'),
  watchlistAdd: (params: {
    imdb_id: string;
    tmdb_id: number | null;
    media_type: MediaType;
    title: string;
    poster_path: string | null;
  }) =>
    http<{ ok: boolean }>('/ui/api/watchlist/add', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  watchlistRemove: (imdb_id: string, media_type: MediaType) =>
    http<{ ok: boolean }>('/ui/api/watchlist/remove', {
      method: 'POST',
      body: JSON.stringify({ imdb_id, media_type }),
    }),

  // User requests
  userRequests: (status?: string) =>
    http<{ items: UserRequest[] }>(
      '/ui/api/user-requests' + (status ? `?status=${status}` : ''),
    ),
  approveRequest: (id: number) =>
    http<{ ok: boolean }>(`/ui/api/user-requests/${id}/approve`, { method: 'POST' }),
  denyRequest: (id: number, note?: string) =>
    http<{ ok: boolean }>(`/ui/api/user-requests/${id}/deny`, {
      method: 'POST',
      body: JSON.stringify({ note }),
    }),
  reopenRequest: (id: number) =>
    http<{ ok: boolean; message: string }>(`/ui/api/user-requests/${id}/reopen`, { method: 'POST' }),
  adminQuotas: () => http<{ rows: QuotaRow[] }>('/ui/api/admin/quotas'),

  // Users (admin)
  users: () => http<{ users: UserRecord[] }>('/ui/api/users'),
  createUser: (params: {
    username: string;
    password: string;
    role?: 'user' | 'admin';
    auto_approve?: boolean;
  }) =>
    http<{ ok: boolean; user_id: number; message?: string }>('/ui/api/users/create', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  updateUser: (id: number, fields: Partial<UserRecord> & { password?: string }) =>
    http<{ ok: boolean }>(`/ui/api/users/${id}/update`, {
      method: 'POST',
      body: JSON.stringify(fields),
    }),
  deleteUser: (id: number) =>
    http<{ ok: boolean }>(`/ui/api/users/${id}/delete`, { method: 'POST' }),

  // Account
  changePassword: (current: string, password: string) =>
    http<{ ok: boolean; error?: string }>('/ui/api/me/password', {
      method: 'POST',
      body: JSON.stringify({ current, password }),
    }),

  // Plugin user fields (self-service toggle)
  setPluginFields: (fields: Record<string, boolean>) =>
    http<{ ok: boolean }>('/ui/api/me/plugin-fields', {
      method: 'POST',
      body: JSON.stringify(fields),
    }),

  // Region
  setRegion: (region: string) =>
    http<{ ok: boolean; region: string }>('/ui/api/me/region', {
      method: 'POST',
      body: JSON.stringify({ region }),
    }),

  // User preferences
  setPreferences: (prefs: Record<string, boolean | string>) =>
    http<{ ok: boolean }>('/ui/api/me/preferences', {
      method: 'POST',
      body: JSON.stringify(prefs),
    }),

  // Jellyfin item lookup (single)
  jellyfinItem: (imdb_id: string) =>
    http<{ jellyfin_id: string | null; jellyfin_url: string | null }>(`/ui/api/jellyfin/item?imdb_id=${encodeURIComponent(imdb_id)}`),

  // Jellyfin batch lookup
  jellyfinItems: (imdb_ids: string[]) =>
    http<{ jellyfin_url: string | null; items: Record<string, string | null> }>(
      `/ui/api/jellyfin/items?imdb_ids=${imdb_ids.map(encodeURIComponent).join(',')}`,
    ),

  // TMDB find by IMDB id
  tmdbFind: (imdb_id: string) =>
    http<{ tmdb_id: number | null; media_type: string | null }>(`/ui/api/tmdb/find?imdb_id=${encodeURIComponent(imdb_id)}`),

  // Library / dashboard
  session: () => http<SessionInfo>('/ui/api/session'),
  loginFlags,
  stats: () => http<StatsOverview>('/ui/api/stats'),
  overview: () => http<OverviewPayload>('/ui/api/overview'),
  libraryStatusMap: () => http<Record<string, string>>('/ui/api/library/status-map'),
  libraryMovies: (opts?: { page?: number; pageSize?: number; search?: string; filter?: string }) => {
    const params = new URLSearchParams();
    if (opts?.page) params.set('page', String(opts.page));
    if (opts?.pageSize) params.set('page_size', String(opts.pageSize));
    if (opts?.search) params.set('search', opts.search);
    if (opts?.filter && opts.filter !== 'all') params.set('filter', opts.filter);
    const qs = params.toString();
    return http<{
      items: any[]; total: number; page: number; page_size: number;
      counts: { all: number; available: number; wanted: number };
    }>(`/ui/api/library/movies${qs ? `?${qs}` : ''}`);
  },
  library: (params: Record<string, string | string[]>) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => (Array.isArray(v) ? v : [v]).forEach((x) => x !== '' && qs.append(k, x)));
    return http<LibraryPage>(`/ui/api/library?${qs.toString()}`);
  },
  libraryViews: () => http<{ counts: Record<string, number>; mirror_on: boolean }>('/ui/api/library/views'),
  adminRequests: (params: Record<string, string>) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== '' && qs.append(k, v));
    return http<AdminRequestPage>(`/ui/api/admin/requests?${qs.toString()}`);
  },
  adminRequestViews: () => http<{ counts: Record<string, number> }>('/ui/api/admin/requests/views'),
  libraryDetail: (imdb: string) => http<LibraryDetail>(`/ui/api/library/${imdb}`),
  librarySeason: (imdb: string, season: number) => http<{ episodes: SeasonEpisode[] }>(`/ui/api/library/${imdb}/season/${season}`),
  libraryCandidates: (imdb: string, season?: number, episode?: number) => {
    // season alone = the season packs for a whole season
    const qs = season != null ? (episode != null ? `?season=${season}&episode=${episode}` : `?season=${season}`) : '';
    return http<CandidatesResponse>(`/ui/api/library/${imdb}/candidates${qs}`);
  },
  librarySwap: (imdb: string, body: { info_hash: string; season?: number; episode?: number; blacklist_old: boolean }) =>
    http<SwapResult>(`/ui/api/library/${imdb}/swap`, { method: 'POST', body: JSON.stringify(body) }),
  libraryActivity: (imdb: string, before: number) => http<{ activity: LibraryDetail['activity'] }>(`/ui/api/library/${imdb}/activity?before=${before}`),
  libraryAction: (path: string, method: 'POST' | 'DELETE' = 'POST', body?: unknown) =>
    http<{ ok: boolean; message: string }>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) }),
  recent: () => http<{ items: any[] }>('/ui/api/activity'),
  myRequests: () => http<{ items: any[] }>('/ui/api/user-requests?mine=1'),
  myQuota: () => http<QuotaInfo>('/ui/api/me/quota'),
  scraperHealth: () => http<ScraperHealth>('/ui/api/scraper-health'),
  adminLogs: (limit?: number, level?: string) => {
    const params = new URLSearchParams();
    if (limit !== undefined) params.set('limit', String(limit));
    if (level) params.set('level', level);
    const qs = params.toString();
    return http<{ lines: LogLine[] }>(`/ui/api/logs${qs ? `?${qs}` : ''}`);
  },

  // Zilean native index (Scrapers tab)
  zileanStatus: () => http<ZileanStatus>('/ui/api/zilean/status'),
  zileanSync: (force = false) =>
    http<{ ok?: boolean; started?: boolean; error?: string }>('/ui/api/zilean/sync', {
      method: 'POST',
      body: JSON.stringify({ force }),
    }),
  zileanImport: () =>
    http<{ ok?: boolean; started?: boolean; error?: string }>('/ui/api/zilean/import', {
      method: 'POST',
    }),

  // Admin: Overview tab
  health: () => http<{ services: HealthService[]; stream_front?: boolean }>('/ui/api/health'),
  activity: () => http<{ events: ActivityEvent[] }>('/ui/api/activity'),
  webhookSecret: () => http<WebhookSecretStatus>('/ui/api/webhook-secret'),
  rotateWebhookSecret: () => http<WebhookSecretStatus>('/ui/api/webhook-secret/rotate', { method: 'POST' }),
  torboxAccounts: () => http<{ accounts: TorboxAccount[] }>('/ui/api/torbox-accounts'),
  torboxAccountAdd: (body: { label: string; api_key: string }) =>
    http<{ ok: boolean; id?: number; message: string }>('/ui/api/torbox-accounts', { method: 'POST', body: JSON.stringify(body) }),
  torboxAccountUpdate: (id: number, body: { label?: string; api_key?: string; enabled?: boolean }) =>
    http<{ ok: boolean; message: string }>(`/ui/api/torbox-accounts/${id}`, { method: 'POST', body: JSON.stringify(body) }),
  torboxAccountDelete: (id: number) => http<{ ok: boolean; message: string }>(`/ui/api/torbox-accounts/${id}`, { method: 'DELETE' }),
  torboxAccountTest: (id: number) => http<{ ok: boolean; message: string }>(`/ui/api/torbox-accounts/${id}/test`, { method: 'POST' }),
  torboxList: () => http<{ torrents: TorboxTorrent[] }>('/ui/api/torbox-list'),
  retryQueue: () => http<{ items: unknown[] }>('/ui/api/retry-queue'),
  releases: () => http<{ releases: Release[] }>('/ui/api/releases'),
  torboxQuota: () => http<TorboxQuota>('/ui/api/torbox-quota'),
  torboxUsage: () => http<TorBoxUsage>('/ui/api/torbox-usage'),
  metricsSummary: () => http<MetricsSummary>('/ui/api/metrics-summary'),
  storage: () => http<{ folders: StorageFolder[] }>('/ui/api/storage'),
  libraryHealth: () => http<LibraryHealth>('/ui/api/orphans'),

  // Arr import
  // Both take the URL and key currently typed in Settings, so a person can
  // test and browse before saving; blank fields fall back to saved values.
  arrTest: (kind: 'radarr' | 'sonarr', conn: ArrConn = {}) =>
    http<{ ok: boolean; version?: string | null; error?: string }>(`/ui/api/arr-import/test-${kind}`, {
      method: 'POST',
      body: JSON.stringify(conn),
    }),
  arrRun: (kind: 'radarr' | 'sonarr') =>
    http<{ ok: boolean }>(`/ui/api/arr-import/${kind}`, {
      method: 'POST',
      body: JSON.stringify({ only_monitored: true }),
    }),
  arrStatus: () =>
    http<{
      running: boolean;
      kind: string | null;
      total: number;
      done: number;
      added: number;
      skipped: number;
      errors: number;
      message: string;
    }>('/ui/api/arr-import/status'),

  autoAddNow: () =>
    http<{ ok: boolean; message?: string }>('/ui/api/auto-add-now', { method: 'POST' }),

  // Wanted lists
  wantedMovies: () => http<{ items: WantedMovie[] }>('/ui/api/wanted-movies'),
  wantedRecheck: () => http<{ ok: boolean; message?: string }>('/ui/api/wanted-recheck', { method: 'POST' }),
  wantedEpisodes: () => http<{ items: WantedEpisode[] }>('/ui/api/wanted-episodes'),

  // Failed processing requests
  failedRequests: () => http<{ items: any[] }>('/ui/api/requests/failed'),
  retryRequest: (id: number) =>
    http<{ ok: boolean; title?: string }>(`/ui/api/requests/${id}/retry`, { method: 'POST' }),
  deleteRequest: (id: number) =>
    http<{ ok: boolean }>(`/ui/api/requests/${id}/delete`, { method: 'POST' }),
  // Admin only: also deletes the .strm files, so the title leaves Jellyfin/Plex.
  purgeRequest: (id: number) =>
    http<{ ok: boolean; strms: number }>(`/ui/api/requests/${id}/purge`, { method: 'POST' }),

  // Trakt
  traktStatus: () =>
    http<{ connected: boolean; username: string | null; synced_at: string | null; configured: boolean }>(
      '/ui/api/trakt/status'
    ),
  traktAuthStart: () =>
    http<{ user_code: string; verification_url: string; expires_in: number; interval: number }>(
      '/ui/api/trakt/auth/start', { method: 'POST' }
    ),
  traktAuthPoll: () =>
    http<{ status: string; username?: string; error?: string }>('/ui/api/trakt/auth/poll'),
  traktRevoke: () =>
    http<{ ok: boolean }>('/ui/api/trakt/auth/revoke', { method: 'POST' }),
  traktSync: () =>
    http<{ ok: boolean; added: number }>('/ui/api/trakt/sync', { method: 'POST' }),
  traktSyncWatched: () =>
    http<{ ok: boolean; watched: number }>('/ui/api/trakt/sync-watched', { method: 'POST' }),
  traktWatched: () =>
    http<{ imdb_ids: string[] }>('/ui/api/trakt/watched'),
  traktWatchedEpisodes: () =>
    http<{ shows: Record<string, Record<string, number[]>> }>('/ui/api/trakt/watched-episodes'),
  traktScrobble: (params: {
    action: 'start' | 'pause' | 'stop';
    media_type: string;
    imdb_id: string;
    progress: number;
    season?: number;
    episode?: number;
    title?: string;
  }) =>
    http<{ ok: boolean }>('/ui/api/trakt/scrobble', {
      method: 'POST',
      body: JSON.stringify(params),
    }),

  // Maintenance
  repairStrms: () =>
    http<{ movie: RepairCounts; series: RepairCounts }>('/ui/api/repair-strms', { method: 'POST' }),
  scanTorboxLibrary: () =>
    http<{ scanned: number; imported: number; skipped: number; failed: number }>(
      '/ui/api/torbox/scan-library', { method: 'POST' }
    ),
  migrateCanonical: () =>
    http<{ scanned: number; renamed: number; merged: number; skipped: number; no_imdb: number; errors?: number }>(
      '/ui/api/migrate-canonical', { method: 'POST' }
    ),
  cleanupDuplicateStrms: () =>
    http<{ scanned: number; cleaned: number; skipped?: number }>(
      '/ui/api/cleanup-duplicate-strms', { method: 'POST' }
    ),
  seriesBackfill: () =>
    http<{ ok: boolean; started: string }>('/ui/api/series-backfill', { method: 'POST' }),

  // Maintenance tab: 14 form-encoded admin routes. Each POSTs and redirects
  // to the dashboard (flash message on the reloaded page) rather than
  // returning JSON, so these go through formPost, not http().
  maintenanceRepairAll: () => formPost('/ui/repair-all'),
  maintenanceRunCleanup: () => formPost('/ui/run-cleanup'),
  maintenanceAutoUpgrade: () => formPost('/ui/auto-upgrade'),
  maintenancePackConsolidate: () => formPost('/ui/pack-consolidate'),
  maintenanceMergeSeries: () => formPost('/ui/merge-series'),
  maintenanceSyncSeerr: () => formPost('/ui/sync-movies'),
  maintenanceLibraryImport: () => formPost('/ui/library-import'),
  maintenanceFixCovers: () => formPost('/ui/refresh-images'),
  maintenanceGenerateNfos: () => formPost('/ui/generate-nfos'),
  maintenanceDbVacuum: () => formPost('/ui/db-vacuum'),
  maintenanceRecovery: () => formPost('/ui/recovery'),
  maintenanceStrmRescan: () => formPost('/ui/strm-rescan'),

  // The three JS-driven maintenance actions: these DO return JSON.
  fixImdbTitles: () =>
    http<{ fixed: Array<Record<string, unknown>>; failed: Array<Record<string, unknown>>; total: number; fixed_count: number }>(
      '/ui/api/fix-imdb-titles', { method: 'POST' }
    ),
  repairTvshowTitles: () =>
    http<{ fixed: number; skipped: number }>('/ui/api/repair-tvshow-titles', { method: 'POST' }),
  clearRetryQueue: () =>
    http<{ ok: boolean; removed: number }>('/ui/api/retry-queue/clear', { method: 'POST' }),

  // Maintenance tab: repair history feed (summary + item list).
  repairOverview: () => http<{ items: RepairItem[]; last_cleanup: RepairSummary | null }>('/ui/api/repair'),
  playabilityState: () => http<{ items: PlayabilityItem[] }>('/ui/api/playability-state'),
  reResolve: (token: string) =>
    http<{ ok: boolean; resolved: boolean; title?: string; hint?: string }>(
      `/ui/api/virtual-items/${encodeURIComponent(token)}/re-resolve`, { method: 'POST' }),

  // Maintenance tab: small manual-input cards for actions whose live UI
  // (search candidates, TorBox list, backup list, show-override list) is
  // out of scope for this tab - each posts the same form-encoded admin
  // route a per-row form would, plain redirect response.
  maintenanceAddMagnet: (magnet: string) => formPost('/ui/add-magnet', { magnet }),
  maintenanceTorboxDelete: (torrentId: string) => formPost('/ui/torbox-delete', { torrent_id: torrentId }),
  maintenanceBackupRestore: (name: string) => formPost('/ui/backup-restore', { name }),
  maintenanceShowOverrideDelete: (imdbId: string) =>
    formPost(`/ui/show-override-delete/${encodeURIComponent(imdbId)}`),

  // Blacklist tab
  blacklist: () => http<{ items: BlacklistItem[] }>('/ui/api/blacklist'),
  blacklistClear: (infoHash: string) => formPost(`/ui/blacklist-clear/${encodeURIComponent(infoHash)}`),

  // Auto-approve (genre rules + favorite actors)
  genres: (type: 'movie' | 'tv') =>
    http<{ genres: Array<{ id: number; name: string }> }>(`/ui/api/genres?type=${type}`),
  autoApproveGenreRules: () =>
    http<{ rules: GenreRule[] }>('/ui/api/auto-approve/genre-rules'),
  setAutoApproveGenreRules: (rules: GenreRule[]) =>
    http<{ ok: boolean }>('/ui/api/auto-approve/genre-rules', {
      method: 'POST',
      body: JSON.stringify({ rules }),
    }),
  runAutoApproveNow: () =>
    http<{ ok: boolean; started: boolean }>('/ui/api/auto-approve/run-now', { method: 'POST' }),

  // MDBList
  mdblistStatus: () =>
    http<{ connected: boolean; list_ids: string }>('/ui/api/mdblist/status'),
  mdblistConnect: (apiKey: string) =>
    http<{ ok: boolean }>('/ui/api/mdblist/connect', {
      method: 'POST',
      body: JSON.stringify({ api_key: apiKey }),
    }),
  mdblistDisconnect: () =>
    http<{ ok: boolean }>('/ui/api/mdblist/disconnect', { method: 'POST' }),
  mdblistLists: () =>
    http<{ lists: Array<{ id: number; name: string }> }>('/ui/api/mdblist/lists'),
  mdblistSetLists: (listIds: (string | number)[]) =>
    http<{ ok: boolean }>('/ui/api/mdblist/lists', {
      method: 'POST',
      body: JSON.stringify({ list_ids: listIds }),
    }),
  mdblistSync: () =>
    http<{ ok: boolean; added: number }>('/ui/api/mdblist/sync', { method: 'POST' }),

  // Settings (admin)
  settings: () =>
    http<{ groups: Array<{ id: string; title: string; items: SettingItem[] }>; hot_reload: string[] }>(
      '/ui/api/settings',
    ),
  settingsSchema: () =>
    http<{ sections: SettingsSection[]; hot_reload: string[] }>('/ui/api/settings/schema'),
  setupSchema: () => http<SetupSchema>('/setup/schema'),
  setupTest: (service: string, values: Record<string, string>) =>
    http<{ ok: boolean; message: string }>(`/setup/test/${service}`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    }),
  setupPicker: (name: string, values: Record<string, string>) =>
    http<{ ok: boolean; options?: { value: string; label: string }[]; error?: string }>(
      `/setup/picker/${name}`,
      { method: 'POST', body: JSON.stringify({ values }) },
    ),
  settingsTest: (service: string, values: Record<string, string>) =>
    http<{ ok: boolean; message: string }>(`/ui/api/settings/test/${service}`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    }),
  settingsPicker: (name: string, values: Record<string, string>) =>
    http<{ ok: boolean; options?: { value: string; label: string }[]; error?: string }>(
      `/ui/api/settings/picker/${name}`,
      { method: 'POST', body: JSON.stringify({ values }) },
    ),
  setNotificationSettings: (values: Record<string, boolean | string>) =>
    http<{ ok: boolean }>('/ui/api/settings/notifications', {
      method: 'POST',
      body: JSON.stringify(values),
    }),
  // The settings form-encoded admin route: POSTs setting_<KEY> fields
  // form-encoded and redirects back to the dashboard rather than returning
  // JSON, same shape as the maintenance routes above, so this goes
  // through formPost.
  saveSettings: (fields: Record<string, string>) => formPost('/ui/settings', fields),
  // The legacy shared fallback password (`POST /ui/set-password`, admin-only,
  // no current-password check) - distinct from a user's own password, which
  // goes through the JSON `changePassword` above with a current-password
  // check. Same form-post/redirect shape as saveSettings.
  setLegacyPassword: (password: string) => formPost('/ui/set-password', { password }),

  // Application shell (nav counts + TorBox pill)
  shellSummary: () => http<ShellSummary>('/ui/api/shell-summary'),
};

export type ShellSummary = {
  counts: { watchlist: number; requests: number; wanted: number };
  torbox: { state: 'ok' | 'degraded' | 'down'; label: string };
};

export type QuotaInfo = { used: number; limit: number; resets_at: string; unlimited: boolean };

export type ScraperHealth = {
  scrapers: {
    name: string;
    latency_ms: number | null;
    state: 'ok' | 'slow' | 'down' | 'unknown';
    samples: number;
  }[];
};

export type LogLine = { time: string; level: string; name: string; msg: string };

export type ZileanStatus = {
  mode: string;
  total_hashes?: number;
  last_synced_at?: string | null;
  last_status?: string;
  last_new_hashes?: number;
  last_pages_processed?: number;
  last_error?: string | null;
  last_import_at?: string | null;
  last_import_count?: number;
  last_import_error?: string | null;
  syncing?: boolean;
  importing?: boolean;
};

export type HealthService = { name: string; status: string; note?: string; error?: string; code?: number };
export type PlayabilityItem = {
  content_key: string;
  status: string;
  last_ok_provider: string | null;
  last_ok_at: string | null;
  last_fail_reason: string | null;
  consecutive_failures: number;
  updated_at: string;
  title: string | null;
  token: string | null;
  strm_path: string | null;
};

export type ActivityEvent = {
  id: number;
  created_at: string;
  event: string;
  title?: string | null;
  message?: string | null;
  success?: boolean;
};

export type TorboxTorrent = {
  id: number;
  name: string;
  hash: string;
  size: number;
  download_state: string;
  download_finished: boolean;
  progress: number;
  created_at: string;
  file_count: number;
};

export type Release = { version: string; date: string; notes: string[] };

export type TorboxQuota = {
  /** Uncached adds this hour: the figure TorBox limits. */
  count: number;
  /** Cached adds this hour; TorBox does not count them against the hour. */
  cached_count: number;
  limit: number;
  window_sec: number;
  by_reason: Record<string, number>;
  oldest_ts: number | null;
  resets_in_sec: number;
};

export type TorBoxUsage = {
  usage: {
    torrent_count: number;
    total_bytes: number;
    total_gb: number;
    states: Record<string, number>;
  };
  plan: string | null;
};

export type MetricRow = { label: string; count: number; avg_real: number | null; sum_int: number | null };

export type MetricsSummary = {
  quality: MetricRow[];
  sources: MetricRow[];
  unique_sources: MetricRow[];
  latency: MetricRow[];
  failures: MetricRow[];
};

export type StorageFolder = { path: string; count: number };

export type LibraryHealth = {
  strm_count: number;
  db_count: number;
  strm_without_db: number;
  db_without_strm: number;
};

export interface RepairItem {
  path: string;
  title: string | null;
  media_type: string | null;
  status: string;
  old_torrent_id: string | number | null;
  new_info_hash: string | null;
  reason: string | null;
  created_at: string | null;
}

export type RepairCounts = {
  scanned: number; ok: number; missing_strm: number; orphaned_tokens: number;
  relinked: number; requeued: number; skipped: number; guarded: number;
};

export interface RepairSummary {
  scanned: number;
  repaired: number;
  deleted: number;
  unfixable: number;
  ran_at: string;
}

export interface BlacklistItem {
  info_hash: string;
  fail_count: number;
  last_error: string | null;
  last_attempt: string | null;
  titles: { imdb_id: string; title: string }[];
}

export interface ArrConn {
  url?: string;
  api_key?: string;
}

export interface SettingItem {
  key: string;
  value: any;
  kind: 'bool' | 'list' | 'int' | 'float' | 'str' | 'enum';
  options?: string[] | null;
  overridden: boolean;
  hot_reload: boolean;
}

export type SettingKind =
  | 'bool' | 'int' | 'float' | 'str' | 'list' | 'url' | 'path' | 'secret'
  | 'select' | 'multiselect' | 'ordered' | 'custom';

export interface SettingsField {
  key: string;
  label: string;
  help: string;
  kind: SettingKind;
  options: { value: string; label: string }[] | null;
  placeholder: string | null;
  unit: string | null;
  min: number | null;
  max: number | null;
  advanced: boolean;
  depends_on: string | null;
  test: string | null;
  picker: string | null;
  component: string | null;
  readonly: boolean;
  required: boolean;
  value: any;
  overridden: boolean;
  hot_reload: boolean;
}

export interface SettingsSection {
  id: string;
  title: string;
  description: string;
  icon: string;
  fields: SettingsField[];
}

export interface WizardStepDef {
  id: string;
  title: string;
  intro: string;
  keys: string[];
  lite: boolean;
}

export interface SetupSchema {
  steps: WizardStepDef[];
  fields: SettingsField[];
  needs_first_admin: boolean;
}

/** GET /ui/api/library row shape. */
export interface LibraryRow {
  id: number;
  imdb_id: string;
  tmdb_id: number | null;
  title: string;
  media_type: string;
  status: string;
  error: string | null;
  quality: string | null;
  source: string | null;
  info_hash: string | null;
  seasons: string | null;
  created_at: string;
  updated_at: string;
  requester: string;
  requester_id: number | null;
  requested_at: string | null;
  playability: { status: string; last_fail_reason: string | null } | null;
  missing_episodes: number;
  retry: { attempt: number; next_retry_at: string } | null;
  arr_mirrored: boolean;
  in_torbox: boolean;
  in_wanted_movies: boolean;
}

export interface LibraryPage {
  rows: LibraryRow[];
  total: number;
  page: number;
  per_page: number;
}

/** GET /ui/api/admin/requests row shape (Requests admin tab). */
export interface AdminRequestRow {
  id: number; imdb_id: string; tmdb_id: number | null; title: string; media_type: string; seasons: string | null;
  status: 'pending' | 'approved' | 'denied'; note: string | null; created_at: string; reviewed_at: string | null;
  user_id: number; username: string; reviewer: string | null; library_status: string | null;
}

export interface AdminRequestPage { rows: AdminRequestRow[]; total: number; page: number; per_page: number }

/** GET /ui/api/admin/quotas row shape (Requests admin tab). */
export interface QuotaRow {
  user_id: number; username: string; used: number; limit: number; remaining: number | null;
  unlimited: boolean; resets_at: string; auto_approve: boolean; paused: boolean; enabled: boolean;
}

export interface RequestRow {
  id: number;
  title: string;
  imdb_id: string;
  media_type: string;
  seasons: string | null;
  status: string;
  quality: string | null;
  source: string | null;
  info_hash: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface LibraryDetail {
  request: RequestRow & { tmdb_id: number | null; arr_mirrored_at: string | null };
  items: { token: string; info_hash: string; strm_path: string | null; torbox_id: number | null; last_played: string | null;
    play_count: number; season: number | null; episode: number | null; debrid_provider: string | null; quality: string | null;
    torbox_account: number | null; torbox_account_label: string | null }[];
  playability: { content_key: string; status: string; last_ok_provider: string | null; last_ok_at: string | null;
    last_fail_reason: string | null; consecutive_failures: number; updated_at: string }[];
  episodes: { season: number; present: number; wanted: number }[] | null;
  monitored: { status: string; last_checked: string | null; seasons: string | null } | null;
  retry: { id: number; attempt: number; next_retry_at: string } | null;
  wanted_movie: { reason: string | null; attempts: number; last_checked: string | null } | null;
  user_requests: { id: number; username: string; status: string; reviewer: string | null; note: string | null; created_at: string; reviewed_at: string | null }[];
  seerr_request_id: number | null;
  override: { quality_preference: string | null; allow_4k: number | null; prefer_hevc: number | null; notes: string | null } | null;
  hashes: { info_hash: string; blacklisted: boolean; fail_count: number; last_error: string | null; current: boolean }[];
  activity: { id: number; event: string; title: string | null; message: string | null; success: number; created_at: string }[];
  arr: { mirrored_at: string | null };
  mirror_on: boolean;
}
export interface SeasonEpisode { season: number; episode: number; present: boolean; strm_path: string | null; token: string | null;
  wanted_status: string | null; attempt_count: number; air_date: string | null; last_attempted: string | null }

export interface Candidate {
  info_hash: string; name: string; quality: string | null; source: string | null; size_gb: number; seeders: number;
  languages: string[]; cached: boolean; scrapers: string[]; kept: boolean; rule: string | null; value: string | null; current: boolean;
  /** Season mode only: episode numbers the pack contains per TorBox's file list; null when unknown. */
  episodes?: number[] | null;
}
export interface CandidatesResponse { current: { info_hash: string; quality: string | null; source: string | null } | null; candidates: Candidate[] }
export interface SwapResult { ok: boolean; message: string; swapped?: number[]; registered?: number[]; skipped?: number[]; busy?: number[] }

export interface TorboxAccount {
  id: number; label: string; enabled: boolean; key_hint: string; items: number;
  health: { rate_limited_until: number | null; auth_failed_at: number | null; budget_left: number };
}

export interface GenreRule {
  media_type: 'movie' | 'tv';
  genre_id: number;
  genre_name: string;
  year_from: number | null;
  year_to: number | null;
  enabled: boolean;
}

// Image helpers  -  TMDB image CDN
export const tmdbImg = {
  poster: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w342${p}` : null),
  backdrop: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w1280${p}` : null),
  logo: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w92${p}` : null),
  profile: (p: string | null | undefined) => (p ? `https://image.tmdb.org/t/p/w185${p}` : null),
};

// Provider IDs (NL)  -  keep in sync with backend tmdb.NL_PROVIDERS
export const NL_PROVIDER_IDS = {
  netflix: 8,
  amazon_prime: 119,
  disney_plus: 337,
  hbo_max: 1899,
  apple_tv_plus: 350,
  videoland: 563,
  npo_plus: 271,
  skyshowtime: 1773,
} as const;
