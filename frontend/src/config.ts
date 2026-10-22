/**
 * Runtime configuration. Only public values belong here: the build inlines
 * every VITE_* variable into the bundle, so never put secrets in them.
 */
const DEFAULT_API_BASE_URL = 'http://localhost:8001';

function normalizeBaseUrl(value: string | undefined): string {
  const trimmed = (value ?? '').trim();
  return (trimmed || DEFAULT_API_BASE_URL).replace(/\/+$/, '');
}

export const API_BASE_URL = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL);
