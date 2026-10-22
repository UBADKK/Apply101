import type { AnalyzedProfile } from '../../api/types';
import { Alert } from '../../components/Alert';
import { ErrorAlert } from '../../components/ErrorAlert';
import { formatDateTime, workAuthorizationLabel } from '../../components/labels';

interface ProfileAnalysisCardProps {
  analysis: AnalyzedProfile | null;
  /** The profile or CV changed in this session after the analysis ran. */
  stale: boolean;
  hasCv: boolean;
  onAnalyze: () => void;
  pending: boolean;
  error: unknown;
  cooldownSeconds: number;
}

function Chips({ items }: { items: string[] }) {
  if (items.length === 0) return <span className="muted">none found</span>;
  return (
    <ul className="chips">
      {items.map((item) => (
        <li key={item}>{item}</li>
      ))}
    </ul>
  );
}

export function ProfileAnalysisCard({ analysis, stale, hasCv, onAnalyze, pending, error, cooldownSeconds }: ProfileAnalysisCardProps) {
  const disabled = pending || cooldownSeconds > 0;
  const label = pending
    ? 'Analyzing…'
    : cooldownSeconds > 0
      ? `Try again in ${cooldownSeconds}s`
      : analysis
        ? 'Re-run analysis'
        : 'Analyze profile';

  return (
    <div>
      {!analysis && (
        <p>
          Your profile has not been analyzed with the current analysis version yet. Matching needs a current analysis.
          {!hasCv && ' Upload your CV first for a better result.'}
        </p>
      )}
      {analysis && stale && (
        <Alert tone="warning" title="Analysis may be outdated">
          You changed your profile or CV after this analysis. Re-run the analysis so matches use the new data.
        </Alert>
      )}
      {analysis && (
        <div className="analysis-summary">
          <p>{analysis.candidate_summary}</p>
          <dl className="facts">
            <div>
              <dt>Skills</dt>
              <dd>
                <Chips items={[...(analysis.strong_skills ?? []), ...(analysis.moderate_skills ?? [])]} />
              </dd>
            </div>
            <div>
              <dt>Languages</dt>
              <dd>
                <Chips items={(analysis.languages ?? []).map((item) => `${item.language} ${item.level}`)} />
              </dd>
            </div>
            <div>
              <dt>Work authorization</dt>
              <dd>{workAuthorizationLabel(analysis.work_authorization_status)}</dd>
            </div>
          </dl>
          <p className="muted small">
            Analyzed {formatDateTime(analysis.analyzed_at)} · version {analysis.analysis_prompt_version}
          </p>
        </div>
      )}
      <ErrorAlert error={error} cooldownSeconds={cooldownSeconds} />
      <button type="button" className="button button-primary" onClick={onAnalyze} disabled={disabled} aria-busy={pending}>
        {label}
      </button>
      {analysis && <p className="muted small">Re-running is limited by a cooldown to protect analysis costs.</p>}
    </div>
  );
}
