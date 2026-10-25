import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/endpoints';
import type { RecommendationCoverage } from '../api/types';
import { useCurrentUser } from '../auth/AuthContext';
import { Alert } from '../components/Alert';
import { CatalogFilters, EMPTY_FILTERS, JobFacts, Pager, toQuery, type FilterValues } from '../components/CatalogFilters';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { LoadingState } from '../components/LoadingState';
import { EligibilityBadge } from '../components/MatchResultView';
import { PageHeader } from '../components/PageHeader';
import { useResource } from '../hooks/useResource';
import { useWorkspace } from '../hooks/useWorkspace';

function CoverageNote({ coverage }: { coverage: RecommendationCoverage }) {
  return (
    <Alert tone="info" title="What this list covers">
      <p>
        Ranked {coverage.evaluated_jobs.toLocaleString('en-GB')} of {coverage.active_jobs.toLocaleString('en-GB')} open jobs in the pool that
        match the filters.
        {coverage.not_analyzed_jobs > 0 || coverage.capped
          ? ` ${coverage.not_analyzed_jobs.toLocaleString('en-GB')} are not analyzed yet and are not ranked, so this is not the best of the whole pool.`
          : ' Every open job that matches the filters was ranked.'}
        {coverage.capped && ` Only the newest ${coverage.pool_limit.toLocaleString('en-GB')} analyzed jobs were ranked.`}
      </p>
      <p className="small">
        Results: {coverage.status_counts.eligible ?? 0} eligible, {coverage.status_counts.review_required ?? 0} need review,{' '}
        {coverage.status_counts.ineligible ?? 0} not eligible. Unknown details lead to “needs review”, never to a rejection.
      </p>
    </Alert>
  );
}

export function RecommendationsPage() {
  const user = useCurrentUser();
  const workspace = useWorkspace();
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [includeIneligible, setIncludeIneligible] = useState(false);
  const options = useResource(() => api.catalogFilters(), []);
  const profile = workspace.data?.profile ?? null;
  const analyzed = Boolean(workspace.data?.analysis);
  const results = useResource(
    async () => (profile && analyzed ? api.recommendations(user.user_id, profile.profile_id, { ...toQuery(filters, page), include_ineligible: includeIneligible }) : null),
    [profile?.profile_id, analyzed, filters, page, includeIneligible, user.user_id],
  );

  return (
    <>
      <PageHeader title="Jobs for me" description="Analyzed open jobs from the pool, ranked against your analyzed profile with the same rules as a single match." />
      {workspace.loading && !workspace.data && <LoadingState label="Loading your profile" />}
      {workspace.error !== null && <ErrorAlert error={workspace.error} />}
      {workspace.data && !profile && (
        <EmptyState title="Create your profile first" action={<Link to="/profile" className="button button-primary">Go to profile</Link>}>
          Recommendations need your profile and its analysis.
        </EmptyState>
      )}
      {workspace.data && profile && !analyzed && (
        <EmptyState title="Analyze your profile first" action={<Link to="/profile" className="button button-primary">Go to profile</Link>}>
          Recommendations use your current profile analysis.
        </EmptyState>
      )}
      {profile && analyzed && (
        <>
          <CatalogFilters
            options={options.data}
            initial={filters}
            busy={results.loading}
            onApply={(values) => {
              setFilters(values);
              setPage(1);
            }}
          />
          <label className="checkbox">
            <input
              type="checkbox"
              checked={includeIneligible}
              onChange={(event) => {
                setIncludeIneligible(event.target.checked);
                setPage(1);
              }}
            />
            Also show jobs I am not eligible for
          </label>
          {results.loading && !results.data && <LoadingState label="Ranking jobs" />}
          {results.error !== null && (
            <>
              <ErrorAlert error={results.error} />
              <button type="button" className="button" onClick={results.reload}>
                Try again
              </button>
            </>
          )}
          {results.data && results.error === null && (
            <>
              <CoverageNote coverage={results.data.coverage} />
              {results.data.items.length === 0 ? (
                <EmptyState title="No ranked jobs">No analyzed open job matches these filters yet.</EmptyState>
              ) : (
                <ol className="card-list" aria-busy={results.loading}>
                  {results.data.items.map((item) => (
                    <li key={item.job_id} className="card">
                      <div className="match-headline">
                        <h2 className="card-title">
                          <Link to={`/jobs/${item.job_id}`}>{item.title}</Link>
                        </h2>
                        <EligibilityBadge status={item.eligibility_status} />
                        <span className="score" aria-label={`Score ${item.overall_score} of 100`}>
                          {item.overall_score}
                        </span>
                      </div>
                      <JobFacts job={item} />
                      <p>{item.summary}</p>
                      {[...item.hard_fail_reasons, ...item.review_reasons].length > 0 && (
                        <ul className="reason-list">
                          {[...item.hard_fail_reasons, ...item.review_reasons].map((reason, index) => (
                            <li key={`${reason.code}-${index}`}>
                              {reason.message} <span className="reason-code">({reason.code})</span>
                            </li>
                          ))}
                        </ul>
                      )}
                      {item.strengths.length > 0 && <p className="small">Strengths: {item.strengths.join(' ')}</p>}
                      {item.weaknesses.length > 0 && <p className="small muted">Watch out: {item.weaknesses.join(' ')}</p>}
                    </li>
                  ))}
                </ol>
              )}
              <Pager page={results.data.page} pageSize={results.data.page_size} total={results.data.total} onPage={setPage} busy={results.loading} />
            </>
          )}
        </>
      )}
    </>
  );
}
