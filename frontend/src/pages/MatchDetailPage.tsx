import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/endpoints';
import type { MatchData } from '../api/types';
import { useCurrentUser } from '../auth/AuthContext';
import { Alert } from '../components/Alert';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { formatDateTime } from '../components/labels';
import { LoadingState } from '../components/LoadingState';
import { MatchResultView } from '../components/MatchResultView';
import { PageHeader } from '../components/PageHeader';
import { useAction } from '../hooks/useAction';
import { useResource } from '../hooks/useResource';
import { loadMatches, useWorkspace } from '../hooks/useWorkspace';

export function MatchDetailPage() {
  const params = useParams();
  const matchId = Number(params.matchId);
  const navigate = useNavigate();
  const user = useCurrentUser();
  const workspace = useWorkspace();
  const profile = workspace.data?.profile ?? null;
  const matches = useResource(
    () => (workspace.data ? loadMatches(user.user_id, profile) : Promise.resolve(null)),
    [user.user_id, profile?.profile_id, Boolean(workspace.data), matchId],
  );
  const [rerun, setRerun] = useState<MatchData | null>(null);

  const entry = matches.data?.results.find((item) => item.match_id === matchId);

  const rematch = useAction(async () => {
    if (!profile || !entry) return undefined;
    const response = await api.match(user.user_id, profile.profile_id, entry.job_id, true);
    setRerun(response.match);
    if (response.match_id !== matchId) navigate(`/matches/${response.match_id}`, { replace: true });
    return response;
  });

  if (workspace.loading || (matches.loading && !matches.data)) {
    return (
      <>
        <PageHeader title="Match result" />
        <LoadingState label="Loading the match" />
      </>
    );
  }
  const error = workspace.error ?? matches.error;
  if (error) {
    return (
      <>
        <PageHeader title="Match result" />
        <ErrorAlert error={error} />
      </>
    );
  }
  if (!entry) {
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

  return (
    <>
      <PageHeader
        title={entry.title}
        description={[entry.company_name, entry.location].filter(Boolean).join(' · ') || undefined}
        actions={
          <>
            <Link to={`/jobs/${entry.job_id}`} className="button button-ghost">
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
          <p className="muted small">Saved result from {formatDateTime(entry.matched_at)}.</p>
        )}
        <ErrorAlert error={rematch.error} cooldownSeconds={rematch.cooldownSeconds} />
        <MatchResultView match={rerun ?? entry.match} />
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
