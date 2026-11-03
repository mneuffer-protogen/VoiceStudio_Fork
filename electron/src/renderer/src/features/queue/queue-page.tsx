import { useQuery } from '@tanstack/react-query';
import {
  AlertCircleIcon,
  CheckCircle2Icon,
  Clock3Icon,
  HourglassIcon,
  ListOrderedIcon,
  LoaderIcon,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { WorkspaceHeader } from '@/components/app-shell/workspace-header';
import { apiJson, describeError } from '@/lib/api/client';

export type QueueSource = 'app' | 'api' | 'mcp' | 'background';

export interface QueueEstimate {
  seconds: number;
  samples: number;
  remaining_s?: number;
  starts_in_s?: number;
}

export interface QueueRow {
  id: number;
  label: string;
  state: 'starting' | 'waiting' | 'running' | 'done' | 'failed';
  source: QueueSource;
  method: string;
  path: string;
  client: string;
  agent: string;
  parent_id: number | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  elapsed_s: number;
  execution_s: number | null;
  status: number | null;
  error: string;
  details: { engine?: string; steps?: number; voice?: string; chars?: number };
  position?: number;
  estimate?: QueueEstimate;
}

export interface QueueSnapshot {
  workers: number;
  running: QueueRow[];
  waiting: QueueRow[];
  recent: QueueRow[];
}

/** Seconds as "45s", "3m 05s" or "1h 02m". */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export function QueuePage() {
  const { t } = useTranslation();
  const queue = useQuery({
    queryKey: ['queue'],
    queryFn: () => apiJson<QueueSnapshot>('/queue'),
    refetchInterval: 2_000,
    refetchIntervalInBackground: false,
  });
  const data = queue.data;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <WorkspaceHeader>
        <h1 className="text-sm font-medium">{t('queue.title')}</h1>
        {data && (
          <span className="text-xs text-muted-foreground">
            {t('queue.summary', {
              running: data.running.length,
              waiting: data.waiting.length,
              workers: data.workers,
            })}
          </span>
        )}
      </WorkspaceHeader>
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-5">
        {queue.isError && (
          <p role="alert" className="text-sm text-destructive">
            {describeError(queue.error)}
          </p>
        )}
        {queue.isPending && <p className="text-sm text-muted-foreground">{t('queue.loading')}</p>}
        {data && (
          <>
            <QueueSection
              title={t('queue.running')}
              empty={t('queue.none_running')}
              rows={data.running}
            />
            <QueueSection
              title={t('queue.waiting')}
              empty={t('queue.none_waiting')}
              rows={data.waiting}
            />
            <QueueSection
              title={t('queue.recent')}
              empty={t('queue.none_recent')}
              rows={data.recent}
            />
          </>
        )}
      </div>
    </div>
  );
}

function QueueSection({ title, empty, rows }: { title: string; empty: string; rows: QueueRow[] }) {
  return (
    <section className="space-y-2">
      <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {title} <span className="tabular-nums">({rows.length})</span>
      </h2>
      {rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{empty}</p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row) => (
            <QueueItem key={row.id} row={row} />
          ))}
        </ul>
      )}
    </section>
  );
}

const STATE_ICON = {
  starting: LoaderIcon,
  waiting: HourglassIcon,
  running: LoaderIcon,
  done: CheckCircle2Icon,
  failed: AlertCircleIcon,
} as const;

function QueueItem({ row }: { row: QueueRow }) {
  const { t } = useTranslation();
  const Icon = STATE_ICON[row.state];
  const details = [
    row.details.engine,
    row.details.steps !== undefined ? t('queue.steps', { n: row.details.steps }) : null,
    row.details.voice ? t('queue.voice', { voice: row.details.voice }) : null,
    row.details.chars !== undefined ? t('queue.chars', { n: row.details.chars }) : null,
  ].filter(Boolean);
  return (
    <li className="rounded-xl border border-border/50 bg-card/20 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Icon
          aria-hidden="true"
          className={
            row.state === 'failed'
              ? 'size-4 text-destructive'
              : row.state === 'running'
                ? 'size-4 animate-spin text-primary'
                : 'size-4 text-muted-foreground'
          }
        />
        {row.position !== undefined && (
          <span className="inline-flex items-center gap-1 text-xs tabular-nums text-muted-foreground">
            <ListOrderedIcon className="size-3" />#{row.position}
          </span>
        )}
        <span className="font-medium">{row.label}</span>
        <span className="rounded-md border border-border/50 bg-muted/25 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {t(`queue.source_${row.source}`)}
        </span>
        <span className="sr-only">{t(`queue.state_${row.state}`)}</span>
        <span className="ms-auto inline-flex items-center gap-1 text-xs tabular-nums text-muted-foreground">
          <Clock3Icon className="size-3" />
          <QueueTiming row={row} />
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {row.method && (
          <span className="font-mono">
            {row.method} {row.path}
          </span>
        )}
        {row.client && <span>{t('queue.client', { client: row.client })}</span>}
        {details.length > 0 && <span>{details.join(' · ')}</span>}
        {row.error && <span className="text-destructive">{row.error}</span>}
        {row.status !== null && row.status >= 400 && (
          <span className="text-destructive">{t('queue.http_status', { status: row.status })}</span>
        )}
      </div>
      {row.agent && (
        <p className="mt-1 truncate text-[11px] text-muted-foreground/70" title={row.agent}>
          {row.agent}
        </p>
      )}
    </li>
  );
}

function QueueTiming({ row }: { row: QueueRow }) {
  const { t } = useTranslation();
  const estimate = row.estimate;
  const basis = estimate
    ? estimate.samples > 0
      ? t('queue.estimate_basis', { n: estimate.samples })
      : t('queue.estimate_first')
    : '';
  if (row.state === 'waiting' && estimate) {
    return (
      <span title={basis}>
        {t('queue.starts_in', { time: formatDuration(estimate.starts_in_s ?? 0) })} ·{' '}
        {t('queue.takes_about', { time: formatDuration(estimate.seconds) })}
      </span>
    );
  }
  if ((row.state === 'running' || row.state === 'starting') && estimate) {
    return (
      <span title={basis}>
        {t('queue.elapsed', { time: formatDuration(row.execution_s ?? row.elapsed_s) })} ·{' '}
        {t('queue.remaining', { time: formatDuration(estimate.remaining_s ?? 0) })}
      </span>
    );
  }
  return <span>{formatDuration(row.execution_s ?? row.elapsed_s)}</span>;
}
