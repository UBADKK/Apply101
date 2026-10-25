/** Shapes returned by the Apply101 backend (see backend/app/schemas.py and the routers). */

export interface CurrentUser {
  user_id: number;
  name: string;
  mail: string;
  is_admin: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface Meta {
  analysis_provider: string;
  demo: boolean;
}

export const WORK_AUTHORIZATION_VALUES = ['germany', 'eu_eea', 'other_country_only', 'none', 'unknown'] as const;
export type WorkAuthorization = (typeof WORK_AUTHORIZATION_VALUES)[number];

export const STUDENT_STATUS_VALUES = ['currently_enrolled', 'not_enrolled', 'unknown'] as const;
export type StudentStatus = (typeof STUDENT_STATUS_VALUES)[number];

export interface Language {
  language_code: string;
  language_name: string;
  proficiency_level: string | null;
  proficiency_scale: string | null;
  is_primary: boolean;
}

export interface Profile {
  profile_id: number;
  user_id: number;
  cv_filename: string | null;
  cv_file_path: string | null;
  cv_text: string | null;
  self_description: string | null;
  target_role: string | null;
  secondary_target_role: string | null;
  target_location: string | null;
  preferred_work_type: string | null;
  preferred_technologies: string | null;
  extra_preferences: string | null;
  visa_sponsorship_needed: boolean | null;
  work_authorization_status: string | null;
  relocation_preference: string | null;
  years_of_experience: number | null;
  seniority_target: string | null;
  current_residence_country: string | null;
  student_status: string | null;
  is_active: boolean;
}

export interface ProfileInput {
  self_description: string | null;
  target_role: string | null;
  secondary_target_role: string | null;
  target_location: string | null;
  preferred_work_type: string | null;
  preferred_technologies: string | null;
  extra_preferences: string | null;
  visa_sponsorship_needed: boolean | null;
  work_authorization_status: WorkAuthorization | null;
  relocation_preference: string | null;
  years_of_experience: number | null;
  seniority_target: string | null;
  current_residence_country: string | null;
  student_status: StudentStatus | null;
  languages: Language[];
}

export interface AnalysisLanguage {
  language: string;
  level: string;
  scale: string;
}

/** One entry of GET /users/{user_id}/profiles/analyzed: the profile's fields
 * plus its current analysis. */
export interface AnalyzedProfileEntry {
  profile_id: number;
  analysis: AnalyzedProfile;
}

/** The `analysis` object of an AnalyzedProfileEntry. */
export interface AnalyzedProfile {
  analysis_id: number;
  analysis_status: string;
  analysis_model: string;
  analysis_prompt_version: string;
  analyzed_at: string | null;
  candidate_summary: string | null;
  strong_skills: string[];
  moderate_skills: string[];
  weak_or_basic_skills: string[];
  tools: string[];
  languages: AnalysisLanguage[];
  target_roles: string[];
  seniority_level: string | null;
  education_level: string | null;
  work_authorization_status: string | null;
  visa_sponsorship_needed: boolean | null;
}

export interface AnalyzeResponse<T> {
  status: 'created' | 'cached' | string;
  analysis_id: number;
  analysis_model: string;
  analysis_prompt_version: string;
  analysis: T;
}

export interface ProfileAnalysisPayload {
  candidate_summary: string;
  strong_skills: string[];
  moderate_skills: string[];
  languages: AnalysisLanguage[];
  [key: string]: unknown;
}

export interface Job {
  job_id: number;
  title: string;
  company_name: string | null;
  location: string | null;
  url: string;
  description_text: string | null;
}

export interface JobInput {
  title: string;
  company_name: string | null;
  location: string | null;
  url: string;
  description_text: string;
}

export interface LanguageRequirement {
  language: string;
  minimum_level: string;
  required: boolean;
  evidence: string | null;
}

/** GET /jobs/analyzed/{job_id}. */
export interface AnalyzedJob {
  job_id: number;
  title: string;
  analysis_id: number;
  analysis_status: string;
  analysis_model: string;
  analysis_prompt_version: string;
  analyzed_at: string | null;
  normalized_role_title: string | null;
  role_family: string | null;
  seniority_level: string | null;
  required_skills: string[];
  preferred_skills: string[];
  language_requirements: LanguageRequirement[];
  visa_sponsorship: string | null;
  work_type: string | null;
  employment_type: string | null;
  dealbreakers: string[];
  analysis: { summary?: string; [key: string]: unknown };
}

export type EligibilityStatus = 'eligible' | 'ineligible' | 'review_required';

export interface MatchReason {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface MatchData {
  match_version: string;
  eligibility_status: EligibilityStatus;
  eligible: boolean;
  hard_fail_reasons: MatchReason[];
  review_reasons: MatchReason[];
  role_fit_score: number;
  skills_fit_score: number;
  experience_fit_score: number;
  seniority_fit_score: number;
  location_fit_score: number;
  work_type_fit_score: number;
  employment_type_fit_score: number;
  language_fit_score: number;
  visa_fit_score: number;
  overall_score: number;
  recommendation: string;
  summary: string;
  strengths: string[];
  weaknesses: string[];
  dealbreaker_warnings: string[];
  scoring_notes?: unknown[];
}

export interface MatchResponse {
  status: 'created' | 'cached' | string;
  match_id: number;
  profile_id: number;
  job_id: number;
  profile_analysis_id: number | null;
  job_analysis_id: number | null;
  match_version: string;
  eligibility_status: EligibilityStatus;
  overall_score: number;
  recommendation: string;
  match: MatchData;
}

export interface SavedMatch {
  match_id: number;
  profile_id: number;
  job_id: number;
  /** The analyses the result was computed from; compare with the current
   * ones to tell whether the saved result is outdated. */
  profile_analysis_id: number | null;
  job_analysis_id: number | null;
  title: string;
  company_name: string | null;
  location: string | null;
  url: string;
  match_version: string;
  eligibility_status: EligibilityStatus;
  overall_score: number;
  recommendation: string;
  summary: string;
  matched_at: string | null;
  match: MatchData;
}

export interface MatchList {
  total_count: number;
  returned_count: number;
  results: SavedMatch[];
}

export type WorkMode = 'remote' | 'hybrid' | 'onsite';

export interface CatalogJob {
  job_id: number;
  title: string;
  company_name: string | null;
  company_id: number | null;
  location: string | null;
  country_code: string | null;
  work_mode: WorkMode | null;
  source: string | null;
  source_label: string | null;
  attribution: string | null;
  url: string;
  first_seen_at: string | null;
  last_seen_at: string | null;
  source_created_at: number | null;
  remote_note: string | null;
  has_current_analysis?: boolean;
}

export interface CatalogPage {
  total: number;
  page: number;
  page_size: number;
  items: CatalogJob[];
}

export interface CatalogFilterOptions {
  sources: { source: string; label: string; count: number }[];
  countries: { country_code: string; count: number }[];
  work_modes: WorkMode[];
}

export interface CatalogQuery {
  q?: string;
  country?: string;
  work_mode?: string;
  source?: string;
  page?: number;
  page_size?: number;
}

export interface Recommendation extends CatalogJob {
  eligibility_status: EligibilityStatus;
  overall_score: number;
  recommendation: string;
  summary: string;
  strengths: string[];
  weaknesses: string[];
  hard_fail_reasons: MatchReason[];
  review_reasons: MatchReason[];
  dealbreaker_warnings: string[];
}

export interface RecommendationCoverage {
  active_jobs: number;
  analyzed_jobs: number;
  evaluated_jobs: number;
  not_analyzed_jobs: number;
  pool_limit: number;
  capped: boolean;
  status_counts: Record<EligibilityStatus, number>;
}

export interface RecommendationPage {
  match_version: string;
  coverage: RecommendationCoverage;
  include_ineligible: boolean;
  total: number;
  page: number;
  page_size: number;
  items: Recommendation[];
}

export interface ImportPreview {
  status: 'ok';
  source: 'greenhouse' | 'lever' | 'ashby' | 'json_ld';
  url: string;
  fields: { title: string | null; company_name: string | null; location: string | null; description_text: string | null };
  work_mode: WorkMode | null;
  missing: string[];
  notes: string[];
}

export type LetterLanguage = 'tr' | 'en' | 'de';

export interface LetterFact {
  id: string;
  source: 'profile' | 'cv' | 'job' | 'company';
  label: string;
  text: string;
  source_url?: string;
}

export interface LetterVersion {
  version_id: number;
  version_no: number;
  kind: 'generated' | 'edited';
  body_text: string;
  grounding: { facts: LetterFact[]; gaps: { code: string; message: string }[]; used_fact_ids: string[]; missing_information: string[] } | null;
  model: string | null;
  prompt_version: string | null;
  created_at: string | null;
}

export interface CoverLetter {
  letter_id: number;
  job_id: number | null;
  profile_id: number | null;
  language: LetterLanguage;
  job_title: string | null;
  company_name: string | null;
  created_at: string | null;
  updated_at: string | null;
  current_version: LetterVersion | null;
  version_count: number;
  versions?: LetterVersion[];
}
