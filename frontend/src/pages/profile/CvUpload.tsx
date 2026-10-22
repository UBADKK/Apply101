import { useId, useRef, useState, type FormEvent } from 'react';
import type { Profile } from '../../api/types';
import { Alert } from '../../components/Alert';
import { ErrorAlert } from '../../components/ErrorAlert';

/** Same limit as backend/routers/profiles.py MAX_CV_UPLOAD_BYTES. */
export const MAX_CV_BYTES = 10 * 1024 * 1024;

interface CvUploadProps {
  profile: Profile;
  onUpload: (file: File) => Promise<Profile | undefined>;
  pending: boolean;
  error: unknown;
}

export function CvUpload({ profile, onUpload, pending, error }: CvUploadProps) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [uploaded, setUploaded] = useState<Profile | null>(null);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setUploaded(null);
    if (!file) {
      setLocalError('Choose a PDF file first.');
      return;
    }
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setLocalError('Only PDF files are accepted.');
      return;
    }
    if (file.size > MAX_CV_BYTES) {
      setLocalError('The file is larger than 10 MB.');
      return;
    }
    setLocalError(null);
    const result = await onUpload(file);
    if (result) {
      setUploaded(result);
      setFile(null);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  const textLength = profile.cv_text?.length ?? 0;

  return (
    <div>
      {profile.cv_filename ? (
        <div className="cv-current">
          <p>
            Current CV: <strong>{profile.cv_filename}</strong>{' '}
            <span className="muted">({textLength.toLocaleString()} characters of text extracted)</span>
          </p>
          {textLength > 0 && (
            <details>
              <summary>Show extracted text</summary>
              <pre className="cv-text">{profile.cv_text}</pre>
            </details>
          )}
          {textLength === 0 && (
            <Alert tone="warning">No text could be extracted from this PDF. Scanned documents need text, so try exporting the CV as a text PDF.</Alert>
          )}
        </div>
      ) : (
        <p className="muted">No CV uploaded yet. The analysis uses the text of your CV together with the profile above.</p>
      )}

      {uploaded && (
        <Alert tone="success" title="CV uploaded">
          {uploaded.cv_filename}: {(uploaded.cv_text?.length ?? 0).toLocaleString()} characters of text extracted.
        </Alert>
      )}
      <ErrorAlert error={error} />
      {localError && <Alert tone="error">{localError}</Alert>}

      <form className="cv-form" onSubmit={handleSubmit} aria-busy={pending}>
        <div className="field">
          <label htmlFor={inputId}>PDF file (up to 10 MB)</label>
          <input
            ref={inputRef}
            id={inputId}
            name="cv"
            type="file"
            accept="application/pdf,.pdf"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setLocalError(null);
            }}
          />
        </div>
        <button type="submit" className="button button-secondary" disabled={pending}>
          {pending ? 'Uploading…' : profile.cv_filename ? 'Replace CV' : 'Upload CV'}
        </button>
      </form>
    </div>
  );
}
