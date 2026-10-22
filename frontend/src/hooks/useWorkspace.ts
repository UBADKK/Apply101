import { useCallback } from 'react';
import { isApiError } from '../api/client';
import { api } from '../api/endpoints';
import type { AnalyzedJob, AnalyzedProfile, AnalyzedProfileEntry, Language, MatchList, Profile } from '../api/types';
import { useCurrentUser } from '../auth/AuthContext';
import { useResource, type Resource } from './useResource';

export interface Workspace {
  /** The user's profile: the first one, as the web client manages one profile. */
  profile: Profile | null;
  languages: Language[];
  /** Current analysis of that profile (current analysis version), if any. */
  analysis: AnalyzedProfile | null;
}

export function currentAnalysis(entries: AnalyzedProfileEntry[], profileId: number): AnalyzedProfile | null {
  return entries.find((entry) => entry.profile_id === profileId)?.analysis ?? null;
}

export function pickProfile(profiles: Profile[]): Profile | null {
  if (profiles.length === 0) return null;
  return [...profiles].sort((a, b) => a.profile_id - b.profile_id)[0];
}

/** Profile, languages and current profile analysis of the signed-in user. */
export function useWorkspace() {
  const user = useCurrentUser();
  const load = useCallback(async (): Promise<Workspace> => {
    const [profiles, languages, analyzed] = await Promise.all([
      api.listProfiles(user.user_id),
      api.listLanguages(user.user_id),
      api.analyzedProfiles(user.user_id),
    ]);
    const profile = pickProfile(profiles);
    return { profile, languages, analysis: profile ? currentAnalysis(analyzed, profile.profile_id) : null };
  }, [user.user_id]);
  return useResource(load, [load]);
}

/** The current profile analysis id: undefined while not known, null when
 * the profile has no current analysis. */
export function currentProfileAnalysisId(workspace: Resource<Workspace>): number | null | undefined {
  if (workspace.loading || !workspace.data) return undefined;
  return workspace.data.analysis?.analysis_id ?? null;
}

/** Same for a job analysis loaded with loadJobAnalysis. */
export function currentJobAnalysisId(jobAnalysis: Resource<AnalyzedJob | null>): number | null | undefined {
  if (jobAnalysis.loading || jobAnalysis.error || jobAnalysis.data === undefined) return undefined;
  return jobAnalysis.data?.analysis_id ?? null;
}

/** Saved match results of the user's profile (the backend's maximum page,
 * best score first); empty without a profile. */
export async function loadMatches(userId: number, profile: Profile | null): Promise<MatchList> {
  if (!profile) return { total_count: 0, returned_count: 0, results: [] };
  return api.listMatches(userId, profile.profile_id);
}

/** Detail of the 404 for a missing or invisible job (backend/app/job_visibility.py). */
const JOB_NOT_FOUND_DETAIL = 'Job not found.';

/** GET /jobs/analyzed/{id}: null when the job is visible but has no current
 * analysis (a 404 with another plain message); a missing or invisible job
 * stays an error. */
export async function loadJobAnalysis(jobId: number): Promise<AnalyzedJob | null> {
  try {
    return await api.analyzedJob(jobId);
  } catch (error) {
    if (isApiError(error) && error.status === 404 && typeof error.detail === 'string' && error.detail !== JOB_NOT_FOUND_DETAIL) {
      return null;
    }
    throw error;
  }
}
