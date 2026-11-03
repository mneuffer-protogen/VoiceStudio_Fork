import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import '@/i18n';
import { formatDuration, QueuePage, type QueueRow, type QueueSnapshot } from './queue-page';

const api = vi.hoisted(() => ({ json: vi.fn() }));
vi.mock('@/lib/api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/client')>()),
  apiJson: api.json,
}));
vi.mock('@/components/app-shell/workspace-header', () => ({
  WorkspaceHeader: ({ children }: { children: React.ReactNode }) => <header>{children}</header>,
}));

const row = (patch: Partial<QueueRow>): QueueRow => ({
  id: 1,
  label: 'Generate speech',
  state: 'running',
  source: 'app',
  method: 'POST',
  path: '/generate',
  client: '1.2.3.4',
  agent: '',
  parent_id: null,
  created_at: 0,
  started_at: 0,
  finished_at: null,
  elapsed_s: 30,
  execution_s: 25,
  status: null,
  error: '',
  details: { engine: 'omnivoice', steps: 16, voice: 'narrator', chars: 420 },
  ...patch,
});

afterEach(() => {
  cleanup();
  api.json.mockReset();
});

it('formats durations', () => {
  expect(formatDuration(45)).toBe('45s');
  expect(formatDuration(185)).toBe('3m 05s');
  expect(formatDuration(3720)).toBe('1h 02m');
});

it('shows running and waiting work with its source and learned estimates', async () => {
  const snapshot: QueueSnapshot = {
    workers: 1,
    running: [row({ estimate: { seconds: 100, samples: 12, remaining_s: 75 } })],
    waiting: [
      row({
        id: 2,
        state: 'waiting',
        source: 'api',
        label: 'Speech (OpenAI API)',
        path: '/v1/audio/speech',
        position: 1,
        started_at: null,
        execution_s: null,
        estimate: { seconds: 40, samples: 0, starts_in_s: 75, remaining_s: 115 },
      }),
    ],
    recent: [row({ id: 3, state: 'failed', status: 500, finished_at: 1 })],
  };
  api.json.mockResolvedValue(snapshot);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <QueuePage />
    </QueryClientProvider>,
  );
  expect(await screen.findByText('Speech (OpenAI API)')).toBeTruthy();
  expect(api.json).toHaveBeenCalledWith('/queue');
  expect(screen.getByText('POST /v1/audio/speech')).toBeTruthy();
  expect(screen.getAllByText(/1m 15s/, { selector: 'span[title]' })).toHaveLength(2);
  expect(screen.getByText(/40s/, { selector: 'span[title]' })).toBeTruthy();
  expect(screen.getAllByText(/omnivoice/).length).toBeGreaterThan(0);
  expect(screen.getByText(/500/)).toBeTruthy();
});
