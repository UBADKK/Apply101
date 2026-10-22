import { useEffect, useRef, type ReactNode } from 'react';

/** Page title that receives focus on navigation, so screen-reader and
 * keyboard users start at the new page's heading. */
export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    document.title = `${title} · Apply101`;
    headingRef.current?.focus();
  }, [title]);

  return (
    <div className="page-header">
      <div>
        <h1 ref={headingRef} tabIndex={-1}>
          {title}
        </h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}
