import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { MatchData, ProfileInput, SavedMatch } from '../api/types';
import { fakeToken, installFakeFetch, USER } from '../test/fakeFetch';
import { PROFILE, renderApp } from '../test/renderApp';
import { ProfileForm } from './profile/ProfileForm';

const MATCH_DATA: MatchData = {
  match_version: 'backend_match_v6',
  eligibility_status: 'ineligible',
  eligible: false,
  hard_fail_reasons: [{ code: 'required_language_missing', message: 'Required language is missing: german.' }],
  review_reasons: [],
  role_fit_score: 28,
  skills_fit_score: 79,
  experience_fit_score: 80,
  seniority_fit_score: 70,
  location_fit_score: 45,
  work_type_fit_score: 60,
  employment_type_fit_score: 80,
  language_fit_score: 0,
  visa_fit_score: 100,
  overall_score: 20,
  recommendation: 'ineligible',
  summary: 'Candidate is ineligible due to 1 confirmed hard requirement issue(s).',
  strengths: [],
  weaknesses: [],
  dealbreaker_warnings: [],
};

function saved(overrides: Partial<SavedMatch>): SavedMatch {
  return {
    match_id: 1,
    profile_id: 1,
    job_id: 7,
    profile_analysis_id: 11,
    job_analysis_id: 21,
    title: 'Synthetic German team role',
    company_name: 'Example GmbH',
    location: 'Munich',
    url: 'https://example.com/jobs/7',
    match_version: 'backend_match_v6',
    eligibility_status: 'ineligible',
    overall_score: 20,
    recommendation: 'ineligible',
    summary: MATCH_DATA.summary,
    matched_at: '2026-10-22T20:00:00',
    match: MATCH_DATA,
    ...overrides,
  };
}

const ANALYZED_JOB_7 = {
  job_id: 7,
  title: 'Synthetic German team role',
  analysis_id: 21,
  analysis_status: 'completed',
  analysis_model: 'gpt-4.1-mini',
  analysis_prompt_version: 'job_analysis_v13',
  analyzed_at: '2026-10-22T19:00:00',
  normalized_role_title: 'Synthetic German team role',
  role_family: 'other',
  seniority_level: 'unknown',
  required_skills: [],
  preferred_skills: [],
  language_requirements: [],
  visa_sponsorship: 'unknown',
  work_type: 'unknown',
  employment_type: 'unknown',
  dealbreakers: [],
  analysis: {},
};

function signedIn(profileAnalysisId: number) {
  return {
    'GET /meta': { body: { analysis_provider: 'fake', demo: true } },
    'GET /auth/me': { body: USER },
    'GET /users/1/profiles': { body: [PROFILE] },
    'GET /users/1/languages': { body: [] },
    'GET /users/1/profiles/analyzed': {
      body: [{ profile_id: 1, analysis: { analysis_id: profileAnalysisId, analysis_prompt_version: 'profile_analysis_v5' } }],
    },
    'GET /jobs/analyzed/7': { body: ANALYZED_JOB_7 },
  };
}

beforeEach(() => {
  window.localStorage.setItem('apply101.accessToken', fakeToken(3600));
});

describe('profile form keeps stored values it does not offer', () => {
  it('saves an unknown language level as null and keeps names, scales and free-text choices', async () => {
    const onSave = vi.fn(async (_input: ProfileInput) => undefined);
    render(
      <ProfileForm
        profile={{ ...PROFILE, preferred_work_type: 'remote, full time', seniority_target: 'principal' }}
        languages={[
          { language_code: 'hu', language_name: 'Hungarian', proficiency_level: null, proficiency_scale: 'ILR', is_primary: true },
          { language_code: 'en', language_name: 'English', proficiency_level: 'C1', proficiency_scale: 'CEFR', is_primary: false },
        ]}
        onSave={onSave}
        pending={false}
        error={null}
      />,
    );
    await userEvent.setup().click(screen.getByRole('button', { name: 'Save changes' }));
    const input = onSave.mock.calls[0][0];
    expect(input.preferred_work_type).toBe('remote, full time');
    expect(input.seniority_target).toBe('principal');
    expect(input.languages).toEqual([
      { language_code: 'hu', language_name: 'Hungarian', proficiency_level: null, proficiency_scale: 'ILR', is_primary: true },
      { language_code: 'en', language_name: 'English', proficiency_level: 'C1', proficiency_scale: 'CEFR', is_primary: false },
    ]);
    expect(screen.getByLabelText('Work type')).toHaveDisplayValue('remote, full time (current value)');
    expect(screen.getByLabelText('Language 1')).toHaveDisplayValue('Hungarian');
  });
});

describe('saved match detail', () => {
  it('shows the new result after a rematch without a "not found" flash', async () => {
    let listCalls = 0;
    installFakeFetch({
      ...signedIn(11),
      'GET /users/1/profiles/1/matches': () => {
        listCalls += 1;
        const results = listCalls === 1 ? [saved({})] : [saved({ match_id: 2 })];
        return { body: { total_count: 1, returned_count: 1, results } };
      },
      'POST /users/1/profiles/1/jobs/7/match': {
        body: {
          status: 'created',
          match_id: 2,
          profile_id: 1,
          job_id: 7,
          profile_analysis_id: 11,
          job_analysis_id: 21,
          match_version: 'backend_match_v6',
          eligibility_status: 'ineligible',
          overall_score: 20,
          recommendation: 'ineligible',
          match: MATCH_DATA,
        },
      },
    });
    renderApp('/matches/1');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Re-run match' }));
    expect(await screen.findByText('Match re-run with your current profile and job analyses.')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Match not found' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Synthetic German team role', level: 1 })).toBeInTheDocument();
  });

  it('warns when the saved result used an older profile analysis', async () => {
    installFakeFetch({
      ...signedIn(12),
      'GET /users/1/profiles/1/matches': { body: { total_count: 1, returned_count: 1, results: [saved({})] } },
    });
    renderApp('/matches/1');
    expect(await screen.findByText('Result may be outdated')).toBeInTheDocument();
    expect(screen.getByText(/older analysis of your profile\./)).toBeInTheDocument();
  });

  it('never renders "Match not found" while the match is opening', async () => {
    // React may only change the text of the existing heading, so watch text
    // changes too, and check what each commit left on the page.
    let flashed = false;
    const observer = new MutationObserver(() => {
      if (document.body.textContent?.includes('Match not found')) flashed = true;
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    installFakeFetch({
      ...signedIn(11),
      'GET /users/1/profiles/1/matches': { body: { total_count: 1, returned_count: 1, results: [saved({})] } },
    });
    renderApp('/matches/1');
    expect(await screen.findByText(/Saved result from/)).toBeInTheDocument();
    observer.disconnect();
    expect(flashed).toBe(false);
  });

  it('asks for a new analysis when the profile and the job have no current analysis', async () => {
    installFakeFetch({
      ...signedIn(11),
      'GET /users/1/profiles/analyzed': { body: [] },
      'GET /jobs/analyzed/7': { status: 404, body: { detail: 'No current analysis for this job.' } },
      'GET /users/1/profiles/1/matches': { body: { total_count: 1, returned_count: 1, results: [saved({})] } },
    });
    renderApp('/matches/1');
    expect(await screen.findByText('Result may be outdated')).toBeInTheDocument();
    expect(
      await screen.findByText(/There is no current analysis of your profile or this job any more\. Analyze them again/),
    ).toBeInTheDocument();
  });

  it('warns when the profile was edited after its analysis', async () => {
    window.localStorage.setItem('apply101.profileChangedAfterAnalysis.1.1', '1');
    installFakeFetch({
      ...signedIn(11),
      'GET /users/1/profiles/1/matches': { body: { total_count: 1, returned_count: 1, results: [saved({})] } },
    });
    renderApp('/matches/1');
    expect(await screen.findByText(/You changed your profile after it was analyzed/)).toBeInTheDocument();
  });

  it('does not warn when the analyses are current', async () => {
    installFakeFetch({
      ...signedIn(11),
      'GET /users/1/profiles/1/matches': { body: { total_count: 1, returned_count: 1, results: [saved({})] } },
    });
    renderApp('/matches/1');
    expect(await screen.findByText(/Saved result from/)).toBeInTheDocument();
    expect(screen.queryByText('Result may be outdated')).not.toBeInTheDocument();
  });
});

describe('saved matches list', () => {
  it('notes results whose profile has no current analysis any more', async () => {
    installFakeFetch({
      ...signedIn(11),
      'GET /users/1/profiles/analyzed': { body: [] },
      'GET /users/1/profiles/1/matches': { body: { total_count: 1, returned_count: 1, results: [saved({})] } },
    });
    renderApp('/matches');
    expect(await screen.findByText(/Your profile has no current analysis\. Analyze it, then open this match/)).toBeInTheDocument();
  });
});
