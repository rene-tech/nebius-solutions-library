import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  type FormEvent,
  type ReactNode,
  useContext,
  useEffect,
  useState,
} from "react";
import { adminApi, AdminApiError } from "../api/client";
import type { OperatorSession, SessionEnvelope } from "../api/accessTypes";

interface SessionContextValue {
  session: OperatorSession;
  logout: () => Promise<void>;
  loggingOut: boolean;
  logoutError: AdminApiError | null;
}

export const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside SessionBoundary");
  return value;
}

interface LoginProps {
  busy: boolean;
  error: AdminApiError | null;
  onLogin: (token: string, principalId?: string) => Promise<void>;
}

export function LoginPage({ busy, error, onLogin }: LoginProps) {
  const [token, setToken] = useState("");
  const [principalId, setPrincipalId] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const transientToken = token.trim();
    const selectedPrincipal = principalId.trim() || undefined;
    setToken("");
    if (!transientToken) return;
    await onLogin(transientToken, selectedPrincipal);
  }

  return (
    <main className="login-shell">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-wordmark">
          <span className="wordmark__mark" aria-hidden="true">F2</span>
          <span>FS2 Serve</span>
        </div>
        <span className="eyebrow">Inference platform administration</span>
        <h1 id="login-title">Operator sign in</h1>
        <p>
          Exchange a bootstrap credential for a short-lived, same-origin operator session.
          The credential is cleared from this form immediately and is never saved by the console.
        </p>
        <form className="form-stack" onSubmit={(event) => void submit(event)}>
          <label>
            Bootstrap access token
            <input
              autoComplete="off"
              autoFocus
              disabled={busy}
              name="bootstrap-token"
              onChange={(event) => setToken(event.target.value)}
              required
              spellCheck={false}
              type="password"
              value={token}
            />
          </label>
          <details className="advanced-field">
            <summary>Use a specific operator identity</summary>
            <label>
              Principal UUID
              <input
                autoComplete="off"
                disabled={busy}
                name="principal-id"
                onChange={(event) => setPrincipalId(event.target.value)}
                pattern="[0-9a-fA-F-]{36}"
                placeholder="Defaults to bootstrap administrator"
                spellCheck={false}
                value={principalId}
              />
            </label>
          </details>
          {error ? (
            <div className="inline-notice inline-notice--error" role="alert">
              <strong>Sign in failed.</strong> {error.message}
              {error.requestId ? <span> Request {error.requestId}</span> : null}
            </div>
          ) : null}
          <button className="button button--primary" disabled={busy} type="submit">
            {busy ? "Creating session…" : "Sign in"}
          </button>
        </form>
        <p className="security-note">Session cookie: Secure · HttpOnly · SameSite=Strict</p>
      </section>
    </main>
  );
}

function SessionLoading() {
  return (
    <main className="login-shell">
      <div className="state-panel state-panel--loading" role="status">Checking operator session…</div>
    </main>
  );
}

function SessionFailure({ error, retry }: { error: AdminApiError; retry: () => void }) {
  return (
    <main className="login-shell">
      <div className="state-panel state-panel--error" role="alert">
        <strong>Session service is unavailable</strong>
        <span>{error.message}</span>
        {error.requestId ? <code>Request {error.requestId}</code> : null}
        <button className="button" onClick={retry} type="button">Try again</button>
      </div>
    </main>
  );
}

export function SessionBoundary({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [override, setOverride] = useState<SessionEnvelope | null | undefined>(undefined);
  const [loginError, setLoginError] = useState<AdminApiError | null>(null);
  const [authenticating, setAuthenticating] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<AdminApiError | null>(null);
  const sessionQuery = useQuery({
    queryKey: ["admin-session"],
    queryFn: ({ signal }) => adminApi.session(signal),
    retry: (count, error) => !(error instanceof AdminApiError && error.status === 401) && count < 1,
    staleTime: 30_000,
  });

  useEffect(() => {
    function expire() {
      setOverride(null);
      queryClient.clear();
    }
    window.addEventListener("fs2:operator-session-expired", expire);
    return () => window.removeEventListener("fs2:operator-session-expired", expire);
  }, [queryClient]);

  async function login(token: string, principalId?: string) {
    setAuthenticating(true);
    setLoginError(null);
    try {
      const session = await adminApi.createSession(token, principalId);
      queryClient.clear();
      queryClient.setQueryData(["admin-session"], session);
      setOverride(session);
    } catch (caught) {
      setLoginError(
        caught instanceof AdminApiError
          ? caught
          : new AdminApiError("Unable to create an operator session", 503, null),
      );
      setOverride(null);
    } finally {
      setAuthenticating(false);
    }
  }

  async function logout() {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await adminApi.deleteSession();
      setOverride(null);
      queryClient.clear();
    } catch (caught) {
      setLogoutError(
        caught instanceof AdminApiError
          ? caught
          : new AdminApiError("Unable to close the operator session", 503, null),
      );
    } finally {
      setLoggingOut(false);
    }
  }

  const session = override === undefined ? sessionQuery.data : override;
  const unauthorized =
    override === null ||
    (sessionQuery.error instanceof AdminApiError && sessionQuery.error.status === 401);

  if (sessionQuery.isPending && override === undefined) return <SessionLoading />;
  if (unauthorized) {
    return <LoginPage busy={authenticating} error={loginError} onLogin={login} />;
  }
  if (!session) {
    const error =
      sessionQuery.error instanceof AdminApiError
        ? sessionQuery.error
        : new AdminApiError("Unable to read the operator session", 503, null);
    return <SessionFailure error={error} retry={() => void sessionQuery.refetch()} />;
  }

  return (
    <SessionContext.Provider value={{ session: session.data, logout, loggingOut, logoutError }}>
      {children}
    </SessionContext.Provider>
  );
}
