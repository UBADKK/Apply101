import { Link } from 'react-router-dom';
import { api } from '../api/endpoints';
import { useCurrentUser } from '../auth/AuthContext';
import { LANGUAGE_LABELS } from '../components/CoverLetterPanel';
import { EmptyState } from '../components/EmptyState';
import { ErrorAlert } from '../components/ErrorAlert';
import { formatDateTime } from '../components/labels';
import { LoadingState } from '../components/LoadingState';
import { PageHeader } from '../components/PageHeader';
import { useResource } from '../hooks/useResource';

export function LettersPage() {
  const user = useCurrentUser();
  const letters = useResource(() => api.listLetters(user.user_id), [user.user_id]);
  return (
    <>
      <PageHeader title="Motivation letters" description="Your drafts. They are visible to you and to the site administrators, never to other users. Open the job to edit, copy or download a letter." />
      {letters.loading && !letters.data && <LoadingState label="Loading your letters" />}
      {letters.error !== null && <ErrorAlert error={letters.error} />}
      {letters.data && letters.data.length === 0 && (
        <EmptyState title="No letters yet" action={<Link to="/for-you" className="button button-primary">Find a job</Link>}>
          Open a job and write a draft in English, German or Turkish.
        </EmptyState>
      )}
      {letters.data && letters.data.length > 0 && (
        <ul className="card-list">
          {letters.data.map((letter) => (
            <li key={letter.letter_id} className="card">
              <h2 className="card-title">
                {letter.job_id !== null ? <Link to={`/jobs/${letter.job_id}`}>{letter.job_title ?? 'Job'}</Link> : (letter.job_title ?? 'Job')}
              </h2>
              <p className="muted">
                {[letter.company_name, LANGUAGE_LABELS[letter.language], `${letter.version_count} version${letter.version_count === 1 ? '' : 's'}`, `updated ${formatDateTime(letter.updated_at)}`]
                  .filter(Boolean)
                  .join(' · ')}
              </p>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
