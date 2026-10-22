import { isApiError } from './client';

export interface ErrorView {
  title: string;
  message: string;
  /** Seconds the server asked us to wait (429), if any. */
  retryAfterSeconds: number | null;
}

const OUTDATED_ANALYSIS_CODES: Record<string, string> = {
  ERR_PROFILE_ANALYSIS_OUTDATED_OR_MISSING:
    'Your profile has no current analysis. Analyze your profile on the Profile page, then match again.',
  ERR_JOB_ANALYSIS_OUTDATED_OR_MISSING: 'This job has no current analysis. Analyze the job first, then match again.',
};

/** Turns any thrown value into a readable title and message for the UI. */
export function describeError(error: unknown, context?: 'login'): ErrorView {
  if (!isApiError(error)) {
    return { title: 'Something went wrong', message: 'An unexpected error occurred. Please try again.', retryAfterSeconds: null };
  }
  const retryAfterSeconds = error.retryAfterSeconds;
  switch (error.status) {
    case 0:
      return { title: 'Server unreachable', message: error.message, retryAfterSeconds };
    case 400:
      if (error.code && OUTDATED_ANALYSIS_CODES[error.code]) {
        return { title: 'Analysis needed', message: OUTDATED_ANALYSIS_CODES[error.code], retryAfterSeconds };
      }
      return { title: 'Request not accepted', message: error.message, retryAfterSeconds };
    case 401:
      return context === 'login'
        ? { title: 'Login failed', message: 'Invalid email or password.', retryAfterSeconds }
        : { title: 'Session ended', message: 'Your session has expired. Please log in again.', retryAfterSeconds };
    case 403:
      return { title: 'Not allowed', message: error.message || 'You are not allowed to do this.', retryAfterSeconds };
    case 404:
      return { title: 'Not found', message: error.message || 'This item does not exist or is not yours.', retryAfterSeconds };
    case 409:
      return { title: 'Conflict', message: error.message, retryAfterSeconds };
    case 413:
      return { title: 'File too large', message: error.message, retryAfterSeconds };
    case 422:
      return { title: 'Check your input', message: formatFieldErrors(error.fieldErrors) || error.message, retryAfterSeconds };
    case 429:
      return {
        title: 'Please wait',
        message:
          retryAfterSeconds !== null
            ? `Too many requests. You can try again in ${retryAfterSeconds} seconds.`
            : 'Too many requests. Please wait a moment before trying again.',
        retryAfterSeconds,
      };
    case 503:
      return {
        title: 'Service unavailable',
        message: `${error.message} Nothing was saved; try again later.`,
        retryAfterSeconds,
      };
    default:
      if (error.status >= 500) {
        return { title: 'Server error', message: 'The server could not complete the request. Please try again later.', retryAfterSeconds };
      }
      return { title: 'Request failed', message: error.message, retryAfterSeconds };
  }
}

function formatFieldErrors(fieldErrors: Record<string, string>): string {
  return Object.entries(fieldErrors)
    .map(([field, message]) => (field === 'form' ? message : `${humanizeField(field)}: ${message}`))
    .join(' ');
}

export function humanizeField(field: string): string {
  const text = field.replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}
