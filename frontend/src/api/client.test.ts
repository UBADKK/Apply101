import { describe, expect, it, vi } from 'vitest';
import { ApiError, apiRequest, configureAuth } from './client';
import { describeError } from './errors';
import { installFakeFetch } from '../test/fakeFetch';

describe('apiRequest errors', () => {
  it('maps a 422 validation list to field errors', async () => {
    installFakeFetch({
      'POST /jobs/manual': {
        status: 422,
        body: { detail: [{ loc: ['body', 'url'], msg: 'Value error, url must be an http or https URL.' }] },
      },
    });
    const error = await apiRequest('/jobs/manual', { method: 'POST', json: {} }).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).fieldErrors).toEqual({ url: 'url must be an http or https URL.' });
  });

  it('reads Retry-After on 429 and the error code and hint of structured details', async () => {
    installFakeFetch({
      'POST /jobs/1/analyze': { status: 429, body: { detail: 'Too many requests. Please try again later.' }, headers: { 'Retry-After': '42' } },
      'POST /users/1/profiles/1/jobs/1/match': {
        status: 400,
        body: { detail: { error_code: 'ERR_JOB_ANALYSIS_OUTDATED_OR_MISSING', message: 'Job must be analyzed.', hint: 'Run analyze.' } },
      },
    });
    const limited = (await apiRequest('/jobs/1/analyze', { method: 'POST' }).catch((reason: unknown) => reason)) as ApiError;
    expect(limited.retryAfterSeconds).toBe(42);
    expect(describeError(limited).message).toContain('42 seconds');

    const outdated = (await apiRequest('/users/1/profiles/1/jobs/1/match', { method: 'POST' }).catch((reason: unknown) => reason)) as ApiError;
    expect(outdated.code).toBe('ERR_JOB_ANALYSIS_OUTDATED_OR_MISSING');
    expect(outdated.hint).toBe('Run analyze.');
    expect(describeError(outdated).title).toBe('Analysis needed');
  });

  it('calls the unauthorized handler only for authenticated requests', async () => {
    const onUnauthorized = vi.fn();
    configureAuth({ getToken: () => 'token', onUnauthorized });
    installFakeFetch({ 'GET /auth/me': { status: 401, body: { detail: 'Could not validate credentials' } } });
    await expect(apiRequest('/auth/me')).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);

    installFakeFetch({ 'POST /auth/login': { status: 401, body: { detail: 'Invalid email or password.' } } });
    await expect(apiRequest('/auth/login', { method: 'POST', auth: false, form: {} })).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    configureAuth({ getToken: () => null, onUnauthorized: () => undefined });
  });

  it('reports an unreachable server as status 0', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));
    const error = (await apiRequest('/meta', { auth: false }).catch((reason: unknown) => reason)) as ApiError;
    expect(error.status).toBe(0);
    expect(describeError(error).title).toBe('Server unreachable');
  });

  it('describes 401, 404 and 503 for people, not developers', () => {
    expect(describeError(new ApiError({ status: 401, message: 'x' }), 'login').message).toBe('Invalid email or password.');
    expect(describeError(new ApiError({ status: 401, message: 'x' })).message).toMatch(/session has expired/);
    expect(describeError(new ApiError({ status: 404, message: 'Job not found.' })).title).toBe('Not found');
    const unavailable = describeError(new ApiError({ status: 503, message: 'Analysis service is not configured.' }));
    expect(unavailable.title).toBe('Service unavailable');
    expect(unavailable.message).toContain('Nothing was saved');
  });
});
