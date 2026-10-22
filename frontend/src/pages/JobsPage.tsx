import { Link } from 'react-router-dom';
import { api } from '../api/endpoints';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { LoadingState } from '../components/LoadingState';
import { PageHeader } from '../components/PageHeader';
import { useResource } from '../hooks/useResource';

export function JobsPage() {
  const jobs = useResource(() => api.listOwnJobs(), []);
  const addLink = (
    <Link to="/jobs/new" className="button button-primary">
      Add a job
    </Link>
  );

  return (
    <>
      <PageHeader title="Your jobs" description="Job postings you added. Only you can see them." actions={addLink} />
      {jobs.loading && !jobs.data && <LoadingState label="Loading your jobs" />}
      {jobs.error !== null && !jobs.data && (
        <>
          <ErrorAlert error={jobs.error} />
          <button type="button" className="button" onClick={jobs.reload}>
            Try again
          </button>
        </>
      )}
      {jobs.data && jobs.data.length === 0 && (
        <EmptyState title="No jobs yet" action={addLink}>
          Paste a job posting with its title, link and description, then analyze it and match it against your profile.
        </EmptyState>
      )}
      {jobs.data && jobs.data.length > 0 && (
        <ul className="card-list">
          {jobs.data.map((job) => (
            <li key={job.job_id} className="card card-link">
              <h2 className="card-title">
                <Link to={`/jobs/${job.job_id}`}>{job.title}</Link>
              </h2>
              <p className="muted">{[job.company_name, job.location].filter(Boolean).join(' · ') || 'No company or location given'}</p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
