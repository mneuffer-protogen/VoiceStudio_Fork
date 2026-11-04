import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useRouterState } from '@tanstack/react-router';
import { LibraryIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { SecondarySidebar } from '@/components/workspace-sidebar';
import { VoicesSidebar } from '@/features/clone/voices-sidebar';
import { setWorkspace, useWorkspace } from '@/lib/store/workspace';
import { LIBRARY_ROUTES } from './use-workspace-sidebar';

// On a phone-width window the library pane would leave the page a sliver, so it
// starts folded there and opens as an overlay (see .secondary-sidebar in
// globals.css) only when asked, without changing the desktop preference.
const PHONE_QUERY = '(max-width: 640px)';

function usePhoneViewport(): boolean {
  const supported = typeof window.matchMedia === 'function';
  return useSyncExternalStore(
    (notify) => {
      if (!supported) return () => {};
      const query = window.matchMedia(PHONE_QUERY);
      query.addEventListener('change', notify);
      return () => query.removeEventListener('change', notify);
    },
    () => supported && window.matchMedia(PHONE_QUERY).matches,
    () => false,
  );
}

/** The user's saved voices and recent takes, beside the slim navigation column. */
export function ContextSidebar() {
  const { t } = useTranslation();
  const { libraryOpen, libraryTab } = useWorkspace();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const phone = usePhoneViewport();
  const [phoneOpen, setPhoneOpen] = useState(false);
  // A library shortcut (or the desktop toggle) still opens and closes it on a phone.
  const lastLibraryOpen = useRef(libraryOpen);
  useEffect(() => {
    if (lastLibraryOpen.current === libraryOpen) return;
    lastLibraryOpen.current = libraryOpen;
    setPhoneOpen(libraryOpen);
  }, [libraryOpen]);
  if (!LIBRARY_ROUTES.has(pathname.replace(/\/+$/, '') || '/')) return null;
  return (
    <SecondarySidebar
      title={t('nav.voice')}
      icon={LibraryIcon}
      variant="library"
      className="flex flex-col overflow-hidden p-0"
      collapsed={phone ? !phoneOpen : !libraryOpen}
      onCollapsedChange={(collapsed) =>
        phone ? setPhoneOpen(!collapsed) : setWorkspace({ libraryOpen: !collapsed })
      }
    >
      <VoicesSidebar initialTab={libraryTab} />
    </SecondarySidebar>
  );
}
