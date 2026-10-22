import { Link } from 'react-router-dom';
import { api } from '../api/endpoints';
import { useCurrentUser } from '../auth/AuthContext';
import { ErrorAlert } from '../components/ErrorAlert';
import { recommendationLabel } from '../components/labels';
import { LoadingState } from '../components/LoadingState';
import { EligibilityBadge } from '../components/MatchResultView';
import { PageHeader } from '../components/PageHeader';
import { useResource } from '../hooks/useResource';
import { loadMatches, useWorkspace } from '../hooks/useWorkspace';

interface Step {
  done: boolean;
  label: string;
  to: string;
  action: string;
}

export function DashboardPage() {
  const user = useCurrentUser();
  const workspace = useWorkspace();
  const jobs = useResource(() => api.listOwnJobs(), []);
  const profile = workspace.data?.profile ?? null;
  const matches = useResource(
    () => (workspace.data ? loadMatches(user.user_id, profile) : Promise.resolve(null)),
    [user.user_id, profile?.profile_id, Boolean(workspace.data)],
  );

  const loading = workspace.loading || jobs.loading || (matches.loading && !matches.data);
  const error = workspace.error ?? jobs.error ?? matches.error;
  const matchCount = matches.data?.results.length ?? 0;

  const steps: Step[] = [
    { done: Boolean(profile), label: 'Create your profile', to: '/profile', action: 'Open profile' },
    { done: Boolean(profile?.cv_filename), label: 'Upload your CV (PDF)', to: '/profile', action: 'Upload CV' },
    { done: Boolean(workspace.data?.analysis), label: 'Analyze your profile', to: '/profile', action: 'Analyze' },
    { done: (jobs.data?.length ?? 0) > 0, label: 'Add a job posting', to: '/jobs/new', action: 'Add a job' },
    { done: matchCount > 0, label: 'Match a job against your profile', to: '/jobs', action: 'Open jobs' },
  ];
  const nextStep = steps.find((step) => !step.done);

  return (
    <>
      <PageHeader title={`Hello, ${user.name}`} description="Your path from profile to a reasoned match." />
      {loading && <LoadingState label="Loading your overview" />}
      {!loading && error !== null && <ErrorAlert error={error} />}
      {!loading && error === null && (
        <>
          <section className="card" aria-labelledby="steps-title">
            <h2 id="steps-title">Getting started</h2>
            <ol className="steps">
              {steps.map((step) => (
                <li key={step.label} className={step.done ? 'step-done' : ''}>
                  <span className="step-state" aria-hidden="true">
                    {step.done ? '✓' : '•'}
                  </span>
                  <span className="step-label">
                    {step.label}
                    <span className="visually-hidden">{step.done ? ' (done)' : ' (to do)'}</span>
                  </span>
                  {!step.done && step === nextStep && (
                    <Link to={step.to} className="button button-primary button-small">
                      {step.action}
                    </Link>
                  )}
                </li>
              ))}
            </ol>
          </section>

          <section className="card" aria-labelledby="recent-title">
            <div className="section-heading">
              <h2 id="recent-title">Recent matches</h2>
              {matchCount > 0 && <Link to="/matches">See all</Link>}
            </div>
            {matchCount === 0 ? (
              <p className="muted">No matches yet.</p>
            ) : (
              <ul className="plain-list recent-matches">
                {[...matches.data!.results]
                  .sort((a, b) => (b.matched_at ?? '').localeCompare(a.matched_at ?? ''))
                  .slice(0, 3)
                  .map((item) => (
                  <li key={item.match_id}>
                    <Link to={`/matches/${item.match_id}`}>{item.title}</Link> <EligibilityBadge status={item.eligibility_status} />{' '}
                    <span className="muted">
                      {item.overall_score}/100 · {recommendationLabel(item.recommendation)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </>
  );
}
