import { useState, type FormEvent } from 'react';
import { isApiError } from '../../api/client';
import type { Language, Profile, ProfileInput, StudentStatus, WorkAuthorization } from '../../api/types';
import { ErrorAlert } from '../../components/ErrorAlert';
import { SelectField, TextAreaField, TextField, type Option } from '../../components/fields';

export const LANGUAGE_OPTIONS: Option[] = [
  ['en', 'English'],
  ['de', 'German'],
  ['tr', 'Turkish'],
  ['fr', 'French'],
  ['es', 'Spanish'],
  ['it', 'Italian'],
  ['pt', 'Portuguese'],
  ['nl', 'Dutch'],
  ['pl', 'Polish'],
  ['sv', 'Swedish'],
  ['da', 'Danish'],
  ['fi', 'Finnish'],
  ['cs', 'Czech'],
  ['ro', 'Romanian'],
  ['el', 'Greek'],
  ['uk', 'Ukrainian'],
  ['ru', 'Russian'],
  ['ar', 'Arabic'],
  ['hi', 'Hindi'],
  ['zh', 'Chinese'],
  ['ja', 'Japanese'],
  ['ko', 'Korean'],
].map(([value, label]) => ({ value, label }));

const LEVEL_OPTIONS: Option[] = [
  { value: 'A1', label: 'A1 (beginner)' },
  { value: 'A2', label: 'A2 (elementary)' },
  { value: 'B1', label: 'B1 (intermediate)' },
  { value: 'B2', label: 'B2 (upper intermediate)' },
  { value: 'C1', label: 'C1 (advanced)' },
  { value: 'C2', label: 'C2 (proficient)' },
  { value: 'native', label: 'Native' },
];

const WORK_TYPE_OPTIONS: Option[] = [
  { value: '', label: 'No preference' },
  { value: 'remote', label: 'Remote' },
  { value: 'hybrid', label: 'Hybrid' },
  { value: 'onsite', label: 'On-site' },
];

const SENIORITY_OPTIONS: Option[] = [
  { value: '', label: 'Not specified' },
  { value: 'intern', label: 'Intern' },
  { value: 'junior', label: 'Junior' },
  { value: 'mid', label: 'Mid-level' },
  { value: 'senior', label: 'Senior' },
  { value: 'lead', label: 'Lead' },
];

const WORK_AUTHORIZATION_OPTIONS: Option[] = [
  { value: '', label: 'Not specified' },
  { value: 'germany', label: 'Authorized to work in Germany' },
  { value: 'eu_eea', label: 'EU/EEA work rights' },
  { value: 'other_country_only', label: 'Authorized only in another country' },
  { value: 'none', label: 'No work authorization' },
  { value: 'unknown', label: 'Not sure' },
];

const VISA_OPTIONS: Option[] = [
  { value: '', label: 'Not specified' },
  { value: 'yes', label: 'Yes, I need sponsorship' },
  { value: 'no', label: 'No, I do not need sponsorship' },
];

const STUDENT_OPTIONS: Option[] = [
  { value: '', label: 'Not specified' },
  { value: 'currently_enrolled', label: 'Currently enrolled' },
  { value: 'not_enrolled', label: 'Not enrolled' },
  { value: 'unknown', label: 'Not sure' },
];

interface LanguageRow {
  key: number;
  code: string;
  level: string;
  primary: boolean;
}

interface FormState {
  self_description: string;
  target_role: string;
  secondary_target_role: string;
  target_location: string;
  preferred_work_type: string;
  preferred_technologies: string;
  extra_preferences: string;
  years_of_experience: string;
  seniority_target: string;
  work_authorization_status: string;
  visa_sponsorship_needed: string;
  current_residence_country: string;
  relocation_preference: string;
  student_status: string;
}

function initialState(profile: Profile | null): FormState {
  const text = (value: string | null | undefined) => value ?? '';
  return {
    self_description: text(profile?.self_description),
    target_role: text(profile?.target_role),
    secondary_target_role: text(profile?.secondary_target_role),
    target_location: text(profile?.target_location),
    preferred_work_type: text(profile?.preferred_work_type),
    preferred_technologies: text(profile?.preferred_technologies),
    extra_preferences: text(profile?.extra_preferences),
    years_of_experience: profile?.years_of_experience != null ? String(profile.years_of_experience) : '',
    seniority_target: text(profile?.seniority_target),
    work_authorization_status: text(profile?.work_authorization_status),
    visa_sponsorship_needed:
      profile?.visa_sponsorship_needed === true ? 'yes' : profile?.visa_sponsorship_needed === false ? 'no' : '',
    current_residence_country: text(profile?.current_residence_country),
    relocation_preference: text(profile?.relocation_preference),
    student_status: text(profile?.student_status),
  };
}

let nextRowKey = 1;

function initialRows(languages: Language[]): LanguageRow[] {
  return languages.map((language) => ({
    key: nextRowKey++,
    code: language.language_code,
    level: language.proficiency_level ?? 'B2',
    primary: Boolean(language.is_primary),
  }));
}

function optional(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function toProfileInput(state: FormState, rows: LanguageRow[]): ProfileInput {
  const years = state.years_of_experience.trim();
  return {
    self_description: optional(state.self_description),
    target_role: optional(state.target_role),
    secondary_target_role: optional(state.secondary_target_role),
    target_location: optional(state.target_location),
    preferred_work_type: optional(state.preferred_work_type),
    preferred_technologies: optional(state.preferred_technologies),
    extra_preferences: optional(state.extra_preferences),
    years_of_experience: years ? Number(years) : null,
    seniority_target: optional(state.seniority_target),
    work_authorization_status: (optional(state.work_authorization_status) as WorkAuthorization | null) ?? null,
    visa_sponsorship_needed: state.visa_sponsorship_needed === 'yes' ? true : state.visa_sponsorship_needed === 'no' ? false : null,
    current_residence_country: optional(state.current_residence_country),
    relocation_preference: optional(state.relocation_preference),
    student_status: (optional(state.student_status) as StudentStatus | null) ?? null,
    languages: rows.map((row) => ({
      language_code: row.code,
      language_name: LANGUAGE_OPTIONS.find((option) => option.value === row.code)?.label ?? row.code,
      proficiency_level: row.level,
      proficiency_scale: 'CEFR',
      is_primary: row.primary,
    })),
  };
}

interface ProfileFormProps {
  profile: Profile | null;
  languages: Language[];
  onSave: (input: ProfileInput) => Promise<unknown>;
  pending: boolean;
  error: unknown;
}

export function ProfileForm({ profile, languages, onSave, pending, error }: ProfileFormProps) {
  const [state, setState] = useState<FormState>(() => initialState(profile));
  const [rows, setRows] = useState<LanguageRow[]>(() => initialRows(languages));
  const [localErrors, setLocalErrors] = useState<Record<string, string>>({});
  const serverErrors = isApiError(error) ? error.fieldErrors : {};
  const fieldError = (name: string) => localErrors[name] ?? serverErrors[name];

  const set = (name: keyof FormState) => (value: string) => setState((current) => ({ ...current, [name]: value }));

  const updateRow = (key: number, change: Partial<LanguageRow>) =>
    setRows((current) =>
      current.map((row) => {
        if (change.primary && row.key !== key) return { ...row, primary: false };
        return row.key === key ? { ...row, ...change } : row;
      }),
    );

  const addRow = () => {
    const used = new Set(rows.map((row) => row.code));
    const code = LANGUAGE_OPTIONS.find((option) => !used.has(option.value))?.value ?? 'en';
    setRows((current) => [...current, { key: nextRowKey++, code, level: 'B2', primary: current.length === 0 }]);
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const next: Record<string, string> = {};
    const years = state.years_of_experience.trim();
    if (years && (!Number.isFinite(Number(years)) || Number(years) < 0 || Number(years) > 60)) {
      next.years_of_experience = 'Enter a number of years between 0 and 60.';
    }
    const codes = rows.map((row) => row.code);
    if (new Set(codes).size !== codes.length) next.languages = 'Each language can only be listed once.';
    setLocalErrors(next);
    if (Object.keys(next).length > 0) return;
    void onSave(toProfileInput(state, rows));
  };

  return (
    <form className="profile-form" onSubmit={handleSubmit} noValidate aria-busy={pending}>
      <ErrorAlert error={error} />

      <fieldset>
        <legend>What you are looking for</legend>
        <div className="form-grid">
          <TextField label="Target role" name="target_role" value={state.target_role} onChange={set('target_role')} placeholder="e.g. Backend Developer" error={fieldError('target_role')} />
          <TextField label="Second target role" name="secondary_target_role" value={state.secondary_target_role} onChange={set('secondary_target_role')} error={fieldError('secondary_target_role')} />
          <TextField label="Preferred location" name="target_location" value={state.target_location} onChange={set('target_location')} placeholder="e.g. Berlin" error={fieldError('target_location')} />
          <SelectField label="Work type" name="preferred_work_type" value={state.preferred_work_type} onChange={set('preferred_work_type')} options={WORK_TYPE_OPTIONS} error={fieldError('preferred_work_type')} />
          <TextField label="Preferred technologies" name="preferred_technologies" value={state.preferred_technologies} onChange={set('preferred_technologies')} placeholder="e.g. Python, FastAPI, SQL" error={fieldError('preferred_technologies')} />
          <TextField label="Other preferences" name="extra_preferences" value={state.extra_preferences} onChange={set('extra_preferences')} error={fieldError('extra_preferences')} />
        </div>
      </fieldset>

      <fieldset>
        <legend>Experience</legend>
        <div className="form-grid">
          <TextField label="Years of professional experience" name="years_of_experience" type="number" min={0} step="any" value={state.years_of_experience} onChange={set('years_of_experience')} error={fieldError('years_of_experience')} />
          <SelectField label="Seniority you are aiming for" name="seniority_target" value={state.seniority_target} onChange={set('seniority_target')} options={SENIORITY_OPTIONS} error={fieldError('seniority_target')} />
        </div>
        <TextAreaField label="About you" name="self_description" rows={4} value={state.self_description} onChange={set('self_description')} hint="A few sentences about your experience and what you do well." error={fieldError('self_description')} />
      </fieldset>

      <fieldset>
        <legend>Work authorization and location</legend>
        <div className="form-grid">
          <SelectField label="Work authorization" name="work_authorization_status" value={state.work_authorization_status} onChange={set('work_authorization_status')} options={WORK_AUTHORIZATION_OPTIONS} error={fieldError('work_authorization_status')} />
          <SelectField label="Visa sponsorship" name="visa_sponsorship_needed" value={state.visa_sponsorship_needed} onChange={set('visa_sponsorship_needed')} options={VISA_OPTIONS} error={fieldError('visa_sponsorship_needed')} />
          <TextField label="Country you live in" name="current_residence_country" value={state.current_residence_country} onChange={set('current_residence_country')} placeholder="e.g. Germany" error={fieldError('current_residence_country')} />
          <TextField label="Relocation" name="relocation_preference" value={state.relocation_preference} onChange={set('relocation_preference')} placeholder="e.g. open to relocating within the EU" error={fieldError('relocation_preference')} />
          <SelectField label="Student status" name="student_status" value={state.student_status} onChange={set('student_status')} options={STUDENT_OPTIONS} hint="Working-student jobs require current enrollment." error={fieldError('student_status')} />
        </div>
      </fieldset>

      <fieldset>
        <legend>Languages</legend>
        {fieldError('languages') && <p className="field-error">{fieldError('languages')}</p>}
        {rows.length === 0 && <p className="muted">No languages yet. Jobs often require a language level, so add the ones you speak.</p>}
        <ul className="language-rows">
          {rows.map((row, index) => (
            <li className="language-row" key={row.key}>
              <SelectField label={`Language ${index + 1}`} name={`language-${row.key}`} value={row.code} onChange={(code) => updateRow(row.key, { code })} options={LANGUAGE_OPTIONS} />
              <SelectField label="Level" name={`level-${row.key}`} value={row.level} onChange={(level) => updateRow(row.key, { level })} options={LEVEL_OPTIONS} />
              <label className="checkbox">
                <input type="radio" name="primary-language" checked={row.primary} onChange={() => updateRow(row.key, { primary: true })} />
                Primary
              </label>
              <button type="button" className="button button-ghost button-small" onClick={() => setRows((current) => current.filter((item) => item.key !== row.key))}>
                Remove<span className="visually-hidden"> language {index + 1}</span>
              </button>
            </li>
          ))}
        </ul>
        <button type="button" className="button button-secondary button-small" onClick={addRow} disabled={rows.length >= LANGUAGE_OPTIONS.length}>
          Add a language
        </button>
      </fieldset>

      <div className="form-actions">
        <button type="submit" className="button button-primary" disabled={pending}>
          {pending ? 'Saving…' : profile ? 'Save changes' : 'Create profile'}
        </button>
      </div>
    </form>
  );
}
