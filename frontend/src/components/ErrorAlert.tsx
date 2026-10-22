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
  const waiting = cooldownSeconds !== undefined && cooldownSeconds > 0;
  return (
    <Alert tone={view.retryAfterSeconds !== null ? 'warning' : 'error'} title={view.title}>
      <p>
        {waiting ? `Too many requests. You can try again in ${cooldownSeconds} seconds.` : view.message}
      </p>
    </Alert>
  );
}
