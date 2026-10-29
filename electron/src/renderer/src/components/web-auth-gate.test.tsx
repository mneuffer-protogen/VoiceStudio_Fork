import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@shared/api/authSession', () => ({ exchangeApiKey: vi.fn() }));
vi.mock('@/lib/api/client', () => ({
  absoluteApiBase: () => 'https://voice.test',
  apiFetch: vi.fn(() => new Promise(() => {})),
  ApiError: class ApiError extends Error {
    status = 0;
  },
}));

import { WebAuthGate } from './web-auth-gate';

const assign = vi.fn();

function requireApiKey() {
  act(() => {
    window.dispatchEvent(new CustomEvent('ov:auth-required', { detail: { mode: 'apikey' } }));
  });
}

function stubConfig(entra: boolean) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response(JSON.stringify({ entra }), { status: 200 })),
  );
}

beforeEach(() => {
  vi.stubGlobal('__WEB_DEPLOYMENT__', true);
  sessionStorage.clear();
  assign.mockReset();
  vi.stubGlobal('location', {
    ...window.location,
    search: '',
    assign,
    origin: 'https://voice.test',
    href: 'https://voice.test/',
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('redirects to Microsoft once when Entra is configured', async () => {
  stubConfig(true);
  render(<WebAuthGate>app</WebAuthGate>);
  requireApiKey();

  await waitFor(() =>
    expect(assign).toHaveBeenCalledWith('https://voice.test/api/auth/entra/login'),
  );
  expect(assign).toHaveBeenCalledTimes(1);
  expect(screen.queryByLabelText('settings.remote_backend_key')).toBeNull();
});

it('does not redirect again right after an attempt, and offers the button', async () => {
  stubConfig(true);
  sessionStorage.setItem('ov_entra_attempt', String(Date.now()));
  render(<WebAuthGate>app</WebAuthGate>);
  requireApiKey();

  expect(await screen.findByText('entraSignIn.button')).toBeTruthy();
  expect(assign).not.toHaveBeenCalled();
});

it('shows the localized failure and waits for a click instead of redirecting', async () => {
  stubConfig(true);
  vi.stubGlobal('location', { ...window.location, search: '?auth_error=denied', assign });
  render(<WebAuthGate>app</WebAuthGate>);
  requireApiKey();

  expect((await screen.findByRole('alert')).textContent).toBe('entraSignIn.denied');
  expect(assign).not.toHaveBeenCalled();
});

it('keeps the API-key form when Entra is not configured', async () => {
  stubConfig(false);
  render(<WebAuthGate>app</WebAuthGate>);
  requireApiKey();

  expect(await screen.findByLabelText('settings.remote_backend_key')).toBeTruthy();
  expect(assign).not.toHaveBeenCalled();
});
