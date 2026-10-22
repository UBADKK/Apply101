/**
 * "The profile or CV changed after its analysis" marker, per profile and per
 * browser. The backend does not invalidate an analysis on PATCH or CV
 * upload, so the web client remembers the change across reloads until the
 * profile is analyzed again.
 */
const key = (profileId: number) => `apply101.profileChangedAfterAnalysis.${profileId}`;

export function isProfileMarkedStale(profileId: number): boolean {
  try {
    return window.localStorage.getItem(key(profileId)) === '1';
  } catch {
    return false;
  }
}

export function markProfileStale(profileId: number): void {
  try {
    window.localStorage.setItem(key(profileId), '1');
  } catch {
    // Storage unavailable: the marker lasts for this page only.
  }
}

export function clearProfileStale(profileId: number): void {
  try {
    window.localStorage.removeItem(key(profileId));
  } catch {
    // Nothing to clear.
  }
}
