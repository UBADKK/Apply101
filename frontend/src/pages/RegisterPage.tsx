import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { isApiError } from '../api/client';
import { ErrorAlert } from '../components/ErrorAlert';
import { TextField } from '../components/fields';
import { PageHeader } from '../components/PageHeader';
import { useAuth } from '../auth/AuthContext';
import { useAction } from '../hooks/useAction';

/** Same minimum as backend/app/security.py MIN_PASSWORD_LENGTH. */
export const MIN_PASSWORD_LENGTH = 15;

interface FormErrors {
  name?: string;
  mail?: string;
  password?: string;
  confirm?: string;
}

export function RegisterPage() {
  const { register } = useAuth();
  const [name, setName] = useState('');
  const [mail, setMail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [errors, setErrors] = useState<FormErrors>({});
  const action = useAction((n: string, m: string, p: string) => register(n, m, p));

  const serverErrors = isApiError(action.error) ? action.error.fieldErrors : {};

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const next: FormErrors = {};
    if (!name.trim()) next.name = 'Enter your name.';
    if (!/^\S+@\S+\.\S+$/.test(mail.trim())) next.mail = 'Enter a valid email address.';
    if (password.length < MIN_PASSWORD_LENGTH) next.password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
    if (confirm !== password) next.confirm = 'The passwords do not match.';
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    void action.run(name.trim(), mail.trim(), password);
  };

  return (
    <div className="card auth-card">
      <PageHeader title="Create an account" description="Your profile, jobs and matches stay private to your account." />
      <ErrorAlert error={action.error} cooldownSeconds={action.cooldownSeconds} />
      <form onSubmit={handleSubmit} noValidate aria-busy={action.pending}>
        <TextField label="Name" name="name" autoComplete="name" required value={name} onChange={setName} error={errors.name ?? serverErrors.name} />
        <TextField
          label="Email"
          name="mail"
          type="email"
          autoComplete="email"
          required
          value={mail}
          onChange={setMail}
          error={errors.mail ?? serverErrors.mail}
        />
        <TextField
          label="Password"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          hint={`At least ${MIN_PASSWORD_LENGTH} characters.`}
          value={password}
          onChange={setPassword}
          error={errors.password ?? serverErrors.password}
        />
        <TextField
          label="Repeat password"
          name="confirm"
          type="password"
          autoComplete="new-password"
          required
          value={confirm}
          onChange={setConfirm}
          error={errors.confirm}
        />
        <button type="submit" className="button button-primary button-block" disabled={action.pending || action.cooldownSeconds > 0}>
          {action.pending ? 'Creating account…' : 'Create account'}
        </button>
      </form>
      <p className="auth-switch">
        Already registered? <Link to="/login">Log in</Link>
      </p>
    </div>
  );
}
