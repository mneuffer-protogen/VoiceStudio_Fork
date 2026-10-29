import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

import { CommercialUseNotice } from './commercial-use-notice';

afterEach(cleanup);

it('blocks until the user agrees, ignoring Escape', async () => {
  render(<CommercialUseNotice />);
  expect(await screen.findByText('commercialNotice.title')).toBeTruthy();
  expect(screen.getByText('commercialNotice.body')).toBeTruthy();

  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
  expect(screen.queryByText('commercialNotice.title')).toBeTruthy();

  fireEvent.click(screen.getByRole('button', { name: 'commercialNotice.agree' }));
  await vi.waitFor(() => expect(screen.queryByText('commercialNotice.title')).toBeNull());
});

it('appears again on the next page open', async () => {
  const first = render(<CommercialUseNotice />);
  fireEvent.click(await screen.findByRole('button', { name: 'commercialNotice.agree' }));
  first.unmount();
  render(<CommercialUseNotice />);
  expect(await screen.findByText('commercialNotice.title')).toBeTruthy();
});
