import { vi } from 'vitest';

export interface FakeResponse {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}

type Handler = FakeResponse | ((request: { url: URL; init: RequestInit }) => FakeResponse);

/**
 * Replaces window.fetch with a router keyed by "METHOD /path" (no query).
 * Unknown routes fail loudly. Returns the mock so tests can count calls.
 */
export function installFakeFetch(routes: Record<string, Handler>) {
  const mock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.toString());
    const key = `${(init.method ?? 'GET').toUpperCase()} ${url.pathname}`;
    const handler = routes[key];
    if (!handler) throw new Error(`Unexpected request: ${key}`);
    const result = typeof handler === 'function' ? handler({ url, init }) : handler;
    const status = result.status ?? 200;
    return new Response(result.body === undefined ? null : JSON.stringify(result.body), {
      status,
      headers: { 'Content-Type': 'application/json', ...(result.headers ?? {}) },
    });
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

export function callsTo(mock: ReturnType<typeof installFakeFetch>, key: string): number {
  return mock.mock.calls.filter(([input, init]) => {
    const url = new URL(typeof input === 'string' ? input : input.toString());
    return `${((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase()} ${url.pathname}` === key;
  }).length;
}

/** A JWT-shaped token (unsigned; the client only reads exp). */
export function fakeToken(expSecondsFromNow = 3600): string {
  const payload = btoa(JSON.stringify({ sub: '1', exp: Math.floor(Date.now() / 1000) + expSecondsFromNow })).replace(/=+$/, '');
  return `e30.${payload}.sig`;
}

export const USER = { user_id: 1, name: 'Test User', mail: 'test.user@example.com', is_admin: false };
