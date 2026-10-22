import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { configureAuth, isApiError } from '../api/client';
import { api } from '../api/endpoints';
import type { CurrentUser } from '../api/types';
import { clearToken, isTokenExpired, readToken, tokenExpiresAt, writeToken } from './session';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous' | 'unreachable';
export type LogoutReason = 'manual' | 'expired';

interface AuthContextValue {
  status: AuthStatus;
  user: CurrentUser | null;
  /** One-off message for the login page (logged out, session expired). */
  notice: string | null;
  login: (mail: string, password: string) => Promise<void>;
  register: (name: string, mail: string, password: string) => Promise<void>;
  logout: (reason?: LogoutReason) => void;
  retry: () => void;
  clearNotice: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const NOTICES: Record<LogoutReason, string> = {
  manual: 'You have been logged out.',
  expired: 'Your session has expired. Please log in again.',
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const tokenRef = useRef<string | null>(readToken());
  const expiryTimer = useRef<number | null>(null);

  const clearExpiryTimer = () => {
    if (expiryTimer.current !== null) {
      window.clearTimeout(expiryTimer.current);
      expiryTimer.current = null;
    }
  };

  const logout = useCallback((reason: LogoutReason = 'manual') => {
    clearExpiryTimer();
    tokenRef.current = null;
    clearToken();
    setUser(null);
    setStatus('anonymous');
    setNotice(NOTICES[reason]);
  }, []);

  const scheduleExpiry = useCallback(
    (token: string) => {
      clearExpiryTimer();
      const expiresAt = tokenExpiresAt(token);
      if (expiresAt === null) return;
      // setTimeout cannot hold more than ~24.8 days; tokens are far shorter.
      const delay = Math.min(Math.max(expiresAt - Date.now(), 0), 2_000_000_000);
      expiryTimer.current = window.setTimeout(() => logout('expired'), delay);
    },
    [logout],
  );

  const loadUser = useCallback(async () => {
    const token = tokenRef.current;
    if (!token) {
      setStatus('anonymous');
      return;
    }
    if (isTokenExpired(token)) {
      logout('expired');
      return;
    }
    setStatus('loading');
    try {
      const me = await api.me();
      setUser(me);
      setStatus('authenticated');
      scheduleExpiry(token);
    } catch (error) {
      if (isApiError(error) && error.status === 401) {
        logout('expired');
      } else {
        setStatus('unreachable');
      }
    }
  }, [logout, scheduleExpiry]);

  useEffect(() => {
    configureAuth({
      getToken: () => tokenRef.current,
      onUnauthorized: () => logout('expired'),
    });
  }, [logout]);

  useEffect(() => {
    void loadUser();
    return clearExpiryTimer;
  }, [loadUser]);

  const login = useCallback(
    async (mail: string, password: string) => {
      const { access_token } = await api.login(mail, password);
      tokenRef.current = access_token;
      writeToken(access_token);
      setNotice(null);
      await loadUser();
    },
    [loadUser],
  );

  const register = useCallback(
    async (name: string, mail: string, password: string) => {
      await api.register(name, mail, password);
      await login(mail, password);
    },
    [login],
  );

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      notice,
      login,
      register,
      logout,
      retry: () => void loadUser(),
      clearNotice: () => setNotice(null),
    }),
    [status, user, notice, login, register, logout, loadUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}

/** For pages behind RequireAuth, where the user is always present. */
export function useCurrentUser(): CurrentUser {
  const { user } = useAuth();
  if (!user) throw new Error('useCurrentUser used outside an authenticated page');
  return user;
}
