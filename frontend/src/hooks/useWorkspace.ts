import { useCallback } from 'react';
import { isApiError } from '../api/client';
import { api } from '../api/endpoints';
import type { AnalyzedProfile, AnalyzedProfileEntry, Language, MatchList, Profile } from '../api/types';
import { useCurrentUser } from '../auth/AuthContext';
import { useResource } from './useResource';

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

/** Saved match results of the user's profile; empty without a profile. */
export async function loadMatches(userId: number, profile: Profile | null): Promise<MatchList> {
  if (!profile) return { total_count: 0, returned_count: 0, results: [] };
  return api.listMatches(userId, profile.profile_id);
}

/** GET /jobs/analyzed/{id} answers 404 with a plain message when the job
 * exists but has no current analysis; that is "not analyzed", not an error. */
export async function loadJobAnalysis(jobId: number) {
  try {
    return await api.analyzedJob(jobId);
  } catch (error) {
    if (isApiError(error) && error.status === 404 && typeof error.detail === 'string') return null;
    throw error;
  }
}
