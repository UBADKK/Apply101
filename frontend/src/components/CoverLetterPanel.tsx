import { useEffect, useState } from 'react';
import { isApiError } from '../api/client';
import { api } from '../api/endpoints';
import type { CoverLetter, LetterLanguage, LetterVersion } from '../api/types';
import { useAction } from '../hooks/useAction';
import { useResource } from '../hooks/useResource';
import { Alert } from './Alert';
import { ErrorAlert } from './ErrorAlert';
import { formatDateTime } from './labels';

export const LANGUAGE_LABELS: Record<LetterLanguage, string> = { en: 'English', de: 'German', tr: 'Turkish' };
const SOURCE_LABELS = { profile: 'your profile', cv: 'your CV', job: 'the job posting', company: 'company information' };
const FAKE_NOTE = 'fake_analysis_provider';

export function downloadText(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function Grounding({ version }: { version: LetterVersion }) {
  const grounding = version.grounding;
  if (!grounding) return <p className="small muted">This version is your own edit.</p>;
  const used = grounding.facts.filter((fact) => grounding.used_fact_ids.includes(fact.id));
  const fake = grounding.missing_information.some((note) => note.includes(FAKE_NOTE));
  return (
    <div className="letter-grounding">
      {fake && (
        <Alert tone="info" title="Demo draft">
          <p>This draft comes from the fake demo provider: a fixed template, not written by a language model.</p>
        </Alert>
      )}
      <h4>Based on</h4>
      {used.length === 0 ? (
        <p className="small muted">The draft names none of your facts.</p>
      ) : (
        <ul className="plain-list small">
          {used.map((fact) => (
            <li key={fact.id}>
              <strong>{fact.label}</strong> — from {SOURCE_LABELS[fact.source]}
              {fact.source_url && (
                <>
                  {' '}
                  (
                  <a href={fact.source_url} target="_blank" rel="noopener noreferrer nofollow">
                    source
                  </a>
                  )
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <h4>Missing or to check</h4>
      <ul className="plain-list small">
        {grounding.gaps.map((gap) => (
          <li key={gap.code}>{gap.message}</li>
        ))}
        {grounding.missing_information
          .filter((note) => !note.includes(FAKE_NOTE))
          .map((note) => (
            <li key={note}>{note}</li>
          ))}
      </ul>
    </div>
  );
}

function LetterEditor({ userId, letter, onChange }: { userId: number; letter: CoverLetter; onChange: (letter: CoverLetter) => void }) {
  const current = letter.current_version;
  const [text, setText] = useState(current?.body_text ?? '');
  const [copied, setCopied] = useState(false);
  useEffect(() => setText(current?.body_text ?? ''), [current?.version_id]);
  const dirty = text.trim() !== (current?.body_text ?? '').trim();

  const save = useAction(async () => {
    if (!current) return undefined;
    const saved = await api.saveLetter(userId, letter.letter_id, text, current.version_id);
    onChange(saved);
    return saved;
  });
  const regenerate = useAction(async () => {
    const updated = await api.regenerateLetter(userId, letter.letter_id);
    onChange(updated);
    return updated;
  });

  const askRegenerate = () => {
    const message = dirty
      ? 'You have unsaved changes. Regenerating writes a new draft; save first if you want to keep your text. Continue without saving?'
      : 'Regenerating adds a new draft version. Your earlier versions, edited ones included, stay in the version list. Continue?';
    if (window.confirm(message)) void regenerate.run();
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  const filename = `motivation-letter-${(letter.job_title ?? 'job').toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40)}-${letter.language}.txt`;

  return (
    <div className="letter-editor">
      {(save.error !== null || regenerate.error !== null) && (
        <ErrorAlert error={save.error ?? regenerate.error} cooldownSeconds={regenerate.error ? regenerate.cooldownSeconds : save.cooldownSeconds} />
      )}
      {isApiError(save.error) && save.error.status === 409 && <p className="small">Reload the page to see the newest version, then save again.</p>}
      <div className="field">
        <label htmlFor={`letter-${letter.letter_id}`}>
          Letter ({LANGUAGE_LABELS[letter.language]}, version {current?.version_no ?? '-'}, {current?.kind === 'edited' ? 'your edit' : 'draft'})
        </label>
        <textarea id={`letter-${letter.letter_id}`} rows={16} value={text} maxLength={8000} onChange={(event) => setText(event.target.value)} />
        <p className="field-hint">Replace every [placeholder] with your own example or remove it before sending.</p>
      </div>
      <div className="form-actions">
        <button type="button" className="button button-primary" onClick={() => void save.run()} disabled={!dirty || !text.trim() || save.pending}>
          {save.pending ? 'Saving…' : 'Save as new version'}
        </button>
        <button type="button" className="button" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
        <button type="button" className="button" onClick={() => downloadText(filename, text)}>
          Download .txt
        </button>
        <button type="button" className="button button-ghost" onClick={askRegenerate} disabled={regenerate.pending || regenerate.cooldownSeconds > 0}>
          {regenerate.pending ? 'Writing…' : 'Regenerate draft'}
        </button>
      </div>
      {current && <Grounding version={current} />}
      {letter.versions && letter.versions.length > 1 && (
        <details className="letter-versions">
          <summary>All versions ({letter.versions.length})</summary>
          <ol className="plain-list small">
            {letter.versions.map((version) => (
              <li key={version.version_id}>
                Version {version.version_no} · {version.kind === 'edited' ? 'your edit' : 'draft'} · {formatDateTime(version.created_at)}{' '}
                <button type="button" className="button button-small button-ghost" onClick={() => setText(version.body_text)}>
                  Load into editor
                </button>
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}

/** Motivation letters for one job: create one per language, then edit, save,
 * copy, download and regenerate. Nothing is generated without a click. */
export function CoverLetterPanel({ userId, profileId, jobId }: { userId: number; profileId: number | null; jobId: number }) {
  const letters = useResource(async () => {
    const all = await api.listLetters(userId);
    const forJob = all.filter((letter) => letter.job_id === jobId);
    return Promise.all(forJob.map((letter) => api.getLetter(userId, letter.letter_id)));
  }, [userId, jobId]);
  const [language, setLanguage] = useState<LetterLanguage>('en');
  const [selected, setSelected] = useState<number | null>(null);

  const create = useAction(async () => {
    if (profileId === null) return undefined;
    const letter = await api.createLetter(userId, profileId, jobId, language);
    letters.setData((current) => [...(current ?? []), letter]);
    setSelected(letter.letter_id);
    return letter;
  });

  const list = letters.data ?? [];
  const active = list.find((letter) => letter.letter_id === selected) ?? list[0];
  const replace = (updated: CoverLetter) => letters.setData((current) => (current ?? []).map((letter) => (letter.letter_id === updated.letter_id ? updated : letter)));
  const existingLanguages = new Set(list.map((letter) => letter.language));

  return (
    <section className="card" aria-labelledby="letter-title">
      <h2 id="letter-title" className="section-heading">
        Motivation letter
      </h2>
      <p className="muted small">
        A first draft written only from your profile, CV, this posting and sourced company information. The writer is told to use only these
        facts and to leave [placeholders] for anything missing; still, check every sentence before you send it.
      </p>
      {letters.error !== null && <ErrorAlert error={letters.error} />}
      {profileId === null ? (
        <p>Create your profile first to write a letter.</p>
      ) : (
        <div className="import-row">
          <div className="field">
            <label htmlFor="letter-language">Language</label>
            <select id="letter-language" value={language} onChange={(event) => setLanguage(event.target.value as LetterLanguage)}>
              {(Object.keys(LANGUAGE_LABELS) as LetterLanguage[]).map((code) => (
                <option key={code} value={code}>
                  {LANGUAGE_LABELS[code]}
                  {existingLanguages.has(code) ? ' (exists)' : ''}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void create.run()}
            disabled={create.pending || create.cooldownSeconds > 0 || existingLanguages.has(language)}
          >
            {create.pending ? 'Writing…' : 'Write a draft'}
          </button>
        </div>
      )}
      {create.error !== null && <ErrorAlert error={create.error} cooldownSeconds={create.cooldownSeconds} />}
      {list.length > 1 && (
        <div className="form-actions" role="group" aria-label="Letters by language">
          {list.map((letter) => (
            <button
              key={letter.letter_id}
              type="button"
              className={`button button-small ${active?.letter_id === letter.letter_id ? 'button-primary' : ''}`}
              aria-pressed={active?.letter_id === letter.letter_id}
              onClick={() => setSelected(letter.letter_id)}
            >
              {LANGUAGE_LABELS[letter.language]}
            </button>
          ))}
        </div>
      )}
      {active && <LetterEditor key={active.letter_id} userId={userId} letter={active} onChange={replace} />}
    </section>
  );
}
