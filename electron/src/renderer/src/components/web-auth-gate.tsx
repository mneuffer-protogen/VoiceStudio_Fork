import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { KeyRoundIcon, LogInIcon } from 'lucide-react';
import { exchangeApiKey } from '@shared/api/authSession';
import { absoluteApiBase, ApiError, apiFetch } from '@/lib/api/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';

type AuthMode = 'apikey' | 'pin';

// Without this guard a sign-in whose cookie cannot stick (plain http, blocked
// cookies) would bounce between the app and Microsoft forever.
const ENTRA_ATTEMPT_KEY = 'ov_entra_attempt';
const ENTRA_AUTO_RETRY_MS = 60_000;

function entraLoginUrl() {
  return `${absoluteApiBase()}/api/auth/entra/login`;
}

function recentEntraAttempt() {
  try {
    return Date.now() - Number(sessionStorage.getItem(ENTRA_ATTEMPT_KEY)) < ENTRA_AUTO_RETRY_MS;
  } catch {
    return false;
  }
}

function startEntraSignIn() {
  try {
    sessionStorage.setItem(ENTRA_ATTEMPT_KEY, String(Date.now()));
  } catch {
    // Storage blocked: the sign-in still works, only the loop guard is lost.
  }
  window.location.assign(entraLoginUrl());
}

export function WebAuthGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [checked, setChecked] = useState(!__WEB_DEPLOYMENT__);
  const [mode, setMode] = useState<AuthMode | null>(null);
  const [value, setValue] = useState('');
  const [pending, setPending] = useState(false);
  const [errorStatus, setErrorStatus] = useState<number | null | undefined>();
  const [entra, setEntra] = useState<boolean | null>(null);
  const submitting = useRef(false);
  const authError = __WEB_DEPLOYMENT__
    ? new URLSearchParams(window.location.search).get('auth_error')
    : null;

  useEffect(() => {
    if (!__WEB_DEPLOYMENT__) return;
    const required = (event: Event) => {
      const detail = (event as CustomEvent<{ mode?: string }>).detail;
      setMode(detail?.mode === 'apikey' ? 'apikey' : 'pin');
      setChecked(true);
      setValue('');
      setErrorStatus(undefined);
    };
    window.addEventListener('ov:auth-required', required);
    void apiFetch('/engines')
      .then(() => setChecked(true))
      .catch((error) => {
        if (!(error instanceof ApiError) || (error.status !== 401 && error.status !== 403)) {
          setChecked(true);
        }
      });
    return () => window.removeEventListener('ov:auth-required', required);
  }, []);

  useEffect(() => {
    if (mode !== 'apikey') return;
    const controller = new AbortController();
    fetch(`${absoluteApiBase()}/api/auth/config`, {
      credentials: 'include',
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((config) => setEntra(Boolean(config?.entra)))
      .catch(() => {
        if (!controller.signal.aborted) setEntra(false);
      });
    return () => controller.abort();
  }, [mode]);

  const autoSignIn = mode === 'apikey' && entra === true && !authError && !recentEntraAttempt();
  useEffect(() => {
    if (autoSignIn) startEntraSignIn();
  }, [autoSignIn]);

  if (!checked || (mode === 'apikey' && entra === null)) {
    return (
      <div className="flex h-full min-h-screen items-center justify-center bg-background text-sm text-muted-foreground">
        {t('preferences.loading')}
      </div>
    );
  }
  if (!mode) return <>{children}</>;

  if (entra) {
    return (
      <div
        className="flex h-full min-h-screen items-center justify-center bg-background p-6"
        role="dialog"
        aria-modal="true"
      >
        <Card className="w-full max-w-md">
          <CardHeader>
            <div className="mb-2 flex size-9 items-center justify-center rounded-md bg-primary/10 text-primary">
              <LogInIcon className="size-5" />
            </div>
            <CardTitle>{t('entraSignIn.title')}</CardTitle>
            <CardDescription>
              {t(autoSignIn ? 'entraSignIn.redirecting' : 'entraSignIn.description')}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {authError && (
              <p className="text-xs text-destructive" role="alert">
                {t(authError === 'denied' ? 'entraSignIn.denied' : 'entraSignIn.failed')}
              </p>
            )}
            <Button className="w-full" type="button" disabled={autoSignIn} onClick={startEntraSignIn}>
              {t('entraSignIn.button')}
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const credential = value.trim();
    if (!credential || submitting.current) return;
    submitting.current = true;
    setPending(true);
    setErrorStatus(undefined);
    try {
      if (mode === 'apikey') {
        setValue('');
        await exchangeApiKey(credential, { apiBase: absoluteApiBase() });
      } else {
        sessionStorage.setItem('ov_pin', credential);
      }
      window.location.reload();
    } catch (error) {
      setErrorStatus((error as { status?: number }).status ?? null);
    } finally {
      submitting.current = false;
      setPending(false);
    }
  };

  const apiKey = mode === 'apikey';
  return (
    <div
      className="flex h-full min-h-screen items-center justify-center bg-background p-6"
      role="dialog"
      aria-modal="true"
    >
      <Card className="w-full max-w-md">
        <CardHeader>
          <div className="mb-2 flex size-9 items-center justify-center rounded-md bg-primary/10 text-primary">
            <KeyRoundIcon className="size-5" />
          </div>
          <CardTitle>
            {t(apiKey ? 'settings.remote_backend_title' : 'network.share_on_network')}
          </CardTitle>
          <CardDescription>
            {t(apiKey ? 'settings.remote_backend_key_placeholder' : 'network.share_confirm_hint')}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-3" onSubmit={submit}>
            <label className="block space-y-1.5 text-xs font-medium" htmlFor="web-auth-credential">
              <span>{t(apiKey ? 'settings.remote_backend_key' : 'network.pin')}</span>
              <Input
                id="web-auth-credential"
                type={apiKey ? 'password' : 'text'}
                inputMode={apiKey ? undefined : 'numeric'}
                value={value}
                onChange={(event) => setValue(event.target.value)}
                autoComplete="off"
                disabled={pending}
                autoFocus
              />
            </label>
            {errorStatus !== undefined && (
              <p className="text-xs text-destructive" role="alert">
                {errorStatus
                  ? t('settings.remote_backend_error_http', { status: errorStatus })
                  : t('settings.remote_backend_error_network')}
              </p>
            )}
            <Button className="w-full" type="submit" disabled={pending || !value.trim()}>
              {t('network.connect')}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
