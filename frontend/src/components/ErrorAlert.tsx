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
  const countdown = cooldownSeconds !== undefined && view.retryAfterSeconds !== null;
  let message = view.message;
  if (countdown && cooldownSeconds > 0) message = `Too many requests. You can try again in ${cooldownSeconds} seconds.`;
  else if (countdown) message = 'You can try again now.';
  return (
    <Alert tone={view.retryAfterSeconds !== null ? 'warning' : 'error'} title={view.title}>
      <p>{message}</p>
    </Alert>
  );
}
