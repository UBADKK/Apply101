import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { useProviderMode, type ProviderMode } from '../hooks/useProviderMode';

/** Which provider this deployment uses, from GET /meta. Demo mode and the
 * wait for the answer must never read as if data went to OpenAI; an unknown
 * answer assumes it may. */
function ModeNote({ mode }: { mode: ProviderMode }) {
  if (mode === 'loading') {
    return (
      <div className="alert alert-info" role="note">
        <p>Checking which provider this deployment uses…</p>
      </div>
    );
  }
  if (mode === 'fake') {
    return (
      <div className="alert alert-info" role="note">
        <p className="alert-title">Demo mode: nothing is sent to an AI provider.</p>
        <p>
          This deployment currently runs with a deterministic fake provider: analyses and letter drafts are produced on the Apply101 server
          without any AI model. The rest of this page describes what happens when the real provider is enabled.
        </p>
      </div>
    );
  }
  if (mode === 'openai') {
    return (
      <div className="alert alert-info" role="note">
        <p>This deployment uses OpenAI for the actions described below.</p>
      </div>
    );
  }
  return (
    <div className="alert alert-info" role="note">
      <p>Apply101 could not confirm which provider this deployment uses, so this page assumes that your data may be sent to OpenAI.</p>
    </div>
  );
}

/** Public notice on which data an AI provider may receive, when and why.
 * Keep in line with the backend prompts: backend/tests/test_ai_data_disclosure.py
 * fails when the data sent changes. */
export function AiDataPage() {
  const mode = useProviderMode();
  // Present tense only with the real provider; "may" when unknown; demo mode
  // and the wait for /meta describe the real provider conditionally.
  const framing = mode === 'openai' ? 'present' : mode === 'unknown' ? 'may' : 'conditional';
  return (
    <>
      <PageHeader title="How Apply101 uses AI" description="Which of your data an AI provider may receive, when, and why." />
      <ModeNote mode={mode} />

      <section className="card prose" aria-labelledby="ai-who">
        <h2 id="ai-who">Who receives your data</h2>
        {framing === 'present' && (
          <p>
            Apply101 uses <strong>OpenAI</strong>, through its API, to analyze profiles and job postings and to write motivation letter drafts.
            OpenAI is a third party: for these actions, Apply101 sends it the data listed below.
          </p>
        )}
        {framing === 'may' && (
          <p>
            Apply101 may use <strong>OpenAI</strong>, through its API, to analyze profiles and job postings and to write motivation letter
            drafts. OpenAI is a third party: for these actions, Apply101 may send it the data listed below.
          </p>
        )}
        {framing === 'conditional' && (
          <p>
            When the real provider is enabled, Apply101 uses <strong>OpenAI</strong>, through its API, to analyze profiles and job postings and
            to write motivation letter drafts. OpenAI is a third party: for these actions, Apply101 then sends it the data listed below.
          </p>
        )}
      </section>

      <section className="card prose" aria-labelledby="ai-when">
        <h2 id="ai-when">When data is sent</h2>
        <p>
          {framing === 'conditional' ? 'When the real provider is enabled, data is sent only' : 'Only'} when you, or an Apply101 administrator
          acting on your account, start one of these actions:
        </p>
        <ul>
          <li>analyzing your profile, or re-running that analysis;</li>
          <li>analyzing a job;</li>
          <li>writing or regenerating a motivation letter draft.</li>
        </ul>
        <p>
          Nothing is sent to an AI provider when you sign up or log in, upload a CV (its text is extracted on the Apply101 server), edit your
          profile, browse the job pool, run a match or view recommendations. When a current analysis already exists, Apply101 reuses it and
          sends nothing again; re-running an analysis or regenerating a draft sends the data again.
        </p>
      </section>

      <section className="card prose" aria-labelledby="ai-what">
        <h2 id="ai-what">What is sent</h2>
        <h3>Profile analysis</h3>
        <ul>
          <li>
            From your account: your <strong>name and email address</strong>, and the skills, years of experience, field of study and degrees
            (master, PhD, Abitur) stored with it.
          </li>
          <li>
            From your profile: profile name, target and secondary target role, preferred and excluded roles, target locations, preferred work
            type, preferred technologies and extra preferences.
          </li>
          <li>
            Your <strong>visa sponsorship</strong> need, <strong>work authorization</strong> status, relocation preference, country of{' '}
            <strong>residence</strong> and student status.
          </li>
          <li>Your years of experience and target seniority.</li>
          <li>
            Your <strong>languages</strong> and their levels.
          </li>
          <li>Your self description.</li>
          <li>
            The text extracted from your <strong>CV</strong> (the beginning of it if it is very long). The PDF file itself is not sent.
          </li>
        </ul>
        <h3>Job analysis</h3>
        <ul>
          <li>
            The <strong>job posting</strong>: its title, company name, location and description (the beginning of it if it is very long). For a
            job you added, this is the text you entered or imported.
          </li>
          <li>No profile or account data.</li>
        </ul>
        <h3>Motivation letter (write or regenerate a draft)</h3>
        <ul>
          <li>
            From your profile: target role, years of experience, self description, preferred technologies, the skills and tools found by your
            profile analysis, and your languages and their levels.
          </li>
          <li>The beginning of your CV text.</li>
          <li>The job's title, company, location, the beginning of its description and the required skills found by the job analysis.</li>
          <li>
            For job-pool postings only: <strong>company information</strong> from Apply101's company list (the company's name, website and
            description).
          </li>
        </ul>
        <p>
          Your name and email are not added as separate fields to a letter request, but your CV text may contain them, as well as your phone
          number, address or other personal details.
        </p>
      </section>

      <section className="card prose" aria-labelledby="ai-why">
        <h2 id="ai-why">Why</h2>
        <p>
          To turn your profile and job postings into structured signals (roles, skills, languages, requirements) that Apply101's matching
          compares, and to write a first draft of a motivation letter. The matching itself runs on the Apply101 server and calls no AI.
        </p>
      </section>

      <section className="card prose" aria-labelledby="ai-keeps">
        <h2 id="ai-keeps">What Apply101 keeps</h2>
        <p>Apply101 stores data in your account, including:</p>
        <ul>
          <li>your account and profile data;</li>
          <li>your uploaded CV file and the text extracted from it;</li>
          <li>profile and job analyses and your matches;</li>
          <li>motivation letter drafts and their versions.</li>
        </ul>
        <p>Apply101 administrators can access this data. No retention period has been defined yet.</p>
      </section>

      <section className="card prose" aria-labelledby="ai-limits">
        <h2 id="ai-limits">Limits of this notice</h2>
        <ul>
          <li>How OpenAI stores or uses the data it receives is governed by OpenAI's own terms, not by Apply101.</li>
          <li>This page is an information notice. It is not a complete privacy policy.</li>
          <li>
            Leave out of your CV and profile anything you do not want to share, for example contact details such as your phone number or home
            address.
          </li>
        </ul>
      </section>

      <p>
        <Link to="/">Back to Apply101</Link>
      </p>
    </>
  );
}
