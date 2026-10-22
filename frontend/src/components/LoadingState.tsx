export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

export function LoadingState({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="loading-state" role="status" aria-live="polite">
      <Spinner />
      <span>{label}…</span>
    </div>
  );
}
