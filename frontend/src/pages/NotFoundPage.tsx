import { Link } from 'react-router-dom';
import { EmptyState } from '../components/EmptyState';
import { PageHeader } from '../components/PageHeader';

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <EmptyState
        title="There is no page at this address."
        action={
          <Link to="/" className="button">
            Go to the overview
          </Link>
        }
      />
    </>
  );
}
