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

/**
 * Blocking notice shown every time the app opens: VoiceStudio is not for
 * commercial use. It has no close button and ignores Escape and outside
 * clicks, so the only way past it is the agree button. Agreement is held in
 * component state only, so the notice returns on the next page open.
 */
export function CommercialUseNotice() {
  const { t } = useTranslation();
  const [agreed, setAgreed] = useState(false);

  return (
    <Dialog open={!agreed} onOpenChange={() => {}}>
      <DialogContent showCloseButton={false} data-commercial-use-notice>
        <DialogHeader>
          <DialogTitle>{t('commercialNotice.title')}</DialogTitle>
          <DialogDescription>{t('commercialNotice.body')}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button autoFocus onClick={() => setAgreed(true)}>
            {t('commercialNotice.agree')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
