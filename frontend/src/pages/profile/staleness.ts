/**
 * "The profile or CV changed after its analysis" marker, per user, profile
 * and browser. The backend does not invalidate an analysis on PATCH or CV
 * upload, so the web client remembers the change across reloads until the
 * profile is analyzed again.
 */
const key = (userId: number, profileId: number) => `apply101.profileChangedAfterAnalysis.${userId}.${profileId}`;

export function isProfileMarkedStale(userId: number, profileId: number): boolean {
  try {
    return window.localStorage.getItem(key(userId, profileId)) === '1';
  } catch {
    return false;
  }
}

export function markProfileStale(userId: number, profileId: number): void {
  try {
    window.localStorage.setItem(key(userId, profileId), '1');
  } catch {
    // Storage unavailable: the marker lasts for this page only.
  }
}

export function clearProfileStale(userId: number, profileId: number): void {
  try {
    window.localStorage.removeItem(key(userId, profileId));
  } catch {
    // Nothing to clear.
  }
}
