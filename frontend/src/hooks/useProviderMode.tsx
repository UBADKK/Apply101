import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../api/endpoints';
import type { Meta } from '../api/types';

/**
 * Which analysis provider the backend uses, from GET /meta.
 * - openai: data from paid actions goes to OpenAI.
 * - fake: the deterministic demo provider; nothing goes to an AI provider.
 * - loading: no answer yet. Callers stay neutral: no provider name and no
 *   demo claim.
 * - unknown: the request failed, or an unrecognised or inconsistent answer.
 *   Callers must treat it like openai ("may be sent"), never like fake.
 */
export type ProviderMode = 'loading' | 'openai' | 'fake' | 'unknown';

export function providerModeFromMeta(meta: Meta | null | undefined): ProviderMode {
  if (meta?.analysis_provider === 'openai' && meta.demo === false) return 'openai';
  if (meta?.analysis_provider === 'fake' && meta.demo === true) return 'fake';
  return 'unknown';
}

// Without a provider (a component rendered on its own) nothing will answer:
// the cautious unknown, never loading forever.
const ProviderModeContext = createContext<ProviderMode>('unknown');

/** Requests /meta once for the whole app; the demo banner and every AI data
 * note read the answer from here. */
export function ProviderModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ProviderMode>('loading');
  // Kept across StrictMode's repeated effect run, so /meta is requested once.
  const request = useRef<Promise<Meta> | null>(null);

  useEffect(() => {
    let active = true;
    request.current ??= api.meta();
    request.current
      .then((meta) => {
        if (active) setMode(providerModeFromMeta(meta));
      })
      .catch(() => {
        // Not retried during the session: it stays unknown, the cautious
        // choice (the notes then assume the real provider).
        if (active) setMode('unknown');
      });
    return () => {
      active = false;
    };
  }, []);

  return <ProviderModeContext.Provider value={mode}>{children}</ProviderModeContext.Provider>;
}

export function useProviderMode(): ProviderMode {
  return useContext(ProviderModeContext);
}
