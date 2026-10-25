import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from '../App';
import { AuthProvider } from '../auth/AuthContext';

export function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </MemoryRouter>,
  );
}

export const PROFILE = {
  profile_id: 1,
  user_id: 1,
  cv_filename: null,
  cv_file_path: null,
  cv_text: null,
  self_description: null,
  target_role: 'Backend Developer',
  secondary_target_role: null,
  target_location: null,
  preferred_work_type: null,
  preferred_technologies: null,
  extra_preferences: null,
  visa_sponsorship_needed: null,
  work_authorization_status: null,
  relocation_preference: null,
  years_of_experience: null,
  seniority_target: null,
  current_residence_country: null,
  student_status: null,
  is_active: true,
};
