import { useState } from 'react';
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

export function ProfilePage() {
  const user = useCurrentUser();
  const workspace = useWorkspace();
  const [saved, setSaved] = useState(false);
  const [stale, setStale] = useState(false);
  const [formKey, setFormKey] = useState(0);

  const save = useAction(async (input: ProfileInput) => {
    const current = workspace.data?.profile;
    const profile = current
      ? await api.updateProfile(user.user_id, current.profile_id, input)
      : await api.createProfile(user.user_id, input);
    const languages = await api.listLanguages(user.user_id);
    workspace.setData({ profile, languages, analysis: workspace.data?.analysis ?? null });
    setSaved(true);
    if (current && workspace.data?.analysis) setStale(true);
    setFormKey((key) => key + 1);
    return profile;
  });

  const upload = useAction(async (file: File) => {
    const current = workspace.data?.profile;
    if (!current) return undefined;
    const profile = await api.uploadCv(user.user_id, current.profile_id, file);
    workspace.setData({ ...workspace.data!, profile });
    if (workspace.data?.analysis) setStale(true);
    return profile;
  });

  const analyze = useAction(async () => {
    const current = workspace.data?.profile;
    if (!current) return undefined;
    await api.analyzeProfile(user.user_id, current.profile_id, Boolean(workspace.data?.analysis));
    const analysis = currentAnalysis(await api.analyzedProfiles(user.user_id), current.profile_id);
    workspace.setData({ ...workspace.data!, analysis });
    setStale(false);
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

  return (
    <>
      <PageHeader
        title="Your profile"
        description="Matching compares these facts and your CV with each job's requirements. Fields you leave empty count as unknown, which can lead to a review instead of a clear result."
      />

      <section className="card" aria-labelledby="profile-details-title">
        <h2 id="profile-details-title">{profile ? 'Profile details' : 'Create your profile'}</h2>
        {saved && !save.error && <Alert tone="success">Profile saved.</Alert>}
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
