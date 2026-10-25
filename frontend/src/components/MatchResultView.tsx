import type { EligibilityStatus, MatchData, MatchReason } from '../api/types';
import { ELIGIBILITY_DESCRIPTIONS, ELIGIBILITY_LABELS, recommendationLabel } from './labels';

const SCORE_ROWS: { key: keyof MatchData; label: string }[] = [
  { key: 'skills_fit_score', label: 'Skills' },
  { key: 'experience_fit_score', label: 'Experience' },
  { key: 'language_fit_score', label: 'Languages' },
  { key: 'role_fit_score', label: 'Role' },
  { key: 'seniority_fit_score', label: 'Seniority' },
  { key: 'location_fit_score', label: 'Location' },
  { key: 'work_type_fit_score', label: 'Work type' },
  { key: 'employment_type_fit_score', label: 'Employment type' },
  { key: 'visa_fit_score', label: 'Visa' },
];

export function EligibilityBadge({ status }: { status: EligibilityStatus }) {
  return <span className={`badge badge-${status}`}>{ELIGIBILITY_LABELS[status] ?? status}</span>;
}

function ReasonList({ title, reasons }: { title: string; reasons: MatchReason[] }) {
  if (reasons.length === 0) return null;
  return (
    <section className="result-section">
      <h3>{title}</h3>
      <ul className="reason-list">
        {reasons.map((reason, index) => (
          <li key={`${reason.code}-${index}`}>
            {reason.message}
            <span className="reason-code"> ({reason.code})</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function TextList({ title, items, tone }: { title: string; items: string[]; tone: 'good' | 'bad' | 'note' }) {
  if (items.length === 0) return null;
  return (
    <section className="result-section">
      <h3>{title}</h3>
      <ul className={`text-list text-list-${tone}`}>
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

export function MatchResultView({ match }: { match: MatchData }) {
  const status = match.eligibility_status;
  return (
    <article className={`match-result match-${status}`} aria-label="Match result">
      <div className="match-headline">
        <div>
          <EligibilityBadge status={status} />
          <p className="match-status-text">{ELIGIBILITY_DESCRIPTIONS[status]}</p>
        </div>
        <div className="score" aria-label={`Overall score ${match.overall_score} out of 100`}>
          <span className="score-value">{match.overall_score}</span>
          <span className="score-scale">/100</span>
          <span className="score-label">{recommendationLabel(match.recommendation)}</span>
        </div>
      </div>
      <p className="match-summary">{match.summary}</p>

      <ReasonList title="Why you are not eligible" reasons={match.hard_fail_reasons ?? []} />
      <ReasonList title="What needs review" reasons={match.review_reasons ?? []} />

      <div className="result-columns">
        <TextList title="Strengths" items={match.strengths ?? []} tone="good" />
        <TextList title="Weaknesses" items={match.weaknesses ?? []} tone="bad" />
      </div>
      <TextList title="Requirements and warnings" items={match.dealbreaker_warnings ?? []} tone="note" />

      <section className="result-section">
        <h3>Score breakdown</h3>
        <dl className="score-breakdown">
          {SCORE_ROWS.map(({ key, label }) => {
            const value = typeof match[key] === 'number' ? (match[key] as number) : null;
            if (value === null) return null;
            return (
              <div className="score-row" key={key}>
                <dt>{label}</dt>
                <dd>
                  <meter min={0} max={100} value={value} aria-label={`${label} score`} />
                  <span className="score-row-value">{value}</span>
                </dd>
              </div>
            );
          })}
        </dl>
      </section>
      <p className="muted small">Matching rules version {match.match_version}</p>
    </article>
  );
}
