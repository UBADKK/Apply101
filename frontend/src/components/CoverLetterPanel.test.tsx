import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { CoverLetter } from '../api/types';
import { callsTo, installFakeFetch } from '../test/fakeFetch';
import { CoverLetterPanel } from './CoverLetterPanel';

function letter(overrides: Partial<CoverLetter> = {}): CoverLetter {
  const version = {
    version_id: 10,
    version_no: 1,
    kind: 'generated' as const,
    body_text: 'Dear hiring team,\n\nI am applying for the position of Backend Developer at Acme.\n\n[Describe a concrete project]',
    grounding: {
      facts: [
        { id: 'job.title', source: 'job' as const, label: 'Job title', text: 'Backend Developer' },
        { id: 'company.about', source: 'company' as const, label: 'About the company', text: 'Acme builds tools.', source_url: 'https://boards-api.greenhouse.io/v1/boards/acme' },
      ],
      gaps: [{ code: 'NO_CV_TEXT', message: 'No CV text: the draft cannot name your past roles or achievements.' }],
      used_fact_ids: ['job.title', 'company.about'],
      missing_information: ['fake_analysis_provider: fake demo draft, not written by a language model.'],
    },
    model: 'gpt-4.1-mini',
    prompt_version: 'cover_letter_v1',
    created_at: '2026-10-25T12:00:00',
  };
  return {
    letter_id: 4, job_id: 7, profile_id: 1, language: 'en', job_title: 'Backend Developer', company_name: 'Acme',
    created_at: null, updated_at: null, current_version: version, version_count: 1, versions: [version], ...overrides,
  };
}

describe('motivation letter panel', () => {
  it('writes a draft only on click and shows its sources, gaps and the demo note', async () => {
    const fetchMock = installFakeFetch({
      'GET /users/1/cover-letters': { body: [] },
      'POST /users/1/profiles/1/jobs/7/cover-letters': ({ init }) => {
        expect(JSON.parse(String(init.body))).toEqual({ language: 'de' });
        return { status: 201, body: letter({ language: 'de' }) };
      },
    });
    const user = userEvent.setup();
    render(<CoverLetterPanel userId={1} profileId={1} jobId={7} />);
    await screen.findByRole('button', { name: 'Write a draft' });
    expect(callsTo(fetchMock, 'POST /users/1/profiles/1/jobs/7/cover-letters')).toBe(0);
    await user.selectOptions(screen.getByLabelText('Language'), 'de');
    await user.click(screen.getByRole('button', { name: 'Write a draft' }));
    expect(await screen.findByText('Demo draft')).toBeInTheDocument();
    expect(screen.getByText(/No CV text/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'source' })).toHaveAttribute('href', 'https://boards-api.greenhouse.io/v1/boards/acme');
    expect(screen.getByLabelText(/Letter \(German/)).toHaveValue(letter().current_version!.body_text);
  });

  it('saves an edit with the version it started from and asks before regenerating', async () => {
    const edited = letter();
    edited.current_version = { ...edited.current_version!, version_id: 11, version_no: 2, kind: 'edited', body_text: 'My own words.', grounding: null };
    let saveBody: unknown;
    const fetchMock = installFakeFetch({
      'GET /users/1/cover-letters': { body: [letter({ versions: undefined })] },
      'GET /users/1/cover-letters/4': { body: letter() },
      'POST /users/1/cover-letters/4/versions': ({ init }) => {
        saveBody = JSON.parse(String(init.body));
        return { body: edited };
      },
      'POST /users/1/cover-letters/4/regenerate': { body: letter() },
    });
    const user = userEvent.setup();
    render(<CoverLetterPanel userId={1} profileId={1} jobId={7} />);
    const editor = await screen.findByLabelText(/Letter \(English/);
    await user.clear(editor);
    await user.type(editor, 'My own words.');
    await user.click(screen.getByRole('button', { name: 'Save as new version' }));
    expect(await screen.findByText('This version is your own edit.')).toBeInTheDocument();
    expect(saveBody).toEqual({ body_text: 'My own words.', base_version_id: 10 });

    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await user.click(screen.getByRole('button', { name: 'Regenerate draft' }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('earlier versions, edited ones included, stay'));
    expect(callsTo(fetchMock, 'POST /users/1/cover-letters/4/regenerate')).toBe(0);
  });

  it('explains the daily limit', async () => {
    installFakeFetch({
      'GET /users/1/cover-letters': { body: [] },
      'POST /users/1/profiles/1/jobs/7/cover-letters': {
        status: 429,
        headers: { 'Retry-After': '3600' },
        body: { detail: { error_code: 'ERR_COVER_LETTER_QUOTA', message: 'You reached the daily limit of letter drafts.' } },
      },
    });
    const user = userEvent.setup();
    render(<CoverLetterPanel userId={1} profileId={1} jobId={7} />);
    await user.click(await screen.findByRole('button', { name: 'Write a draft' }));
    expect(await screen.findByText(/try again in 3600 seconds/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Write a draft' })).toBeDisabled();
  });
});
