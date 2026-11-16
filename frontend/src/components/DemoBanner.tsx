import { useProviderMode } from '../hooks/useProviderMode';

/** Shown when the backend runs the deterministic fake analysis provider. */
export function DemoBanner() {
  const mode = useProviderMode();
  if (mode !== 'fake') return null;
  return (
    <div className="demo-banner" role="note">
      <strong>Demo mode.</strong> Analyses come from a deterministic fake provider, not an AI model. Use fake data only.
    </div>
  );
}
