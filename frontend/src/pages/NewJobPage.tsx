import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { isApiError } from '../api/client';
import { api } from '../api/endpoints';
import type { JobInput } from '../api/types';
import { ErrorAlert } from '../components/ErrorAlert';
import { TextAreaField, TextField } from '../components/fields';
import { PageHeader } from '../components/PageHeader';
import { useAction } from '../hooks/useAction';

/** Same limits as backend/app/schemas.py (MANUAL_JOB_*). */
export const JOB_LIMITS = { title: 300, optional: 200, url: 2048, descriptionMin: 50, descriptionMax: 12000 };

type Errors = Partial<Record<keyof JobInput, string>>;

export function validateJob(input: JobInput): Errors {
  const errors: Errors = {};
  if (!input.title.trim()) errors.title = 'Enter the job title.';
  else if (input.title.trim().length > JOB_LIMITS.title) errors.title = `Use at most ${JOB_LIMITS.title} characters.`;
  if (input.company_name && input.company_name.length > JOB_LIMITS.optional) errors.company_name = `Use at most ${JOB_LIMITS.optional} characters.`;
  if (input.location && input.location.length > JOB_LIMITS.optional) errors.location = `Use at most ${JOB_LIMITS.optional} characters.`;
  const url = input.url.trim();
  if (!url) errors.url = 'Enter the link to the job posting.';
  else {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') errors.url = 'Use an http or https link.';
    } catch {
      errors.url = 'Enter a full link, starting with https://.';
    }
    if (!errors.url && url.length > JOB_LIMITS.url) errors.url = `Use at most ${JOB_LIMITS.url} characters.`;
  }
  const length = input.description_text.trim().length;
  if (length < JOB_LIMITS.descriptionMin) errors.description_text = `Paste the job description (at least ${JOB_LIMITS.descriptionMin} characters).`;
  else if (length > JOB_LIMITS.descriptionMax) errors.description_text = `Use at most ${JOB_LIMITS.descriptionMax} characters.`;
  return errors;
}

export function NewJobPage() {
  const navigate = useNavigate();
  const [title, setTitle] = useState('');
  const [company, setCompany] = useState('');
  const [location, setLocation] = useState('');
  const [url, setUrl] = useState('');
  const [description, setDescription] = useState('');
  const [errors, setErrors] = useState<Errors>({});

  const create = useAction(async (input: JobInput) => {
    const job = await api.createJob(input);
    navigate(`/jobs/${job.job_id}`, { state: { created: true } });
    return job;
  });

  const serverError = create.error;
  const serverFieldErrors = isApiError(serverError) ? serverError.fieldErrors : {};
  const duplicateUrl = isApiError(serverError) && serverError.status === 409 ? serverError.message : undefined;
  const fieldError = (name: keyof JobInput) => errors[name] ?? serverFieldErrors[name];

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const input: JobInput = {
      title: title.trim(),
      company_name: company.trim() || null,
      location: location.trim() || null,
      url: url.trim(),
      description_text: description.trim(),
    };
    const next = validateJob(input);
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    void create.run(input);
  };

  return (
    <>
      <PageHeader title="Add a job" description="Copy the posting from the company's site. The link is stored for reference; the text you paste is what gets analyzed." />
      <section className="card">
        {serverError !== null && !duplicateUrl && <ErrorAlert error={serverError} cooldownSeconds={create.cooldownSeconds} />}
        <form onSubmit={handleSubmit} noValidate aria-busy={create.pending}>
          <div className="form-grid">
            <TextField label="Job title" name="title" required value={title} onChange={setTitle} maxLength={JOB_LIMITS.title} error={fieldError('title')} />
            <TextField label="Link to the posting" name="url" type="url" required value={url} onChange={setUrl} placeholder="https://" error={fieldError('url') ?? duplicateUrl} />
            <TextField label="Company" name="company_name" value={company} onChange={setCompany} maxLength={JOB_LIMITS.optional} error={fieldError('company_name')} />
            <TextField label="Location" name="location" value={location} onChange={setLocation} maxLength={JOB_LIMITS.optional} error={fieldError('location')} />
          </div>
          <TextAreaField
            label="Job description"
            name="description_text"
            required
            rows={12}
            value={description}
            onChange={setDescription}
            maxLength={JOB_LIMITS.descriptionMax}
            hint={`${description.trim().length.toLocaleString()} / ${JOB_LIMITS.descriptionMax.toLocaleString()} characters (at least ${JOB_LIMITS.descriptionMin}).`}
            error={fieldError('description_text')}
          />
          <div className="form-actions">
            <button type="submit" className="button button-primary" disabled={create.pending}>
              {create.pending ? 'Saving…' : 'Save job'}
            </button>
            <Link to="/jobs" className="button button-ghost">
              Cancel
            </Link>
          </div>
        </form>
      </section>
    </>
  );
}
