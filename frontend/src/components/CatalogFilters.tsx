import { useState, type FormEvent } from 'react';
import type { CatalogFilterOptions, CatalogJob, CatalogQuery } from '../api/types';
import { formatDateTime } from './labels';

const WORK_MODE_LABELS: Record<string, string> = { remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site' };

export interface FilterValues {
  q: string;
  country: string;
  work_mode: string;
  source: string;
}

export const EMPTY_FILTERS: FilterValues = { q: '', country: '', work_mode: '', source: '' };

export function toQuery(filters: FilterValues, page: number): CatalogQuery {
  return { q: filters.q.trim() || undefined, country: filters.country || undefined, work_mode: filters.work_mode || undefined, source: filters.source || undefined, page };
}

/** Search and filters of the shared pool; applied on submit, not per key. */
export function CatalogFilters({ options, initial, onApply, busy }: { options: CatalogFilterOptions | undefined; initial: FilterValues; onApply: (values: FilterValues) => void; busy: boolean }) {
  const [values, setValues] = useState(initial);
  const set = (name: keyof FilterValues) => (value: string) => setValues((current) => ({ ...current, [name]: value }));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onApply(values);
  };
  return (
    <form className="card filter-bar" onSubmit={submit} role="search" aria-label="Filter jobs">
      <div className="field">
        <label htmlFor="filter-q">Search</label>
        <input id="filter-q" name="q" type="search" value={values.q} maxLength={100} placeholder="Title, company or place" onChange={(event) => set('q')(event.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="filter-country">Country</label>
        <select id="filter-country" value={values.country} onChange={(event) => set('country')(event.target.value)}>
          <option value="">Any</option>
          {(options?.countries ?? []).map((country) => (
            <option key={country.country_code} value={country.country_code}>
              {country.country_code === 'unknown' ? 'Not stated by the source' : country.country_code} ({country.count})
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="filter-work-mode">Work mode</label>
        <select id="filter-work-mode" value={values.work_mode} onChange={(event) => set('work_mode')(event.target.value)}>
          <option value="">Any</option>
          {(options?.work_modes ?? ['remote', 'hybrid', 'onsite']).map((mode) => (
            <option key={mode} value={mode}>
              {WORK_MODE_LABELS[mode] ?? mode}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="filter-source">Source</label>
        <select id="filter-source" value={values.source} onChange={(event) => set('source')(event.target.value)}>
          <option value="">Any</option>
          {(options?.sources ?? []).map((source) => (
            <option key={source.source} value={source.source}>
              {source.label} ({source.count})
            </option>
          ))}
        </select>
      </div>
      <div className="form-actions">
        <button type="submit" className="button button-primary" disabled={busy}>
          Apply
        </button>
        <button
          type="button"
          className="button button-ghost"
          onClick={() => {
            setValues(EMPTY_FILTERS);
            onApply(EMPTY_FILTERS);
          }}
        >
          Clear
        </button>
      </div>
    </form>
  );
}

export function JobFacts({ job }: { job: CatalogJob }) {
  const place = [job.location, job.country_code && !job.location?.includes(job.country_code) ? job.country_code : null].filter(Boolean).join(' · ');
  const seen = job.last_seen_at ?? job.first_seen_at;
  return (
    <>
      <p className="muted">{[job.company_name, place].filter(Boolean).join(' · ') || 'No company or location given'}</p>
      <p className="chips">
        {job.work_mode && <span className="chip">{WORK_MODE_LABELS[job.work_mode] ?? job.work_mode}</span>}
        <span className="chip">{job.source_label ?? job.source ?? 'Unknown source'}</span>
        {seen && <span className="chip">Last seen {formatDateTime(seen)}</span>}
      </p>
      {job.remote_note && <p className="small muted">{job.remote_note}</p>}
      <p className="small">
        <a href={job.url} target="_blank" rel="noopener noreferrer nofollow">
          Original posting<span className="visually-hidden"> (opens in a new tab)</span>
        </a>
        {job.attribution && (
          <span className="muted">
            {' · '}
            {job.attribution_url ? (
              <a href={job.attribution_url} target="_blank" rel="noopener noreferrer">
                {job.attribution}
              </a>
            ) : (
              job.attribution
            )}
          </span>
        )}
      </p>
    </>
  );
}

export function Pager({ page, pageSize, total, onPage, busy }: { page: number; pageSize: number; total: number; onPage: (page: number) => void; busy: boolean }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) return null;
  return (
    <nav className="pager" aria-label="Pages">
      <button type="button" className="button button-small" disabled={busy || page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span>
        Page {page} of {pages}
      </span>
      <button type="button" className="button button-small" disabled={busy || page >= pages} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </nav>
  );
}
