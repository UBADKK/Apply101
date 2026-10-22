import { useEffect, useRef, useState } from 'react';
import { api } from '../api/endpoints';
import type { ProfileInput } from '../api/types';
import { useCurrentUser } from '../auth/AuthContext';
import { Alert } from '../components/Alert';
import { ErrorAlert } from '../components/ErrorAlert';
import { LoadingState } from '../components/LoadingState';
import { PageHeader } from '../components/PageHeader';
import { useAction } from '../hooks/useAction';
import { currentAnalysis, useWorkspace } from '../hooks/useWorkspace';
import { CvUpload } from './profile/CvUpload';
import { ProfileAnalysisCard } from './profile/ProfileAnalysisCard';
import { ProfileForm } from './profile/ProfileForm';
import { clearProfileStale, isProfileMarkedStale, markProfileStale } from './profile/staleness';

export function ProfilePage() {
  const user = useCurrentUser();
  const workspace = useWorkspace();
  const { setData } = workspace;
  const [saved, setSaved] = useState(false);
  const [staleVersion, setStaleVersion] = useState(0);
  const [formKey, setFormKey] = useState(0);
  const savedRef = useRef<HTMLDivElement>(null);

  // The form remounts with the saved values; keep the keyboard position by
  // moving focus to the confirmation.
  useEffect(() => {
    if (saved) savedRef.current?.focus();
  }, [saved, formKey]);

  const markChanged = (profileId: number, hadAnalysis: boolean) => {
    if (!hadAnalysis) return;
    markProfileStale(profileId);
    setStaleVersion((value) => value + 1);
  };

  const save = useAction(async (input: ProfileInput) => {
    const current = workspace.data?.profile;
    const profile = current
      ? await api.updateProfile(user.user_id, current.profile_id, input)
      : await api.createProfile(user.user_id, input);
    const languages = await api.listLanguages(user.user_id);
    setData((latest) => ({ profile, languages, analysis: latest?.analysis ?? null }));
    if (current) markChanged(profile.profile_id, Boolean(workspace.data?.analysis));
    setSaved(true);
    setFormKey((key) => key + 1);
    return profile;
  });

  const upload = useAction(async (file: File) => {
    const current = workspace.data?.profile;
    if (!current) return undefined;
    const profile = await api.uploadCv(user.user_id, current.profile_id, file);
    setData((latest) => ({ languages: [], analysis: null, ...latest, profile }));
    markChanged(profile.profile_id, Boolean(workspace.data?.analysis));
    return profile;
  });

  const analyze = useAction(async () => {
    const current = workspace.data?.profile;
    if (!current) return undefined;
    await api.analyzeProfile(user.user_id, current.profile_id, Boolean(workspace.data?.analysis));
    const analysis = currentAnalysis(await api.analyzedProfiles(user.user_id), current.profile_id);
    setData((latest) => ({ profile: current, languages: [], ...latest, analysis }));
    clearProfileStale(current.profile_id);
    setStaleVersion((value) => value + 1);
    return analysis;
  });

  if (workspace.loading && !workspace.data) {
    return (
      <>
        <PageHeader title="Your profile" />
        <LoadingState label="Loading your profile" />
      </>
    );
  }
  if (workspace.error && !workspace.data) {
    return (
      <>
        <PageHeader title="Your profile" />
        <ErrorAlert error={workspace.error} />
        <button type="button" className="button" onClick={workspace.reload}>
          Try again
        </button>
      </>
    );
  }

  const { profile, languages, analysis } = workspace.data!;
  // staleVersion re-renders after the marker changes.
  const stale = staleVersion >= 0 && Boolean(profile && analysis && isProfileMarkedStale(profile.profile_id));

  return (
    <>
      <PageHeader
        title="Your profile"
        description="Matching compares these facts and your CV with each job's requirements. Fields you leave empty count as unknown, which can lead to a review instead of a clear result."
      />

      <section className="card" aria-labelledby="profile-details-title">
        <h2 id="profile-details-title">{profile ? 'Profile details' : 'Create your profile'}</h2>
        {saved && !save.error && (
          <div ref={savedRef} tabIndex={-1}>
            <Alert tone="success">Profile saved.</Alert>
          </div>
        )}
        <ProfileForm
          key={formKey}
          profile={profile}
          languages={languages}
          onSave={(input) => {
            setSaved(false);
            return save.run(input);
          }}
          pending={save.pending}
          error={save.error}
        />
      </section>

      {profile && (
        <section className="card" aria-labelledby="cv-title">
          <h2 id="cv-title">CV</h2>
          <CvUpload profile={profile} onUpload={(file) => upload.run(file)} pending={upload.pending} error={upload.error} />
        </section>
      )}

      {profile && (
        <section className="card" aria-labelledby="analysis-title">
          <h2 id="analysis-title">Profile analysis</h2>
          <ProfileAnalysisCard
            analysis={analysis}
            stale={stale}
            hasCv={Boolean(profile.cv_filename)}
            onAnalyze={() => void analyze.run()}
            pending={analyze.pending}
            error={analyze.error}
            cooldownSeconds={analyze.cooldownSeconds}
          />
        </section>
      )}
    </>
  );
}
