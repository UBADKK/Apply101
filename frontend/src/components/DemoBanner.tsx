import { useEffect, useState } from 'react';
import { api } from '../api/endpoints';
import type { Meta } from '../api/types';

/** Shown when the backend runs the deterministic fake analysis provider. */
export function DemoBanner() {
  const [meta, setMeta] = useState<Meta | null>(null);

  useEffect(() => {
    let active = true;
    api
      .meta()
      .then((value) => {
        if (active) setMeta(value);
      })
      .catch(() => {
        // The banner is informational; the pages report connection errors.
      });
    return () => {
      active = false;
    };
  }, []);

  if (!meta?.demo) return null;
  return (
    <div className="demo-banner" role="note">
      <strong>Demo mode.</strong> Analyses come from a deterministic fake provider, not an AI model. Use fake data only.
    </div>
  );
}
