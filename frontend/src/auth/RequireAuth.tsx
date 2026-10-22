import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Alert } from '../components/Alert';
import { LoadingState } from '../components/LoadingState';
import { useAuth } from './AuthContext';

export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, retry } = useAuth();
  const location = useLocation();

  if (status === 'loading') return <LoadingState label="Checking your session" />;
  if (status === 'unreachable') {
    return (
      <main className="page page-narrow" id="main">
        <Alert tone="error" title="Server unreachable">
          Cannot reach the Apply101 server. Check that the backend is running.
        </Alert>
        <button type="button" className="button" onClick={retry}>
          Try again
        </button>
      </main>
    );
  }
  if (status === 'anonymous') {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return <>{children}</>;
}

/** Login and register pages: send signed-in users onwards. */
export function PublicOnly({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? '/';
  if (status === 'loading') return <LoadingState label="Checking your session" />;
  if (status === 'authenticated') return <Navigate to={from} replace />;
  return <>{children}</>;
}
