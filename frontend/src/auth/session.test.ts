import { describe, expect, it } from 'vitest';
import { fakeToken } from '../test/fakeFetch';
import { isTokenExpired, tokenExpiresAt } from './session';

describe('token expiry', () => {
  it('reads exp from the JWT payload', () => {
    const token = fakeToken(60);
    const expiresAt = tokenExpiresAt(token);
    expect(expiresAt).not.toBeNull();
    expect(expiresAt! - Date.now()).toBeGreaterThan(50_000);
    expect(isTokenExpired(token)).toBe(false);
  });

  it('treats a past exp as expired and an unreadable token as unknown', () => {
    expect(isTokenExpired(fakeToken(-10))).toBe(true);
    expect(tokenExpiresAt('not-a-jwt')).toBeNull();
    expect(isTokenExpired('not-a-jwt')).toBe(false);
  });
});
