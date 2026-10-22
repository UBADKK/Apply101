import { useEffect, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { isApiError } from '../api/client';
import { api } from '../api/endpoints';
import type { AnalyzedJob, MatchData } from '../api/types';
import { useCurrentUser } from '../auth/AuthContext';
import { Alert } from '../components/Alert';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { formatDateTime, titleCase } from '../components/labels';
import { LoadingState } from '../components/LoadingState';
import { MatchResultView } from '../components/MatchResultView';
import { PageHeader } from '../components/PageHeader';
import { useAction } from '../hooks/useAction';
import { useResource } from '../hooks/useResource';
import { loadJobAnalysis, loadMatches, useWorkspace } from '../hooks/useWorkspace';

interface ShownMatch {
  data: MatchData;
  source: 'saved' | 'created' | 'cached';
  matchedAt: string | null;
}

function known(value: string | null | undefined): value is string {
  return Boolean(value) && value !== 'unknown';
}

function JobAnalysisSummary({ analysis }: { analysis: AnalyzedJob }) {
  const facts: [string, string][] = [];
  if (known(analysis.work_type)) facts.push(['Work type', titleCase(analysis.work_type)]);
  if (known(analysis.employment_type)) facts.push(['Employment type', titleCase(analysis.employment_type)]);
  if (known(analysis.seniority_level)) facts.push(['Seniority', titleCase(analysis.seniority_level)]);
  if (known(analysis.visa_sponsorship)) facts.push(['Visa sponsorship', analysis.visa_sponsorship === 'yes' ? 'Offered' : 'Not offered']);
  return (
    <div className="analysis-summary">
      {analysis.analysis?.summary && <p>{analysis.analysis.summary}</p>}
      <dl className="facts">
        <div>
          <dt>Required skills</dt>
          <dd>
            {analysis.required_skills.length ? (
              <ul className="chips">
                {analysis.required_skills.map((skill) => (
                  <li key={skill}>{skill}</li>
                ))}
              </ul>
            ) : (
              <span className="muted">none found</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Language requirements</dt>
          <dd>
            {analysis.language_requirements.length ? (
              <ul className="plain-list">
                {analysis.language_requirements.map((requirement, index) => (
                  <li key={`${requirement.language}-${index}`}>
                    {titleCase(requirement.language)} {requirement.minimum_level !== 'unknown' ? requirement.minimum_level.toUpperCase() : ''}
                    {requirement.required ? ' (required)' : ' (preferred)'}
                  </li>
                ))}
              </ul>
            ) : (
              <span className="muted">none stated</span>
            )}
          </dd>
        </div>
        {facts.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {analysis.dealbreakers.length > 0 && (
        <>
          <h3>Hard requirements noted</h3>
          <ul className="text-list text-list-note">
            {analysis.dealbreakers.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </>
      )}
      <p className="muted small">
        Analyzed {formatDateTime(analysis.analyzed_at)} · version {analysis.analysis_prompt_version}
      </p>
    </div>
  );
}

export function JobDetailPage() {
  const params = useParams();
  const jobId = Number(params.jobId);
  const user = useCurrentUser();
  const location = useLocation();
  const justCreated = Boolean((location.state as { created?: boolean } | null)?.created);

  const job = useResource(() => api.getJob(jobId), [jobId]);
  const jobAnalysis = useResource(() => loadJobAnalysis(jobId), [jobId]);
  const workspace = useWorkspace();
  const profile = workspace.data?.profile ?? null;
  const profileAnalysis = workspace.data?.analysis ?? null;
  const saved = useResource(
    () => (workspace.data ? loadMatches(user.user_id, profile) : Promise.resolve(null)),
    [user.user_id, profile?.profile_id, Boolean(workspace.data)],
  );
  const [shown, setShown] = useState<ShownMatch | null>(null);

  useEffect(() => {
    const entry = saved.data?.results.find((item) => item.job_id === jobId);
    if (entry) setShown((current) => current ?? { data: entry.match, source: 'saved', matchedAt: entry.matched_at });
  }, [saved.data, jobId]);

  const analyze = useAction(async () => {
    await api.analyzeJob(jobId);
    jobAnalysis.reload();
    return true;
  });

  const match = useAction(async (force: boolean) => {
    if (!profile) return undefined;
    const response = await api.match(user.user_id, profile.profile_id, jobId, force);
    setShown({ data: response.match, source: response.status === 'cached' ? 'cached' : 'created', matchedAt: null });
    return response;
  });

  if (!Number.isInteger(jobId) || jobId <= 0 || (isApiError(job.error) && job.error.status === 404)) {
    return (
      <>
        <PageHeader title="Job not found" />
        <EmptyState
          title="This job does not exist or is not yours."
          action={
            <Link to="/jobs" className="button">
              Back to your jobs
            </Link>
          }
        />
      </>
    );
  }
  if (job.loading && !job.data) {
    return (
      <>
        <PageHeader title="Job" />
        <LoadingState label="Loading the job" />
      </>
    );
  }
  if (!job.data) {
    return (
      <>
        <PageHeader title="Job" />
        <ErrorAlert error={job.error} />
        <button type="button" className="button" onClick={job.reload}>
          Try again
        </button>
      </>
    );
  }

  const data = job.data;
  const analysisReady = Boolean(jobAnalysis.data);
  const canMatch = Boolean(profile && profileAnalysis && analysisReady);

  return (
    <>
      <PageHeader
        title={data.title}
        description={[data.company_name, data.location].filter(Boolean).join(' · ') || undefined}
        actions={
          <Link to="/jobs" className="button button-ghost">
            All jobs
          </Link>
        }
      />
      {justCreated && <Alert tone="success">Job saved. Analyze it next, then match it against your profile.</Alert>}

      <section className="card" aria-labelledby="posting-title">
        <h2 id="posting-title">Posting</h2>
        <p>
          <a href={data.url} target="_blank" rel="noopener noreferrer">
            Open the original posting<span className="visually-hidden"> (opens in a new tab)</span>
          </a>
        </p>
        <details>
          <summary>Show the description</summary>
          <p className="description-text">{data.description_text}</p>
        </details>
      </section>

      <section className="card" aria-labelledby="job-analysis-title">
        <h2 id="job-analysis-title">Job analysis</h2>
        {jobAnalysis.loading && !jobAnalysis.data && <LoadingState label="Loading the analysis" />}
        {jobAnalysis.error !== null && <ErrorAlert error={jobAnalysis.error} />}
        {!jobAnalysis.loading && jobAnalysis.error === null && !jobAnalysis.data && (
          <>
            <p>This job has not been analyzed with the current analysis version yet.</p>
            <ErrorAlert error={analyze.error} cooldownSeconds={analyze.cooldownSeconds} />
            <button
              type="button"
              className="button button-primary"
              onClick={() => void analyze.run()}
              disabled={analyze.pending || analyze.cooldownSeconds > 0}
              aria-busy={analyze.pending}
            >
              {analyze.pending ? 'Analyzing…' : analyze.cooldownSeconds > 0 ? `Try again in ${analyze.cooldownSeconds}s` : 'Analyze job'}
            </button>
            <p className="muted small">Analyses count toward a daily limit and are not retried automatically.</p>
          </>
        )}
        {jobAnalysis.data && <JobAnalysisSummary analysis={jobAnalysis.data} />}
      </section>

      <section className="card" aria-labelledby="match-title">
        <h2 id="match-title">Match with your profile</h2>
        {!canMatch && workspace.data && (
          <div className="checklist">
            <p>Matching needs:</p>
            <ul>
              <li className={profile ? 'done' : ''}>
                {profile ? 'A profile' : <Link to="/profile">Create your profile</Link>}
              </li>
              <li className={profileAnalysis ? 'done' : ''}>
                {profileAnalysis ? 'A current profile analysis' : <Link to="/profile">Analyze your profile</Link>}
              </li>
              <li className={analysisReady ? 'done' : ''}>{analysisReady ? 'A current job analysis' : 'Analyze this job (above)'}</li>
            </ul>
          </div>
        )}
        <ErrorAlert error={match.error} cooldownSeconds={match.cooldownSeconds} />
        {shown && (
          <>
            <p className="muted small">
              {shown.source === 'saved' && `Saved result from ${formatDateTime(shown.matchedAt)}.`}
              {shown.source === 'cached' && 'Saved result (nothing changed since it was computed).'}
              {shown.source === 'created' && 'Freshly computed result.'}
            </p>
            <MatchResultView match={shown.data} />
          </>
        )}
        {canMatch && (
          <div className="form-actions">
            <button
              type="button"
              className="button button-primary"
              onClick={() => void match.run(Boolean(shown))}
              disabled={match.pending || match.cooldownSeconds > 0}
              aria-busy={match.pending}
            >
              {match.pending ? 'Matching…' : shown ? 'Re-run match' : 'Match with my profile'}
            </button>
          </div>
        )}
      </section>
    </>
  );
}
