import { Alert } from './Alert';

export interface AnalysisIds {
  profile_analysis_id: number | null;
  job_analysis_id: number | null;
}

/** Which current analyses differ from the ones a saved result used. Unknown
 * current ids (not loaded, or no current analysis) are not compared. */
export function outdatedParts(
  saved: AnalysisIds,
  currentProfileAnalysisId: number | null | undefined,
  currentJobAnalysisId: number | null | undefined,
): ('profile' | 'job')[] {
  const parts: ('profile' | 'job')[] = [];
  if (currentProfileAnalysisId != null && saved.profile_analysis_id !== currentProfileAnalysisId) parts.push('profile');
  if (currentJobAnalysisId != null && saved.job_analysis_id !== currentJobAnalysisId) parts.push('job');
  return parts;
}

export function StaleMatchNotice({ parts }: { parts: ('profile' | 'job')[] }) {
  if (parts.length === 0) return null;
  const what = parts.length === 2 ? 'your profile and this job' : parts[0] === 'profile' ? 'your profile' : 'this job';
  return (
    <Alert tone="warning" title="Result may be outdated">
      This saved result was computed from an older analysis of {what}. Re-run the match to use the current analyses.
    </Alert>
  );
}
