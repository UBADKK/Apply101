import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { callsTo, fakeToken, installFakeFetch, USER } from '../test/fakeFetch';
import { renderApp } from '../test/renderApp';

const EMPTY_WORKSPACE = {
  'GET /meta': { body: { analysis_provider: 'fake', demo: true } },
  'GET /users/1/profiles': { body: [] },
  'GET /users/1/languages': { body: [] },
  'GET /users/1/profiles/analyzed': { body: [] },
  'GET /jobs/': { body: [] },
};

describe('sessions', () => {
  it('sends anonymous visitors of a protected page to the login page', async () => {
    installFakeFetch({ 'GET /meta': { body: { analysis_provider: 'openai', demo: false } } });
    renderApp('/jobs');
    expect(await screen.findByRole('heading', { name: 'Log in' })).toBeInTheDocument();
    expect(screen.queryByText(/Demo mode/)).not.toBeInTheDocument();
  });

  it('ends an expired stored session without calling the API, and says so', async () => {
    const fetchMock = installFakeFetch({ 'GET /meta': { body: { analysis_provider: 'fake', demo: true } } });
    window.localStorage.setItem('apply101.accessToken', fakeToken(-60));
    renderApp('/profile');
    expect(await screen.findByText('Your session has expired. Please log in again.')).toBeInTheDocument();
    expect(callsTo(fetchMock, 'GET /auth/me')).toBe(0);
    expect(window.localStorage.getItem('apply101.accessToken')).toBeNull();
  });

  it('ends the session when the server rejects the stored token (401)', async () => {
    installFakeFetch({
      'GET /meta': { body: { analysis_provider: 'fake', demo: true } },
      'GET /auth/me': { status: 401, body: { detail: 'Could not validate credentials' } },
    });
    window.localStorage.setItem('apply101.accessToken', fakeToken(3600));
    renderApp('/');
    expect(await screen.findByText('Your session has expired. Please log in again.')).toBeInTheDocument();
  });

  it('restores a stored valid session on reload', async () => {
    installFakeFetch({ ...EMPTY_WORKSPACE, 'GET /auth/me': { body: USER } });
    window.localStorage.setItem('apply101.accessToken', fakeToken(3600));
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Hello, Test User' })).toBeInTheDocument();
    expect(await screen.findByText(/Demo mode/)).toBeInTheDocument();
  });
});

describe('login', () => {
  it('logs in once even when the button is clicked repeatedly', async () => {
    let releaseLogin: () => void = () => undefined;
    const loginGate = new Promise<void>((resolve) => {
      releaseLogin = resolve;
    });
    const token = fakeToken(3600);
    const fetchMock = installFakeFetch({
      ...EMPTY_WORKSPACE,
      'POST /auth/login': () => ({ body: { access_token: token, token_type: 'bearer' } }),
      'GET /auth/me': { body: USER },
    });
    // Hold the login response so the second and third clicks happen while it is pending.
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes('/auth/login')) await loginGate;
      return original(input, init);
    });

    const user = userEvent.setup();
    renderApp('/login');
    await user.type(await screen.findByLabelText(/Email/), 'test.user@example.com');
    await user.type(screen.getByLabelText(/Password/), 'a-synthetic-password-123');
    const button = screen.getByRole('button', { name: 'Log in' });
    await user.click(button);
    await user.click(button);
    await user.click(button);
    expect(screen.getByRole('button', { name: 'Logging in…' })).toBeDisabled();
    releaseLogin();

    expect(await screen.findByRole('heading', { name: 'Hello, Test User' })).toBeInTheDocument();
    expect(callsTo(fetchMock, 'POST /auth/login')).toBe(1);
    expect(window.localStorage.getItem('apply101.accessToken')).toBe(token);
  });

  it('shows a clear message for a wrong password', async () => {
    installFakeFetch({
      'GET /meta': { body: { analysis_provider: 'fake', demo: true } },
      'POST /auth/login': { status: 401, body: { detail: 'Invalid email or password.' } },
    });
    const user = userEvent.setup();
    renderApp('/login');
    await user.type(await screen.findByLabelText(/Email/), 'test.user@example.com');
    await user.type(screen.getByLabelText(/Password/), 'a-wrong-password-1234');
    await user.click(screen.getByRole('button', { name: 'Log in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Log in' })).toBeEnabled());
  });
});
