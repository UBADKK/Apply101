import { useId, type ChangeEvent, type ReactNode } from 'react';

interface BaseFieldProps {
  label: string;
  name: string;
  hint?: ReactNode;
  error?: string;
  required?: boolean;
}

function describedBy(hintId: string, errorId: string, hint: ReactNode, error?: string): string | undefined {
  const ids = [hint ? hintId : null, error ? errorId : null].filter(Boolean);
  return ids.length ? ids.join(' ') : undefined;
}

function FieldFrame({
  id,
  label,
  hint,
  error,
  required,
  hintId,
  errorId,
  children,
}: BaseFieldProps & { id: string; hintId: string; errorId: string; children: ReactNode }) {
  return (
    <div className={`field${error ? ' field-invalid' : ''}`}>
      <label htmlFor={id}>
        {label}
        {required && <span className="required-mark"> (required)</span>}
      </label>
      {children}
      {hint && (
        <p className="field-hint" id={hintId}>
          {hint}
        </p>
      )}
      {error && (
        <p className="field-error" id={errorId}>
          {error}
        </p>
      )}
    </div>
  );
}

interface TextFieldProps extends BaseFieldProps {
  value: string;
  onChange: (value: string) => void;
  type?: 'text' | 'email' | 'password' | 'url' | 'number';
  autoComplete?: string;
  placeholder?: string;
  maxLength?: number;
  min?: number;
  step?: number | 'any';
}

export function TextField({ label, name, hint, error, required, value, onChange, type = 'text', ...rest }: TextFieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  return (
    <FieldFrame id={id} label={label} name={name} hint={hint} error={error} required={required} hintId={hintId} errorId={errorId}>
      <input
        id={id}
        name={name}
        type={type}
        value={value}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(hintId, errorId, hint, error)}
        onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value)}
        {...rest}
      />
    </FieldFrame>
  );
}

interface TextAreaFieldProps extends BaseFieldProps {
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  maxLength?: number;
  placeholder?: string;
}

export function TextAreaField({ label, name, hint, error, required, value, onChange, rows = 4, ...rest }: TextAreaFieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  return (
    <FieldFrame id={id} label={label} name={name} hint={hint} error={error} required={required} hintId={hintId} errorId={errorId}>
      <textarea
        id={id}
        name={name}
        rows={rows}
        value={value}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(hintId, errorId, hint, error)}
        onChange={(event: ChangeEvent<HTMLTextAreaElement>) => onChange(event.target.value)}
        {...rest}
      />
    </FieldFrame>
  );
}

export interface Option {
  value: string;
  label: string;
}

interface SelectFieldProps extends BaseFieldProps {
  value: string;
  onChange: (value: string) => void;
  options: Option[];
}

/** A stored value outside the option list (e.g. written through the API)
 * stays selectable as itself, so saving the form never silently replaces it
 * with the first option. */
export function withCurrentValue(options: Option[], value: string, currentLabel?: string): Option[] {
  if (options.some((option) => option.value === value)) return options;
  return [...options, { value, label: currentLabel ?? `${value} (current value)` }];
}

export function SelectField({ label, name, hint, error, required, value, onChange, options }: SelectFieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  options = withCurrentValue(options, value);
  return (
    <FieldFrame id={id} label={label} name={name} hint={hint} error={error} required={required} hintId={hintId} errorId={errorId}>
      <select
        id={id}
        name={name}
        value={value}
        required={required}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(hintId, errorId, hint, error)}
        onChange={(event: ChangeEvent<HTMLSelectElement>) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </FieldFrame>
  );
}
