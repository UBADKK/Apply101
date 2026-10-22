import { useCallback, useEffect, useRef, useState } from 'react';
import { isApiError } from '../api/client';

export interface Action<A extends unknown[], R> {
  /** Runs the action unless one is already running or a cooldown is active;
   * resolves to the result, or undefined when skipped or failed. */
  run: (...args: A) => Promise<R | undefined>;
  pending: boolean;
  error: unknown;
  /** Seconds left before the server allows another try (after a 429). */
  cooldownSeconds: number;
  reset: () => void;
}

/**
 * One user-triggered request at a time. A second click while it runs is
 * ignored (no double submit). A 429 starts a countdown from Retry-After;
 * the request is never re-sent automatically -- the user decides.
 */
export function useAction<A extends unknown[], R>(action: (...args: A) => Promise<R>): Action<A, R> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [cooldownUntil, setCooldownUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const running = useRef(false);
  const mounted = useRef(true);
  const actionRef = useRef(action);
  actionRef.current = action;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (cooldownUntil === null) return;
    const timer = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= cooldownUntil) {
        setCooldownUntil(null);
        window.clearInterval(timer);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [cooldownUntil]);

  const cooldownSeconds = cooldownUntil === null ? 0 : Math.max(0, Math.ceil((cooldownUntil - now) / 1000));

  const run = useCallback(
    async (...args: A) => {
      if (running.current) return undefined;
      if (cooldownUntil !== null && Date.now() < cooldownUntil) return undefined;
      running.current = true;
      setPending(true);
      setError(null);
      try {
        return await actionRef.current(...args);
      } catch (reason) {
        if (mounted.current) {
          setError(reason);
          if (isApiError(reason) && reason.status === 429 && reason.retryAfterSeconds) {
            const until = Date.now() + reason.retryAfterSeconds * 1000;
            setNow(Date.now());
            setCooldownUntil(until);
          }
        }
        return undefined;
      } finally {
        running.current = false;
        if (mounted.current) setPending(false);
      }
    },
    [cooldownUntil],
  );

  const reset = useCallback(() => setError(null), []);
  return { run, pending, error, cooldownSeconds, reset };
}
