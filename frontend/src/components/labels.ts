import type { EligibilityStatus } from '../api/types';

export const ELIGIBILITY_LABELS: Record<EligibilityStatus, string> = {
  eligible: 'Eligible',
  ineligible: 'Not eligible',
  review_required: 'Needs review',
};

export const ELIGIBILITY_DESCRIPTIONS: Record<EligibilityStatus, string> = {
  eligible: 'No hard requirement blocks this job.',
  ineligible: 'At least one confirmed hard requirement is not met.',
  review_required: 'Some facts are missing, so check the points below before applying.',
};

const RECOMMENDATION_LABELS: Record<string, string> = {
  strong_apply: 'Strong match: apply',
  apply: 'Good match: apply',
  maybe: 'Possible match',
  weak_match: 'Weak match',
  manual_review: 'Review manually',
  ineligible: 'Do not apply',
};

export function recommendationLabel(value: string): string {
  return RECOMMENDATION_LABELS[value] ?? value;
}

const WORK_AUTHORIZATION_LABELS: Record<string, string> = {
  germany: 'Authorized to work in Germany',
  eu_eea: 'EU/EEA work rights',
  other_country_only: 'Authorized only in another country',
  none: 'No work authorization',
  unknown: 'Not sure',
};

export function workAuthorizationLabel(value: string | null): string {
  if (!value) return 'Not specified';
  return WORK_AUTHORIZATION_LABELS[value] ?? value;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return 'unknown time';
  const date = new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  // The UI is English, so dates are too, whatever the browser locale is.
  return date.toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
}

export function titleCase(value: string): string {
  return value
    .replace(/_/g, ' ')
    .split(' ')
    .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : word))
    .join(' ');
}
