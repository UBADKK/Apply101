import { Link } from 'react-router-dom';
import { useCurrentUser } from '../auth/AuthContext';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { formatDateTime, recommendationLabel } from '../components/labels';
import { LoadingState } from '../components/LoadingState';
import { EligibilityBadge } from '../components/MatchResultView';
import { PageHeader } from '../components/PageHeader';
import { useResource } from '../hooks/useResource';
import { loadMatches, useWorkspace } from '../hooks/useWorkspace';

export function MatchesPage() {
  const user = useCurrentUser();
  const workspace = useWorkspace();
  const profile = workspace.data?.profile ?? null;
  const matches = useResource(
    () => (workspace.data ? loadMatches(user.user_id, profile) : Promise.resolve(null)),
    [user.user_id, profile?.profile_id, Boolean(workspace.data)],
  );

  const currentAnalysisId = workspace.data?.analysis?.analysis_id;
  const loading = workspace.loading || (matches.loading && !matches.data);
  const error = workspace.error ?? matches.error;

  return (
    <>
      <PageHeader title="Saved matches" description="Every match result is saved. Open one to see its reasons again." />
      {loading && <LoadingState label="Loading your matches" />}
      {!loading && error !== null && (
        <>
          <ErrorAlert error={error} />
          <button type="button" className="button" onClick={() => (workspace.error ? workspace.reload() : matches.reload())}>
            Try again
          </button>
        </>
      )}
      {!loading && error === null && workspace.data && !profile && (
        <EmptyState
          title="No profile yet"
          action={
            <Link to="/profile" className="button button-primary">
              Create your profile
            </Link>
          }
        >
          Matches compare your profile with a job, so start with your profile.
        </EmptyState>
      )}
      {!loading && error === null && profile && matches.data && matches.data.results.length === 0 && (
        <EmptyState
          title="No matches yet"
          action={
            <Link to="/jobs" className="button button-primary">
              Go to your jobs
            </Link>
          }
        >
          Open an analyzed job and choose “Match with my profile”.
        </EmptyState>
      )}
      {!loading && matches.data && matches.data.total_count > matches.data.returned_count && (
        <p className="muted small">
          Showing the {matches.data.returned_count} best of {matches.data.total_count} saved matches.
        </p>
      )}
      {!loading && matches.data && matches.data.results.length > 0 && (
        <ul className="card-list">
          {matches.data.results.map((item) => (
            <li key={item.match_id} className="card card-link match-card">
              <div className="match-card-main">
                <h2 className="card-title">
                  <Link to={`/matches/${item.match_id}`}>{item.title}</Link>
                </h2>
                <p className="muted">{[item.company_name, item.location].filter(Boolean).join(' · ')}</p>
                <p className="muted small">Matched {formatDateTime(item.matched_at)}</p>
                {currentAnalysisId != null && item.profile_analysis_id !== currentAnalysisId && (
                  <p className="outdated-note small">Computed from an older profile analysis. Open it to re-run.</p>
                )}
              </div>
              <div className="match-card-side">
                <EligibilityBadge status={item.eligibility_status} />
                <span className="match-card-score">{item.overall_score}/100</span>
                <span className="muted small">{recommendationLabel(item.recommendation)}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
