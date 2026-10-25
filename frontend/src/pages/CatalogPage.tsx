import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/endpoints';
import { CatalogFilters, EMPTY_FILTERS, JobFacts, Pager, toQuery, type FilterValues } from '../components/CatalogFilters';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { LoadingState } from '../components/LoadingState';
import { PageHeader } from '../components/PageHeader';
import { useResource } from '../hooks/useResource';

export function CatalogPage() {
  const [filters, setFilters] = useState<FilterValues>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const options = useResource(() => api.catalogFilters(), []);
  const jobs = useResource(() => api.catalogJobs(toQuery(filters, page)), [filters, page]);

  return (
    <>
      <PageHeader
        title="Job pool"
        description="Open jobs collected from public company job boards. Newest first. Opening the pool never starts an analysis."
        actions={
          <Link to="/for-you" className="button button-primary">
            Jobs for me
          </Link>
        }
      />
      <CatalogFilters
        options={options.data}
        initial={filters}
        busy={jobs.loading}
        onApply={(values) => {
          setFilters(values);
          setPage(1);
        }}
      />
      {jobs.loading && !jobs.data && <LoadingState label="Loading jobs" />}
      {jobs.error !== null && (
        <>
          <ErrorAlert error={jobs.error} />
          <button type="button" className="button" onClick={jobs.reload}>
            Try again
          </button>
        </>
      )}
      {jobs.data && jobs.error === null && (
        <>
          <p className="muted" aria-live="polite">
            {jobs.data.total.toLocaleString('en-GB')} open {jobs.data.total === 1 ? 'job' : 'jobs'}
          </p>
          {jobs.data.items.length === 0 ? (
            <EmptyState title="No jobs found">Try other filters, or clear them.</EmptyState>
          ) : (
            <ul className="card-list" aria-busy={jobs.loading}>
              {jobs.data.items.map((job) => (
                <li key={job.job_id} className="card">
                  <h2 className="card-title">
                    <Link to={`/jobs/${job.job_id}`}>{job.title}</Link>
                  </h2>
                  <JobFacts job={job} />
                  <p className="small muted">{job.has_current_analysis ? 'Analyzed: it can appear in Jobs for me.' : 'Not analyzed yet: it is not ranked in Jobs for me.'}</p>
                </li>
              ))}
            </ul>
          )}
          <Pager page={jobs.data.page} pageSize={jobs.data.page_size} total={jobs.data.total} onPage={setPage} busy={jobs.loading} />
        </>
      )}
    </>
  );
}
