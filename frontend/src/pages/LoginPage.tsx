import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { Alert } from '../components/Alert';
import { ErrorAlert } from '../components/ErrorAlert';
import { TextField } from '../components/fields';
import { PageHeader } from '../components/PageHeader';
import { useAuth } from '../auth/AuthContext';
import { useAction } from '../hooks/useAction';

export function LoginPage() {
  const { login, notice } = useAuth();
  const [mail, setMail] = useState('');
  const [password, setPassword] = useState('');
  const action = useAction((email: string, secret: string) => login(email, secret));

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(mail.trim(), password);
  };

  return (
    <div className="card auth-card">
      <PageHeader title="Log in" description="Use the email and password you registered with." />
      {notice && !action.error && <Alert tone="info">{notice}</Alert>}
      <ErrorAlert error={action.error} context="login" cooldownSeconds={action.cooldownSeconds} />
      <form onSubmit={handleSubmit} noValidate={false} aria-busy={action.pending}>
        <TextField label="Email" name="mail" type="email" autoComplete="email" required value={mail} onChange={setMail} />
        <TextField
          label="Password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={setPassword}
        />
        <button type="submit" className="button button-primary button-block" disabled={action.pending || action.cooldownSeconds > 0}>
          {action.pending ? 'Logging in…' : 'Log in'}
        </button>
      </form>
      <p className="auth-switch">
        No account yet? <Link to="/register">Create one</Link>
      </p>
    </div>
  );
}
