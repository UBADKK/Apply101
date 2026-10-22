import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';

export interface Resource<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
  /** Replace the data, or update it from the latest value (never a stale copy). */
  setData: (value: T | ((current: T | undefined) => T)) => void;
}

/** Loads data when deps change; stale responses from earlier loads are ignored. */
export function useResource<T>(load: () => Promise<T>, deps: DependencyList): Resource<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    loadRef
      .current()
      .then((value) => {
        if (active) setData(value);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [...deps, version]);

  const reload = useCallback(() => setVersion((value) => value + 1), []);
  return { data, error, loading, reload, setData };
}
