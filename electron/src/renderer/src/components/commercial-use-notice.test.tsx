import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

import {
  COMMERCIAL_NOTICE_COOLDOWN_MS,
  COMMERCIAL_NOTICE_KEY,
  CommercialUseNotice,
  commercialNoticeDue,
} from './commercial-use-notice';

function clearAgreement() {
  document.cookie = `${COMMERCIAL_NOTICE_KEY}=; max-age=0; path=/`;
  window.localStorage.removeItem(COMMERCIAL_NOTICE_KEY);
}

beforeEach(clearAgreement);
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  clearAgreement();
});

it('blocks until the user agrees, ignoring Escape', async () => {
  render(<CommercialUseNotice />);
  expect(await screen.findByText('commercialNotice.title')).toBeTruthy();
  expect(screen.getByText('commercialNotice.body')).toBeTruthy();

  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  expect(screen.queryByText('commercialNotice.title')).toBeTruthy();

  fireEvent.click(screen.getByRole('button', { name: 'commercialNotice.agree' }));
  await vi.waitFor(() => expect(screen.queryByText('commercialNotice.title')).toBeNull());
});

it('stays hidden on the next page open within a day of agreeing', async () => {
  const first = render(<CommercialUseNotice />);
  fireEvent.click(await screen.findByRole('button', { name: 'commercialNotice.agree' }));
  first.unmount();
  expect(document.cookie).toContain(`${COMMERCIAL_NOTICE_KEY}=`);
  render(<CommercialUseNotice />);
  expect(screen.queryByText('commercialNotice.title')).toBeNull();
});

it('remembers agreement through localStorage when cookies do not persist', () => {
  const now = Date.now();
  window.localStorage.setItem(COMMERCIAL_NOTICE_KEY, String(now - 1000));
  expect(commercialNoticeDue(now)).toBe(false);
});

it('appears again once a day has passed', () => {
  const now = Date.now();
  window.localStorage.setItem(COMMERCIAL_NOTICE_KEY, String(now - COMMERCIAL_NOTICE_COOLDOWN_MS));
  expect(commercialNoticeDue(now)).toBe(true);
});

it('treats a missing, garbled or future timestamp as due', () => {
  const now = Date.now();
  expect(commercialNoticeDue(now)).toBe(true);
  window.localStorage.setItem(COMMERCIAL_NOTICE_KEY, 'garbage');
  expect(commercialNoticeDue(now)).toBe(true);
  window.localStorage.setItem(COMMERCIAL_NOTICE_KEY, String(now + 60_000));
  expect(commercialNoticeDue(now)).toBe(true);
});
