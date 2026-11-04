import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

/** How long one agreement lasts before the notice is shown again. */
export const COMMERCIAL_NOTICE_COOLDOWN_MS = 24 * 60 * 60 * 1000;
export const COMMERCIAL_NOTICE_KEY = 'vs_commercial_notice_agreed_at';

/**
 * Agreement time from the cookie, or from localStorage where cookies do not
 * persist (the packaged Electron app loads from file://). Unreadable storage
 * counts as "never agreed", so the notice shows rather than being skipped.
 */
function readAgreedAt(): number | null {
  let raw: string | null = null;
  try {
    const match = document.cookie
      .split('; ')
      .find((part) => part.startsWith(`${COMMERCIAL_NOTICE_KEY}=`));
    raw = match ? decodeURIComponent(match.slice(COMMERCIAL_NOTICE_KEY.length + 1)) : null;
  } catch {
    raw = null;
  }
  if (raw === null) {
    try {
      raw = window.localStorage.getItem(COMMERCIAL_NOTICE_KEY);
    } catch {
      raw = null;
    }
  }
  const value = raw === null ? Number.NaN : Number(raw);
  return Number.isFinite(value) ? value : null;
}

function writeAgreedAt(now: number): void {
  const maxAge = Math.floor(COMMERCIAL_NOTICE_COOLDOWN_MS / 1000);
  try {
    document.cookie = `${COMMERCIAL_NOTICE_KEY}=${now}; max-age=${maxAge}; path=/; SameSite=Lax`;
  } catch {
    // Cookies blocked: localStorage below still records the agreement.
  }
  try {
    window.localStorage.setItem(COMMERCIAL_NOTICE_KEY, String(now));
  } catch {
    // Storage blocked: the notice shows again next time, which is safe.
  }
}

export function commercialNoticeDue(now: number = Date.now()): boolean {
  const agreedAt = readAgreedAt();
  // A future timestamp (clock moved back) is treated as stale.
  return agreedAt === null || agreedAt > now || now - agreedAt >= COMMERCIAL_NOTICE_COOLDOWN_MS;
}

/**
 * Blocking notice: VoiceStudio is not for commercial use. It has no close
 * button and ignores Escape and outside clicks, so the only way past it is the
 * agree button. Agreement is remembered for one day (cookie, with a
 * localStorage fallback), after which the notice appears again on open.
 */
export function CommercialUseNotice() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(() => commercialNoticeDue());

  const agree = () => {
    writeAgreedAt(Date.now());
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={() => {}}>
      <DialogContent showCloseButton={false} data-commercial-use-notice>
        <DialogHeader>
          <DialogTitle>{t('commercialNotice.title')}</DialogTitle>
          <DialogDescription>{t('commercialNotice.body')}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button autoFocus onClick={agree}>
            {t('commercialNotice.agree')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
