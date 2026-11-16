import { Link } from 'react-router-dom';
import { useProviderMode } from '../hooks/useProviderMode';

export const AI_DATA_PATH = '/ai-data';
export const AI_DATA_LINK_TEXT = 'How Apply101 uses AI';

export type AiDataAction = 'profile-analysis' | 'job-analysis' | 'letter-create' | 'letter-regenerate' | 'account';

/** What each action sends; keep in line with AiDataPage and the backend
 * prompts (backend/tests/test_ai_data_disclosure.py guards the labels). */
const SENT: Record<Exclude<AiDataAction, 'account'>, { action: string; data: string; extra?: string }> = {
  'profile-analysis': {
    action: 'Analyzing your profile',
    data: 'your name and email, your CV text, your languages and your profile details, such as roles, locations, visa, work authorization and residence,',
  },
  'job-analysis': {
    action: 'Analyzing this job',
    data: "the posting's title, company, location and description",
    extra: 'No profile or account data is sent.',
  },
  'letter-create': {
    action: 'Writing a draft',
    data: 'your profile facts, skills, languages and CV text, this posting and, for job-pool postings, company information',
  },
  'letter-regenerate': {
    action: 'Regenerating a draft',
    data: 'your profile facts, skills, languages and CV text, this posting and, for job-pool postings, company information',
  },
};

/** Sign-up: what the three actions send, also when an administrator starts them. */
const ACCOUNT_NOTE = {
  openai:
    'Profile analyses and letter drafts started by you or an administrator on your account send your profile data, including your CV text, to OpenAI; profile analyses also send your name and email, and job analyses send only the posting.',
  unknown:
    'Profile analyses and letter drafts started by you or an administrator on your account may send your profile data, including your CV text, to OpenAI; profile analyses may also send your name and email, and job analyses may send only the posting.',
};

/** Link to the notice; opens in a new tab so a half-filled form is kept. */
export function AiDataLink() {
  return (
    <Link to={AI_DATA_PATH} target="_blank" rel="noopener noreferrer">
      {AI_DATA_LINK_TEXT}
      <span className="visually-hidden"> (opens in a new tab)</span>
    </Link>
  );
}

/**
 * A short note next to an action that can call the AI provider, visible
 * before the click. While /meta loads it names no provider and claims no
 * demo; demo mode says nothing is sent; an unknown provider falls back to
 * "may send", never to the demo wording.
 */
export function AiDataNote({ action }: { action: AiDataAction }) {
  const mode = useProviderMode();
  const link = <AiDataLink />;
  if (mode === 'loading') {
    return (
      <p className="ai-note small" role="note">
        AI data use: {link}
      </p>
    );
  }
  if (mode === 'fake') {
    return (
      <p className="ai-note small" role="note">
        Demo mode: nothing is sent to an AI provider. {link}
      </p>
    );
  }
  if (action === 'account') {
    return (
      <p className="ai-note small" role="note">
        {ACCOUNT_NOTE[mode]} {link}
      </p>
    );
  }
  const { action: subject, data, extra } = SENT[action];
  return (
    <p className="ai-note small" role="note">
      {subject} {mode === 'openai' ? 'sends' : 'may send'} {data} to OpenAI.{extra ? ` ${extra}` : ''} {link}
    </p>
  );
}
