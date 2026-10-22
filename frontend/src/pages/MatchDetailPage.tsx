import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/endpoints';
import type { SavedMatch } from '../api/types';
import { useCurrentUser } from '../auth/AuthContext';
import { Alert } from '../components/Alert';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { formatDateTime } from '../components/labels';
import { LoadingState } from '../components/LoadingState';
import { MatchResultView } from '../components/MatchResultView';
import { PageHeader } from '../components/PageHeader';
import { outdatedParts, StaleMatchNotice } from '../components/StaleMatchNotice';
import { useAction } from '../hooks/useAction';
import { useResource } from '../hooks/useResource';
import {
  currentJobAnalysisId,
  currentProfileAnalysisId,
  loadJobAnalysis,
  loadMatches,
  useWorkspace,
} from '../hooks/useWorkspace';
import { isProfileMarkedStale } from './profile/staleness';

export function MatchDetailPage() {
  const params = useParams();
  const matchId = Number(params.matchId);
  const navigate = useNavigate();
  const user = useCurrentUser();
  const workspace = useWorkspace();
  const profile = workspace.data?.profile ?? null;
  const matches = useResource(
    () => (workspace.data ? loadMatches(user.user_id, profile) : Promise.resolve(null)),
    [user.user_id, profile?.profile_id, Boolean(workspace.data)],
  );
  // What is on screen. After a rematch it holds the new result right away,
  // while the list (and the URL with the new match id) catch up.
  const [shown, setShown] = useState<SavedMatch | null>(null);
  const [rerun, setRerun] = useState(false);

  const listed = matches.data?.results.find((item) => item.match_id === matchId) ?? null;
  useEffect(() => {
    if (listed) setShown(listed);
  }, [listed]);
  // Render from the list right away; the effect above only keeps it on
  // screen while the list reloads after a rematch.
  const current = shown ?? listed;

  const jobAnalysis = useResource(
    () => (current ? loadJobAnalysis(current.job_id) : Promise.resolve(null)),
    [current?.job_id],
  );

  const rematch = useAction(async () => {
    if (!profile || !current) return undefined;
    const response = await api.match(user.user_id, profile.profile_id, current.job_id, true);
    setShown({
      ...current,
      match_id: response.match_id,
      profile_analysis_id: response.profile_analysis_id,
      job_analysis_id: response.job_analysis_id,
      eligibility_status: response.eligibility_status,
      overall_score: response.overall_score,
      recommendation: response.recommendation,
      matched_at: null,
      match: response.match,
    });
    setRerun(true);
    if (response.match_id !== matchId) navigate(`/matches/${response.match_id}`, { replace: true });
    matches.reload();
    return response;
  });

  // Until the list for the loaded profile has arrived, "not found" is not known.
  const loading = workspace.loading || matches.loading || (workspace.data !== undefined && matches.data == null && matches.error === null);
  const error = workspace.error ?? matches.error;

  if (!current && loading) {
    return (
      <>
        <PageHeader title="Match result" />
        <LoadingState label="Loading the match" />
      </>
    );
  }
  if (!current && error) {
    return (
      <>
        <PageHeader title="Match result" />
        <ErrorAlert error={error} />
      </>
    );
  }
  if (!current) {
    return (
      <>
        <PageHeader title="Match not found" />
        <EmptyState
          title="This saved match does not exist or is not yours."
          action={
            <Link to="/matches" className="button">
              Back to saved matches
            </Link>
          }
        />
      </>
    );
  }

  const stale = rerun ? [] : outdatedParts(current, currentProfileAnalysisId(workspace), currentJobAnalysisId(jobAnalysis));
  const profileEdited = Boolean(profile && workspace.data?.analysis && isProfileMarkedStale(user.user_id, profile.profile_id));

  return (
    <>
      <PageHeader
        title={current.title}
        description={[current.company_name, current.location].filter(Boolean).join(' · ') || undefined}
        actions={
          <>
            <Link to={`/jobs/${current.job_id}`} className="button button-ghost">
              Open the job
            </Link>
            <Link to="/matches" className="button button-ghost">
              All matches
            </Link>
          </>
        }
      />
      <section className="card">
        {rerun ? (
          <Alert tone="success">Match re-run with your current profile and job analyses.</Alert>
        ) : (
          <p className="muted small">Saved result from {formatDateTime(current.matched_at)}.</p>
        )}
        <StaleMatchNotice parts={stale} profileEdited={profileEdited} />
        <ErrorAlert error={rematch.error} cooldownSeconds={rematch.cooldownSeconds} />
        <MatchResultView match={current.match} />
        <div className="form-actions">
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void rematch.run()}
            disabled={rematch.pending || rematch.cooldownSeconds > 0}
            aria-busy={rematch.pending}
          >
            {rematch.pending ? 'Matching…' : 'Re-run match'}
          </button>
        </div>
      </section>
    </>
  );
}
