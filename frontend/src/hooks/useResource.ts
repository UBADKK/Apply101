import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';

export interface Resource<T> {
  data: T | undefined;
  error: unknown;
  /** True while a load runs -- and already in the render where the deps
   * changed but the new load has not started yet, so pages never treat the
   * previous deps' data as the answer for the new ones. */
  loading: boolean;
  reload: () => void;
  /** Replace the data, or update it from the latest value (never a stale copy). */
  setData: (value: T | ((current: T | undefined) => T)) => void;
}

function sameDeps(a: DependencyList, b: DependencyList): boolean {
  return a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
}

/** Loads data when deps change; stale responses from earlier loads are ignored. */
export function useResource<T>(load: () => Promise<T>, deps: DependencyList): Resource<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [version, setVersion] = useState(0);
  const [loadedDeps, setLoadedDeps] = useState<DependencyList | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    let active = true;
    setLoadedDeps(deps);
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
  const depsPending = loadedDeps === null || !sameDeps(loadedDeps, deps);
  return { data, error, loading: loading || depsPending, reload, setData };
}
