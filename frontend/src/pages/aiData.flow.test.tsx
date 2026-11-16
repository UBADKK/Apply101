import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { callsTo, fakeToken, installFakeFetch, USER, type FakeResponse } from '../test/fakeFetch';
import { PROFILE, renderApp } from '../test/renderApp';

const OPENAI: FakeResponse = { body: { analysis_provider: 'openai', demo: false } };
const FAKE: FakeResponse = { body: { analysis_provider: 'fake', demo: true } };

const JOB = {
  job_id: 3,
  title: 'Synthetic Backend Developer',
  company_name: 'Example GmbH',
  location: 'Berlin',
  url: 'https://example.com/jobs/synthetic',
  description_text: 'Synthetic posting with Python, FastAPI and SQL.',
};

const VERSION = {
  version_id: 10,
  version_no: 1,
  kind: 'generated' as const,
  body_text: 'Dear hiring team,\n\n[Describe a concrete project]',
  grounding: { facts: [], gaps: [], used_fact_ids: [], missing_information: [] },
  model: 'gpt-4.1-mini',
  prompt_version: 'cover_letter_v1',
  created_at: '2026-10-25T12:00:00',
};
const LETTER = {
  letter_id: 4, job_id: 3, profile_id: 1, language: 'en', job_title: JOB.title, company_name: JOB.company_name,
  created_at: null, updated_at: null, current_version: VERSION, version_count: 1, versions: [VERSION],
};

function signedIn(meta: FakeResponse) {
  window.localStorage.setItem('apply101.accessToken', fakeToken(3600));
  return installFakeFetch({
    'GET /meta': meta,
    'GET /auth/me': { body: USER },
    'GET /users/1/profiles': { body: [PROFILE] },
    'GET /users/1/languages': { body: [] },
    'GET /users/1/profiles/analyzed': { body: [] },
    'GET /users/1/profiles/1/matches': { body: { total_count: 0, returned_count: 0, results: [] } },
    'GET /jobs/3': { body: JOB },
    'GET /jobs/analyzed/3': { status: 404, body: { detail: 'No current completed analysis exists for this job.' } },
    'GET /users/1/cover-letters': { body: [LETTER] },
    'GET /users/1/cover-letters/4': { body: LETTER },
  });
}

const ERROR_META: FakeResponse = { status: 500, body: { detail: 'Internal Server Error' } };

/** Holds every /meta response until the returned function is called. */
function holdMeta(fetchMock: ReturnType<typeof installFakeFetch>) {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const original = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (input, init) => {
    if (new URL(String(input)).pathname.endsWith('/meta')) await gate;
    return original(input, init);
  });
  return () => release();
}

const REGISTER_OPENAI_NOTE =
  /^Profile analyses and letter drafts started by you or an administrator on your account send your profile data, including your CV text, to OpenAI; profile analyses also send your name and email, and job analyses send only the posting\./;

/** Job detail with all three AI actions on screen: analyze, write, regenerate. */
async function openJob() {
  renderApp('/jobs/3');
  await screen.findByRole('button', { name: 'Analyze job' });
  await screen.findByRole('button', { name: 'Write a draft' });
  await screen.findByRole('button', { name: 'Regenerate draft' });
}

function sectionOf(headingName: string) {
  return screen.getByRole('heading', { name: headingName }).closest('section')!;
}

function noteWith(container: HTMLElement, text: RegExp) {
  const note = within(container)
    .getAllByRole('note')
    .find((element) => text.test(element.textContent ?? ''));
  expect(note, `no note matching ${text}`).toBeDefined();
  return note!;
}

function expectPageLink(note: HTMLElement) {
  expect(within(note).getByRole('link', { name: /How Apply101 uses AI/ })).toHaveAttribute('href', '/ai-data');
}

describe('AI data notes with the real provider', () => {
  it('names OpenAI and what is sent next to job analysis, letter create and regenerate', async () => {
    const fetchMock = signedIn(OPENAI);
    await openJob();
    const job = noteWith(sectionOf('Job analysis'), /^Analyzing this job sends the posting's title, company, location and description to OpenAI\. No profile or account data is sent\./);
    expectPageLink(job);
    const letters = sectionOf('Motivation letter');
    expectPageLink(noteWith(letters, /^Writing a draft sends your profile facts, skills, languages and CV text, this posting and, for job-pool postings, company information to OpenAI\./));
    expectPageLink(noteWith(letters, /^Regenerating a draft sends .* to OpenAI\./));
    expect(screen.queryByText(/Demo mode/)).not.toBeInTheDocument();
    expect(callsTo(fetchMock, 'GET /meta')).toBe(1);
  });

  it('names OpenAI and the profile data next to the profile analysis', async () => {
    signedIn(OPENAI);
    renderApp('/profile');
    await screen.findByRole('button', { name: 'Analyze profile' });
    const note = await waitFor(() =>
      noteWith(
        sectionOf('Profile analysis'),
        /^Analyzing your profile sends your name and email, your CV text, your languages and your profile details, such as roles, locations, visa, work authorization and residence, to OpenAI\./,
      ),
    );
    expectPageLink(note);
  });
});

describe('AI data notes in demo mode', () => {
  it('never says data goes to OpenAI and shows the demo wording and banner, with one /meta request', async () => {
    const fetchMock = signedIn(FAKE);
    await openJob();
    await waitFor(() => expect(screen.getAllByText(/Demo mode: nothing is sent to an AI provider\./)).toHaveLength(3));
    expect(document.body).not.toHaveTextContent(/OpenAI/);
    expect(screen.getByText('Demo mode.')).toBeInTheDocument();
    expect(callsTo(fetchMock, 'GET /meta')).toBe(1);
  });

  it('shows the demo wording next to the profile analysis', async () => {
    signedIn(FAKE);
    renderApp('/profile');
    await screen.findByRole('button', { name: 'Analyze profile' });
    await waitFor(() => expectPageLink(noteWith(sectionOf('Profile analysis'), /^Demo mode: nothing is sent to an AI provider\./)));
    expect(document.body).not.toHaveTextContent(/OpenAI/);
  });
});

describe('AI data notes when the provider is unknown', () => {
  it.each([
    ['a failed /meta request', { status: 500, body: { detail: 'Internal Server Error' } }],
    ['an unrecognised provider', { body: { analysis_provider: 'other', demo: false } }],
    ['an inconsistent answer', { body: { analysis_provider: 'fake', demo: false } }],
  ])('falls back to the disclosure after %s', async (_label, meta) => {
    const fetchMock = signedIn(meta);
    await openJob();
    await waitFor(() => expect(callsTo(fetchMock, 'GET /meta')).toBe(1));
    noteWith(sectionOf('Job analysis'), /^Analyzing this job may send .* to OpenAI\./);
    noteWith(sectionOf('Motivation letter'), /^Writing a draft may send .* to OpenAI\./);
    noteWith(sectionOf('Motivation letter'), /^Regenerating a draft may send .* to OpenAI\./);
    expect(screen.queryByText(/Demo mode/)).not.toBeInTheDocument();
    expect(screen.queryByText(/nothing is sent/)).not.toBeInTheDocument();
  });
});

describe('AI data notes when the provider is unknown (failed /meta)', () => {
  it('uses the cautious wording next to the profile analysis', async () => {
    const fetchMock = signedIn(ERROR_META);
    renderApp('/profile');
    await screen.findByRole('button', { name: 'Analyze profile' });
    await waitFor(() => noteWith(sectionOf('Profile analysis'), /^Analyzing your profile may send your name and email, .* to OpenAI\./));
    expect(callsTo(fetchMock, 'GET /meta')).toBe(1);
    expect(screen.queryByText(/Demo mode|nothing is sent/)).not.toBeInTheDocument();
  });

  it('uses the cautious wording on the register page', async () => {
    installFakeFetch({ 'GET /meta': ERROR_META });
    renderApp('/register');
    const form = (await screen.findByRole('button', { name: 'Create account' })).closest('form')!;
    const note = await waitFor(() => noteWith(form, /may send your profile data, including your CV text, to OpenAI; profile analyses may also send your name and email, and job analyses may send only the posting\./));
    expect(note).toHaveTextContent(/^Profile analyses and letter drafts started by you or an administrator on your account may send/);
    expect(screen.queryByText(/Demo mode|nothing is sent/)).not.toBeInTheDocument();
  });

  it('uses the cautious wording on the notice page', async () => {
    installFakeFetch({ 'GET /meta': ERROR_META });
    renderApp('/ai-data');
    expect(
      await screen.findByText('Apply101 could not confirm which provider this deployment uses, so this page assumes that your data may be sent to OpenAI.'),
    ).toBeInTheDocument();
    expect(screen.getByText(/^Apply101 may use/)).toHaveTextContent(/Apply101 may send it the data listed below\./);
    expect(document.body).not.toHaveTextContent(/Demo mode|When the real provider is enabled|This deployment uses OpenAI/);
  });
});

describe('AI data notes while /meta is loading', () => {
  it('stay neutral on a paid-action screen, then show the answer', async () => {
    const fetchMock = signedIn(OPENAI);
    const release = holdMeta(fetchMock);
    await openJob();
    expect(within(sectionOf('Job analysis')).getByRole('note')).toHaveTextContent(/^AI data use: How Apply101 uses AI/);
    expect(document.body).not.toHaveTextContent(/OpenAI|Demo mode/);
    release();
    await waitFor(() => noteWith(sectionOf('Job analysis'), /^Analyzing this job sends .* to OpenAI\./));
    expect(callsTo(fetchMock, 'GET /meta')).toBe(1);
  });

  it('stay neutral on the register page, then show the demo wording', async () => {
    const fetchMock = installFakeFetch({ 'GET /meta': FAKE });
    const release = holdMeta(fetchMock);
    renderApp('/register');
    const form = (await screen.findByRole('button', { name: 'Create account' })).closest('form')!;
    expect(within(form).getByRole('note')).toHaveTextContent(/^AI data use: How Apply101 uses AI/);
    expect(document.body).not.toHaveTextContent(/OpenAI|Demo mode/);
    release();
    await waitFor(() => noteWith(form, /^Demo mode: nothing is sent to an AI provider\./));
    expect(screen.getByText('Demo mode.')).toBeInTheDocument();
  });

  it('describe the real provider conditionally on the notice page', async () => {
    const fetchMock = installFakeFetch({ 'GET /meta': FAKE });
    const release = holdMeta(fetchMock);
    renderApp('/ai-data');
    const checking = await screen.findByText('Checking which provider this deployment uses…');
    expect(checking.closest('[role="note"]')).not.toHaveTextContent(/OpenAI/);
    expect(screen.getByText(/^When the real provider is enabled, Apply101 uses/)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/Demo mode|could not confirm|This deployment uses OpenAI|Apply101 may use/);
    // No sentence names OpenAI in the present tense without the condition.
    expect(screen.queryByText(/^Apply101 uses/)).not.toBeInTheDocument();
    release();
    expect(await screen.findByText('Demo mode: nothing is sent to an AI provider.')).toBeInTheDocument();
    expect(screen.getByText(/^When the real provider is enabled, Apply101 uses/)).toBeInTheDocument();
  });
});

describe('the "How Apply101 uses AI" page', () => {
  it('is linked logged out from the register page and the footer', async () => {
    installFakeFetch({ 'GET /meta': OPENAI });
    renderApp('/register');
    await screen.findByRole('heading', { name: 'Create an account' });
    expect(screen.queryByText(/stay private/)).not.toBeInTheDocument();
    const form = screen.getByRole('button', { name: 'Create account' }).closest('form')!;
    expectPageLink(await waitFor(() => noteWith(form, REGISTER_OPENAI_NOTE)));
    const footerLink = within(screen.getByRole('contentinfo')).getByRole('link', { name: 'How Apply101 uses AI (opens in a new tab)' });
    expect(footerLink).toHaveAttribute('href', '/ai-data');
    expect(footerLink).toHaveAttribute('target', '_blank');
  });

  it('opens logged out', async () => {
    installFakeFetch({ 'GET /meta': OPENAI });
    renderApp('/ai-data');
    expect(await screen.findByRole('heading', { name: 'How Apply101 uses AI' })).toBeInTheDocument();
    expect(await screen.findByText('This deployment uses OpenAI for the actions described below.')).toBeInTheDocument();
    expect(screen.getByText(/^Apply101 uses/)).toHaveTextContent(/Apply101 sends it the data listed below\./);
  });

  it('is linked from the login page footer', async () => {
    installFakeFetch({ 'GET /meta': OPENAI });
    renderApp('/login');
    await screen.findByRole('heading', { name: 'Log in' });
    expect(within(screen.getByRole('contentinfo')).getByRole('link', { name: 'How Apply101 uses AI (opens in a new tab)' })).toHaveAttribute('href', '/ai-data');
  });

  it('keeps registration working', async () => {
    const token = fakeToken(3600);
    const fetchMock = installFakeFetch({
      'GET /meta': OPENAI,
      'POST /auth/register': { status: 201, body: USER },
      'POST /auth/login': { body: { access_token: token, token_type: 'bearer' } },
      'GET /auth/me': { body: USER },
      'GET /users/1/profiles': { body: [] },
      'GET /users/1/languages': { body: [] },
      'GET /users/1/profiles/analyzed': { body: [] },
      'GET /jobs/': { body: [] },
    });
    const user = userEvent.setup();
    renderApp('/register');
    await user.type(await screen.findByLabelText(/^Name/), 'Test User');
    await user.type(screen.getByLabelText(/^Email/), 'test.user@example.com');
    await user.type(screen.getByLabelText(/^Password/), 'a-synthetic-password-123');
    await user.type(screen.getByLabelText(/^Repeat password/), 'a-synthetic-password-123');
    await user.click(screen.getByRole('button', { name: 'Create account' }));
    expect(await screen.findByRole('heading', { name: 'Hello, Test User' })).toBeInTheDocument();
    expect(callsTo(fetchMock, 'POST /auth/register')).toBe(1);
    expect(within(screen.getByRole('contentinfo')).getByRole('link', { name: 'How Apply101 uses AI (opens in a new tab)' })).toHaveAttribute('href', '/ai-data');
  });

  it('names every operation and data category and makes no unsupported promise', async () => {
    installFakeFetch({ 'GET /meta': OPENAI });
    renderApp('/ai-data');
    await screen.findByText('This deployment uses OpenAI for the actions described below.');
    const text = document.body.textContent ?? '';
    for (const required of [
      /OpenAI, through its API/,
      /Profile analysis/,
      /Job analysis/,
      /Motivation letter \(write or regenerate a draft\)/,
      /name and email address/,
      /CV text/,
      /visa sponsorship/,
      /work authorization/,
      /residence/,
      /languages/,
      /job posting/,
      /company information/,
      /job-pool postings only/,
      /administrator/,
      /governed by OpenAI's own terms/,
      /not a complete privacy policy/,
      /No retention period has been defined yet/,
    ]) {
      expect(text, `missing ${required}`).toMatch(required);
    }
    for (const forbidden of [
      /never (be )?stored/i,
      /not (be )?stored/i,
      /(never|not) (be )?used (for|to) train/i,
      /deleted (after|within)/i,
      /encrypt/i,
      /GDPR|DSGVO/i,
      /\bcompliant\b/i,
      /you (have )?(consented|agreed)/i,
      /your consent/i,
    ]) {
      expect(text, `forbidden ${forbidden}`).not.toMatch(forbidden);
    }
  });

  it('says first in demo mode that nothing is sent to an AI provider', async () => {
    installFakeFetch({ 'GET /meta': FAKE });
    renderApp('/ai-data');
    const header = (await screen.findByRole('heading', { level: 1, name: 'How Apply101 uses AI' })).closest('.page-header')!;
    expect(header).not.toHaveTextContent(/OpenAI/);
    const note = await screen.findByText('Demo mode: nothing is sent to an AI provider.');
    expect(note.closest('[role="note"]')).toHaveTextContent(/describes what happens when the real provider is enabled/);
    expect(screen.queryByText('This deployment uses OpenAI for the actions described below.')).not.toBeInTheDocument();
  });
});
