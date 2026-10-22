import { apiRequest } from './client';
import type {
  AnalyzedJob,
  AnalyzedProfileEntry,
  AnalyzeResponse,
  CurrentUser,
  Job,
  JobInput,
  Language,
  MatchList,
  MatchResponse,
  Meta,
  Profile,
  ProfileAnalysisPayload,
  ProfileInput,
  TokenResponse,
} from './types';

export const api = {
  meta: () => apiRequest<Meta>('/meta', { auth: false }),

  register: (name: string, mail: string, password: string) =>
    apiRequest<CurrentUser>('/auth/register', { method: 'POST', json: { name, mail, password }, auth: false }),
  login: (mail: string, password: string) =>
    apiRequest<TokenResponse>('/auth/login', { method: 'POST', form: { username: mail, password }, auth: false }),
  me: () => apiRequest<CurrentUser>('/auth/me'),

  listProfiles: (userId: number) => apiRequest<Profile[]>(`/users/${userId}/profiles`),
  createProfile: (userId: number, input: ProfileInput) =>
    apiRequest<Profile>(`/users/${userId}/profiles`, { method: 'POST', json: input }),
  updateProfile: (userId: number, profileId: number, input: ProfileInput) =>
    apiRequest<Profile>(`/users/${userId}/profiles/${profileId}`, { method: 'PATCH', json: input }),
  listLanguages: (userId: number) => apiRequest<Language[]>(`/users/${userId}/languages`),
  uploadCv: (userId: number, profileId: number, file: File) => {
    const body = new FormData();
    body.append('file', file);
    return apiRequest<Profile>(`/users/${userId}/profiles/${profileId}/upload-cv`, { method: 'POST', body });
  },
  analyzedProfiles: (userId: number) => apiRequest<AnalyzedProfileEntry[]>(`/users/${userId}/profiles/analyzed`),
  analyzeProfile: (userId: number, profileId: number, forceReanalyze: boolean) =>
    apiRequest<AnalyzeResponse<ProfileAnalysisPayload>>(`/users/${userId}/profiles/${profileId}/analyze`, {
      method: 'POST',
      query: forceReanalyze ? { force_reanalyze: true } : undefined,
    }),

  listOwnJobs: () => apiRequest<Job[]>('/jobs/', { query: { owned: true, limit: 200 } }),
  getJob: (jobId: number) => apiRequest<Job>(`/jobs/${jobId}`),
  createJob: (input: JobInput) => apiRequest<Job>('/jobs/manual', { method: 'POST', json: input }),
  analyzedJob: (jobId: number) => apiRequest<AnalyzedJob>(`/jobs/analyzed/${jobId}`),
  analyzeJob: (jobId: number) => apiRequest<AnalyzeResponse<Record<string, unknown>>>(`/jobs/${jobId}/analyze`, { method: 'POST' }),

  match: (userId: number, profileId: number, jobId: number, forceRematch: boolean) =>
    apiRequest<MatchResponse>(`/users/${userId}/profiles/${profileId}/jobs/${jobId}/match`, {
      method: 'POST',
      query: forceRematch ? { force_rematch: true } : undefined,
    }),
  listMatches: (userId: number, profileId: number) =>
    apiRequest<MatchList>(`/users/${userId}/profiles/${profileId}/matches`, { query: { limit: 500 } }),
};
