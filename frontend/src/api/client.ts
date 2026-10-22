import { API_BASE_URL } from '../config';

export type FieldErrors = Record<string, string>;

interface ApiErrorInit {
  status: number;
  message: string;
  code?: string | null;
  hint?: string | null;
  detail?: unknown;
  retryAfterSeconds?: number | null;
  fieldErrors?: FieldErrors;
}

/** Any failed request. status 0 means the server could not be reached. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly hint: string | null;
  readonly detail: unknown;
  readonly retryAfterSeconds: number | null;
  readonly fieldErrors: FieldErrors;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code ?? null;
    this.hint = init.hint ?? null;
    this.detail = init.detail;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
    this.fieldErrors = init.fieldErrors ?? {};
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

let tokenGetter: () => string | null = () => null;
let unauthorizedHandler: (() => void) | null = null;

/** Wired up by AuthProvider: where the bearer token comes from, and what a
 * 401 on an authenticated request does (end the session). */
export function configureAuth(options: { getToken: () => string | null; onUnauthorized: () => void }): void {
  tokenGetter = options.getToken;
  unauthorizedHandler = options.onUnauthorized;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  json?: unknown;
  form?: Record<string, string>;
  body?: FormData;
  query?: Record<string, string | number | boolean | undefined>;
  auth?: boolean;
  signal?: AbortSignal;
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number.parseInt(value, 10);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  const date = Date.parse(value);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.ceil((date - Date.now()) / 1000));
}

function fieldName(loc: unknown): string | null {
  if (!Array.isArray(loc)) return null;
  const parts = loc.filter((part) => part !== 'body' && part !== 'query' && part !== 'path');
  const last = parts[parts.length - 1];
  return typeof last === 'string' || typeof last === 'number' ? String(last) : null;
}

function cleanValidationMessage(message: string): string {
  return message.replace(/^Value error, /, '');
}

async function toApiError(response: Response): Promise<ApiError> {
  const retryAfterSeconds = parseRetryAfter(response.headers.get('Retry-After'));
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const detail = body && typeof body === 'object' && 'detail' in body ? (body as { detail: unknown }).detail : body;
  const base = { status: response.status, detail, retryAfterSeconds };

  if (typeof detail === 'string') {
    return new ApiError({ ...base, message: detail });
  }
  if (Array.isArray(detail)) {
    const fieldErrors: FieldErrors = {};
    for (const item of detail) {
      if (!item || typeof item !== 'object') continue;
      const name = fieldName((item as { loc?: unknown }).loc) ?? 'form';
      const msg = (item as { msg?: unknown }).msg;
      if (typeof msg === 'string' && !fieldErrors[name]) fieldErrors[name] = cleanValidationMessage(msg);
    }
    return new ApiError({ ...base, message: 'Some fields need attention.', fieldErrors });
  }
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>;
    return new ApiError({
      ...base,
      message: typeof record.message === 'string' ? record.message : `Request failed (${response.status}).`,
      code: typeof record.error_code === 'string' ? record.error_code : null,
      hint: typeof record.hint === 'string' ? record.hint : null,
    });
  }
  return new ApiError({ ...base, message: `Request failed (${response.status}).` });
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', json, form, body, query, auth = true, signal } = options;
  const url = new URL(API_BASE_URL + path, window.location.origin);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) url.searchParams.set(key, String(value));
  }

  const headers: Record<string, string> = { Accept: 'application/json' };
  let payload: BodyInit | undefined;
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(json);
  } else if (form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded';
    payload = new URLSearchParams(form).toString();
  } else if (body) {
    payload = body;
  }
  const token = auth ? tokenGetter() : null;
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(url.toString(), { method, headers, body: payload, signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    // Also what an unexpected server error without CORS headers looks like.
    throw new ApiError({
      status: 0,
      message: 'Cannot reach the Apply101 server, or it failed to answer. Check that the backend is running and try again.',
    });
  }

  if (response.ok) {
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }
  const error = await toApiError(response);
  if (response.status === 401 && token) unauthorizedHandler?.();
  throw error;
}
