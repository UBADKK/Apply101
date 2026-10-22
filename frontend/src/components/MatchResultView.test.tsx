import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { MatchData } from '../api/types';
import { MatchResultView } from './MatchResultView';

function match(overrides: Partial<MatchData>): MatchData {
  return {
    match_version: 'backend_match_v6',
    eligibility_status: 'eligible',
    eligible: true,
    hard_fail_reasons: [],
    review_reasons: [],
    role_fit_score: 28,
    skills_fit_score: 79,
    experience_fit_score: 100,
    seniority_fit_score: 70,
    location_fit_score: 100,
    work_type_fit_score: 60,
    employment_type_fit_score: 80,
    language_fit_score: 100,
    visa_fit_score: 100,
    overall_score: 74,
    recommendation: 'apply',
    summary: 'Candidate is eligible. Backend rule-based score is 74.',
    strengths: ['Candidate skills match many job requirements.'],
    weaknesses: ['Role is not very close to the candidate target role.'],
    dealbreaker_warnings: ['Required language: english (minimum c1).'],
    ...overrides,
  };
}

describe('MatchResultView', () => {
  it('shows an eligible result with score, strengths and weaknesses', () => {
    render(<MatchResultView match={match({})} />);
    expect(screen.getByText('Eligible')).toBeInTheDocument();
    expect(screen.getByLabelText('Overall score 74 out of 100')).toBeInTheDocument();
    expect(screen.getByText('Good match: apply')).toBeInTheDocument();
    expect(screen.getByText('Candidate skills match many job requirements.')).toBeInTheDocument();
    expect(screen.getByText('Role is not very close to the candidate target role.')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Why you are not eligible' })).not.toBeInTheDocument();
  });

  it('explains an ineligible result with its hard-fail reasons', () => {
    render(
      <MatchResultView
        match={match({
          eligibility_status: 'ineligible',
          eligible: false,
          overall_score: 20,
          recommendation: 'ineligible',
          hard_fail_reasons: [{ code: 'required_language_missing', message: 'Required language is missing: german.' }],
        })}
      />,
    );
    expect(screen.getByText('Not eligible')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Why you are not eligible' })).toBeInTheDocument();
    expect(screen.getByText(/Required language is missing: german\./)).toBeInTheDocument();
    expect(screen.getByText('Do not apply')).toBeInTheDocument();
  });

  it('lists what needs review for review_required', () => {
    render(
      <MatchResultView
        match={match({
          eligibility_status: 'review_required',
          eligible: false,
          overall_score: 57,
          recommendation: 'manual_review',
          review_reasons: [{ code: 'candidate_language_level_unknown', message: 'Language level is unknown: german.' }],
        })}
      />,
    );
    expect(screen.getByText('Needs review')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'What needs review' })).toBeInTheDocument();
    expect(screen.getByText(/Language level is unknown: german\./)).toBeInTheDocument();
  });
});
