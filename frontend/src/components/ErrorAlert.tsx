import { isApiError } from '../api/client';
import { describeError } from '../api/errors';
import { Alert } from './Alert';

interface ErrorAlertProps {
  error: unknown;
  context?: 'login';
  /** Live countdown for 429 responses, from useAction. */
  cooldownSeconds?: number;
}

export function ErrorAlert({ error, context, cooldownSeconds }: ErrorAlertProps) {
  if (!error) return null;
  const view = describeError(error, context);
  const countdown = cooldownSeconds !== undefined && isApiError(error) && error.status === 429 && view.retryAfterSeconds !== null;
  if (countdown && cooldownSeconds === 0) {
    return (
      <Alert tone="info" title="You can try again">
        <p>The waiting time is over. You can send the request again now.</p>
      </Alert>
    );
  }
  const message = countdown ? `Too many requests. You can try again in ${cooldownSeconds} seconds.` : view.message;
  return (
    <Alert tone={view.retryAfterSeconds !== null ? 'warning' : 'error'} title={view.title}>
      <p>{message}</p>
    </Alert>
  );
}
