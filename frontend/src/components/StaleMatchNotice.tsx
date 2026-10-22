import { Alert } from './Alert';

export interface AnalysisIds {
  profile_analysis_id: number | null;
  job_analysis_id: number | null;
}

/** A current analysis id: a number, null when loaded and there is no
 * current analysis, or undefined while it is not known yet. */
export type CurrentAnalysisId = number | null | undefined;

export interface OutdatedPart {
  part: 'profile' | 'job';
  /** True when there is no current analysis at all (e.g. after a new
   * analysis version), false when a newer one exists. */
  missing: boolean;
}

/** Why a saved result no longer reflects the current analyses. Ids that are
 * not known yet are not compared. */
export function outdatedParts(saved: AnalysisIds, currentProfile: CurrentAnalysisId, currentJob: CurrentAnalysisId): OutdatedPart[] {
  const parts: OutdatedPart[] = [];
  if (currentProfile !== undefined && saved.profile_analysis_id !== currentProfile) {
    parts.push({ part: 'profile', missing: currentProfile === null });
  }
  if (currentJob !== undefined && saved.job_analysis_id !== currentJob) {
    parts.push({ part: 'job', missing: currentJob === null });
  }
  return parts;
}

const SUBJECT = { profile: 'your profile', job: 'this job' };

interface StaleMatchNoticeProps {
  parts: OutdatedPart[];
  /** The profile was edited after its current analysis (this browser). */
  profileEdited?: boolean;
}

export function StaleMatchNotice({ parts, profileEdited = false }: StaleMatchNoticeProps) {
  if (parts.length === 0 && !profileEdited) return null;
  const missing = parts.filter((item) => item.missing).map((item) => SUBJECT[item.part]);
  const older = parts.filter((item) => !item.missing).map((item) => SUBJECT[item.part]);
  return (
    <Alert tone="warning" title="Result may be outdated">
      {older.length > 0 && <p>This saved result was computed from an older analysis of {older.join(' and ')}.</p>}
      {missing.length > 0 && (
        <p>
          There is no current analysis of {missing.join(' or ')} any more. Analyze {missing.length > 1 ? 'them' : 'it'} again,
          then re-run the match.
        </p>
      )}
      {profileEdited && parts.length === 0 && (
        <p>You changed your profile after it was analyzed. Re-run the profile analysis, then re-run the match.</p>
      )}
      {older.length > 0 && missing.length === 0 && <p>Re-run the match to use the current analyses.</p>}
    </Alert>
  );
}
