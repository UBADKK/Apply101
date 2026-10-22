import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { callsTo, fakeToken, installFakeFetch, USER } from '../test/fakeFetch';
import { PROFILE, renderApp } from '../test/renderApp';

const SIGNED_IN = {
  'GET /meta': { body: { analysis_provider: 'fake', demo: true } },
  'GET /auth/me': { body: USER },
  'GET /users/1/profiles': { body: [PROFILE] },
  'GET /users/1/languages': { body: [] },
  'GET /users/1/profiles/analyzed': { body: [] },
  'GET /users/1/profiles/1/matches': { body: { total_count: 0, returned_count: 0, results: [] } },
};

const JOB = {
  job_id: 3,
  title: 'Synthetic Backend Developer',
  company_name: 'Example GmbH',
  location: 'Berlin',
  url: 'https://example.com/jobs/synthetic',
  description_text: 'Synthetic posting with Python, FastAPI and SQL. Fluent English (C1) is required.',
};

beforeEach(() => {
  window.localStorage.setItem('apply101.accessToken', fakeToken(3600));
});

describe('new job form', () => {
  it('blocks a too short description before calling the API', async () => {
    const fetchMock = installFakeFetch(SIGNED_IN);
    const user = userEvent.setup();
    renderApp('/jobs/new');
    await user.type(await screen.findByLabelText(/Job title/), 'Synthetic role');
    await user.type(screen.getByLabelText(/Link to the posting/), 'https://example.com/jobs/1');
    await user.type(screen.getByLabelText(/Job description/), 'Too short.');
    await user.click(screen.getByRole('button', { name: 'Save job' }));
    expect(await screen.findByText(/at least 50 characters/)).toBeInTheDocument();
    expect(callsTo(fetchMock, 'POST /jobs/manual')).toBe(0);
  });

  it('shows a duplicate URL (409) next to the URL field', async () => {
    installFakeFetch({
      ...SIGNED_IN,
      'POST /jobs/manual': {
        status: 409,
        body: { detail: { error_code: 'ERR_JOB_URL_ALREADY_EXISTS', message: 'You already have a job with this URL.' } },
      },
    });
    const user = userEvent.setup();
    renderApp('/jobs/new');
    await user.type(await screen.findByLabelText(/Job title/), 'Synthetic role');
    await user.type(screen.getByLabelText(/Link to the posting/), 'https://example.com/jobs/1');
    await user.type(screen.getByLabelText(/Job description/), JOB.description_text);
    await user.click(screen.getByRole('button', { name: 'Save job' }));
    const urlField = screen.getByLabelText(/Link to the posting/);
    expect(await screen.findByText('You already have a job with this URL.')).toBeInTheDocument();
    expect(urlField).toHaveAttribute('aria-invalid', 'true');
  });
});

describe('job detail', () => {
  it('shows the same not-found page for another user’s job (404)', async () => {
    installFakeFetch({
      ...SIGNED_IN,
      'GET /jobs/9': { status: 404, body: { detail: 'Job not found.' } },
      'GET /jobs/analyzed/9': { status: 404, body: { detail: 'Job not found.' } },
    });
    renderApp('/jobs/9');
    expect(await screen.findByRole('heading', { name: 'Job not found' })).toBeInTheDocument();
  });

  it('respects Retry-After after a 429 and never re-sends the analysis by itself', async () => {
    const fetchMock = installFakeFetch({
      ...SIGNED_IN,
      'GET /jobs/3': { body: JOB },
      'GET /jobs/analyzed/3': { status: 404, body: { detail: 'No current completed analysis exists for this job.' } },
      'POST /jobs/3/analyze': {
        status: 429,
        body: { detail: 'Too many requests. Please try again later.' },
        headers: { 'Retry-After': '30' },
      },
    });
    const user = userEvent.setup();
    renderApp('/jobs/3');
    await user.click(await screen.findByRole('button', { name: 'Analyze job' }));
    expect(await screen.findByText(/You can try again in (30|29) seconds/)).toBeInTheDocument();
    const waiting = screen.getByRole('button', { name: /Try again in \d+s/ });
    expect(waiting).toBeDisabled();
    await user.click(waiting);
    expect(callsTo(fetchMock, 'POST /jobs/3/analyze')).toBe(1);
  });

  it('guides the user when the profile has no current analysis', async () => {
    installFakeFetch({
      ...SIGNED_IN,
      'GET /jobs/3': { body: JOB },
      'GET /jobs/analyzed/3': { status: 404, body: { detail: 'No current completed analysis exists for this job.' } },
    });
    renderApp('/jobs/3');
    const matchSection = (await screen.findByRole('heading', { name: 'Match with your profile' })).closest('section')!;
    expect(await within(matchSection).findByRole('link', { name: 'Analyze your profile' })).toHaveAttribute('href', '/profile');
    expect(within(matchSection).queryByRole('button', { name: 'Match with my profile' })).not.toBeInTheDocument();
  });
});
