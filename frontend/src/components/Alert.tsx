import type { ReactNode } from 'react';

export type AlertTone = 'info' | 'success' | 'warning' | 'error';

interface AlertProps {
  tone?: AlertTone;
  title?: string;
  children?: ReactNode;
  className?: string;
}

/** Errors are announced immediately (role=alert); everything else politely. */
export function Alert({ tone = 'info', title, children, className = '' }: AlertProps) {
  return (
    <div className={`alert alert-${tone} ${className}`.trim()} role={tone === 'error' ? 'alert' : 'status'}>
      {title && <p className="alert-title">{title}</p>}
      {children && <div className="alert-body">{children}</div>}
    </div>
  );
}
