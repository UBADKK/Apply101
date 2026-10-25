import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { callsTo, fakeToken, installFakeFetch, USER } from '../test/fakeFetch';
import { PROFILE, renderApp } from '../test/renderApp';

const BASE = {
  'GET /meta': { body: { analysis_provider: 'fake', demo: true } },
  'GET /auth/me': { body: USER },
  'GET /users/1/profiles': { body: [PROFILE] },
  'GET /users/1/languages': { body: [] },
  'GET /catalog/filters': {
    body: { sources: [{ source: 'greenhouse', label: 'Greenhouse Job Board', count: 2 }], countries: [{ country_code: 'DE', count: 2 }], work_modes: ['remote', 'hybrid', 'onsite'] },
  },
};

const CARD = {
  company_name: 'Acme',
  company_id: 1,
  location: 'Berlin, Germany',
  country_code: 'DE',
  source: 'greenhouse',
  source_label: 'Greenhouse Job Board',
  attribution: 'Job listing published on the company’s Greenhouse job board; apply on the original posting.',
  first_seen_at: '2026-10-25T12:00:00',
  last_seen_at: '2026-10-25T12:00:00',
  source_created_at: null,
};

const ANALYZED = { 'GET /users/1/profiles/analyzed': { body: [{ profile_id: 1, analysis: { analysis_id: 11, analysis_prompt_version: 'profile_analysis_v5' } }] } };

beforeEach(() => {
  window.localStorage.setItem('apply101.accessToken', fakeToken(3600));
});

describe('job pool', () => {
  it('lists open jobs with source link and applies filters on submit', async () => {
    const queries: string[] = [];
    installFakeFetch({
      ...BASE,
      'GET /users/1/profiles/analyzed': { body: [] },
      'GET /catalog/jobs': ({ url }) => {
        queries.push(url.search);
        return {
          body: {
            total: 1,
            page: 1,
            page_size: 20,
            items: [{ ...CARD, job_id: 5, title: 'Backend Developer', url: 'https://job-boards.greenhouse.io/acme/jobs/1', work_mode: 'remote', remote_note: 'Remote does not say from which countries you may work.', has_current_analysis: false }],
          },
        };
      },
    });
    const user = userEvent.setup();
    renderApp('/catalog');
    expect(await screen.findByRole('link', { name: 'Backend Developer' })).toHaveAttribute('href', '/jobs/5');
    const original = screen.getByRole('link', { name: /Original posting/ });
    expect(original).toHaveAttribute('href', 'https://job-boards.greenhouse.io/acme/jobs/1');
    expect(original).toHaveAttribute('rel', expect.stringContaining('noopener'));
    expect(screen.getByText(/from which countries/)).toBeInTheDocument();
    expect(screen.getByText(/Not analyzed yet/)).toBeInTheDocument();
    await user.type(screen.getByLabelText('Search'), 'python');
    expect(queries).toHaveLength(1);
    await user.selectOptions(screen.getByLabelText('Work mode'), 'remote');
    await user.click(screen.getByRole('button', { name: 'Apply' }));
    await screen.findByRole('link', { name: 'Backend Developer' });
    expect(queries.at(-1)).toContain('q=python');
    expect(queries.at(-1)).toContain('work_mode=remote');
  });
});

describe('jobs for me', () => {
  it('asks for a profile analysis first and calls nothing else', async () => {
    const fetchMock = installFakeFetch({ ...BASE, 'GET /users/1/profiles/analyzed': { body: [] } });
    renderApp('/for-you');
    expect(await screen.findByText('Analyze your profile first')).toBeInTheDocument();
    expect(callsTo(fetchMock, 'GET /users/1/profiles/1/recommendations')).toBe(0);
  });

  it('shows the coverage and ranked results with reasons', async () => {
    installFakeFetch({
      ...BASE,
      ...ANALYZED,
      'GET /users/1/profiles/1/recommendations': {
        body: {
          match_version: 'backend_match_v6',
          coverage: { active_jobs: 40, analyzed_jobs: 12, evaluated_jobs: 12, not_analyzed_jobs: 28, pool_limit: 1000, capped: false, status_counts: { eligible: 1, review_required: 1, ineligible: 10 } },
          include_ineligible: false,
          total: 2,
          page: 1,
          page_size: 20,
          items: [
            { ...CARD, job_id: 5, title: 'Backend Developer', url: 'https://x.test/5', work_mode: null, remote_note: null, eligibility_status: 'eligible', overall_score: 74, recommendation: 'apply', summary: 'Candidate is eligible.', strengths: ['Skills match.'], weaknesses: [], hard_fail_reasons: [], review_reasons: [], dealbreaker_warnings: [] },
            { ...CARD, job_id: 6, title: 'Data Engineer', url: 'https://x.test/6', work_mode: null, remote_note: null, eligibility_status: 'review_required', overall_score: 57, recommendation: 'manual_review', summary: 'Manual review is required.', strengths: [], weaknesses: [], hard_fail_reasons: [], review_reasons: [{ code: 'work_authorization_unknown', message: 'Work authorization is not known.' }], dealbreaker_warnings: [] },
          ],
        },
      },
    });
    renderApp('/for-you');
    expect(await screen.findByText(/Ranked 12 of 40 open jobs/)).toBeInTheDocument();
    expect(screen.getByText(/28 are not analyzed yet and are not ranked, so this is not the best of the whole pool/)).toBeInTheDocument();
    const items = screen.getAllByRole('listitem').filter((item) => within(item).queryByRole('heading'));
    expect(within(items[0]).getByText('Backend Developer')).toBeInTheDocument();
    expect(within(items[1]).getByText(/Work authorization is not known/)).toBeInTheDocument();
  });
});

describe('import from a link', () => {
  it('fills the form from the preview and saves only on submit', async () => {
    const fetchMock = installFakeFetch({
      ...BASE,
      'GET /users/1/profiles/analyzed': { body: [] },
      'POST /jobs/import/preview': {
        body: {
          status: 'ok',
          source: 'lever',
          url: 'https://jobs.lever.co/acme/1',
          fields: { title: 'Frontend Engineer', company_name: null, location: 'Istanbul', description_text: 'Work on our React app with TypeScript, testing and accessibility in a small team.' },
          work_mode: 'hybrid',
          missing: ['company_name'],
          notes: ['The job board does not name the company in the posting; add it yourself.'],
        },
      },
    });
    const user = userEvent.setup();
    renderApp('/jobs/new');
    await user.type(await screen.findByLabelText('Job link'), 'https://jobs.lever.co/acme/1');
    await user.click(screen.getByRole('button', { name: 'Read posting' }));
    expect(await screen.findByText('Posting read: check the fields below')).toBeInTheDocument();
    expect(screen.getByText(/add it yourself/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Job title/)).toHaveValue('Frontend Engineer');
    expect(screen.getByLabelText(/Link to the posting/)).toHaveValue('https://jobs.lever.co/acme/1');
    expect(screen.getByLabelText(/^Company/)).toHaveValue('');
    expect(callsTo(fetchMock, 'POST /jobs/manual')).toBe(0);
  });

  it('falls back to pasting the text when the page cannot be read', async () => {
    installFakeFetch({
      ...BASE,
      'GET /users/1/profiles/analyzed': { body: [] },
      'POST /jobs/import/preview': {
        status: 422,
        body: { detail: { error_code: 'ERR_IMPORT_SITE_UNSUPPORTED', message: 'This site cannot be imported. Paste the job text instead.', fallback: 'paste_text' } },
      },
    });
    const user = userEvent.setup();
    renderApp('/jobs/new');
    await user.type(await screen.findByLabelText('Job link'), 'https://www.linkedin.com/jobs/view/1');
    await user.click(screen.getByRole('button', { name: 'Read posting' }));
    expect(await screen.findByText(/Paste the job title, link and text in the form below instead/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Job title/)).toHaveValue('');
  });
});
