import { execFileSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';
import { sandboxDataEnv } from './sandbox';

/**
 * Shared pool, "Jobs for me" and URL import against the isolated servers of
 * playwright.config.ts. The pool is seeded with synthetic postings through
 * catalog_sync (seed_catalog.py refuses anything but the sandbox), analyzed
 * by an admin with the bounded batch route and the fake provider.
 */
const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '..', '..');
const API = 'http://localhost:8011';
const RUN = Date.now().toString(36);
const ADMIN = { mail: `admin.${RUN}@example.com`, password: `admin-demo-password-${RUN}` };
const USER = { name: 'Cem Demo', mail: `cem.${RUN}@example.com`, password: `cem-demo-password-${RUN}` };

function python(): string {
  return process.env.APPLY101_PYTHON ?? join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
}

async function token(mail: string, password: string): Promise<string> {
  const response = await fetch(`${API}/auth/login`, { method: 'POST', body: new URLSearchParams({ username: mail, password }) });
  expect(response.status).toBe(200);
  return (await response.json()).access_token;
}

async function api(path: string, bearer: string, init: RequestInit = {}) {
  const response = await fetch(`${API}${path}`, { ...init, headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json', ...(init.headers ?? {}) } });
  return { status: response.status, body: await response.json().catch(() => null) };
}

async function login(page: Page) {
  await page.goto('/login');
  await page.getByLabel('Email').fill(USER.mail);
  await page.getByLabel('Password').fill(USER.password);
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.getByRole('heading', { name: `Hello, ${USER.name}` })).toBeVisible();
}

test.describe.serial('Shared pool and jobs for me', () => {
  test.beforeAll(async () => {
    execFileSync(python(), [join(here, 'seed_catalog.py'), ADMIN.mail, ADMIN.password], { cwd: repoRoot, env: { ...process.env, ...sandboxDataEnv(process.env.APPLY101_E2E_SANDBOX as string) }, stdio: 'pipe' });
    const admin = await token(ADMIN.mail, ADMIN.password);
    const batch = await api('/jobs/analyze-missing?limit=10', admin, { method: 'POST' });
    expect(batch.status, JSON.stringify(batch.body)).toBe(200);

    const registered = await fetch(`${API}/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: USER.name, mail: USER.mail, password: USER.password }) });
    expect(registered.status).toBe(201);
    const bearer = await token(USER.mail, USER.password);
    const me = await api('/auth/me', bearer);
    const profile = await api(`/users/${me.body.user_id}/profiles`, bearer, {
      method: 'POST',
      body: JSON.stringify({
        target_role: 'Backend Developer', years_of_experience: 3, visa_sponsorship_needed: false, work_authorization_status: 'eu_eea',
        student_status: 'not_enrolled', current_residence_country: 'Germany',
        languages: [{ language_code: 'en', language_name: 'English', proficiency_level: 'C1', proficiency_scale: 'CEFR', is_primary: true }],
      }),
    });
    expect(profile.status).toBe(200);
    const analyzed = await api(`/users/${me.body.user_id}/profiles/${profile.body.profile_id}/analyze`, bearer, { method: 'POST' });
    expect(analyzed.status).toBe(200);
  });

  test('the pool lists open jobs with filters and the source link', async ({ page }) => {
    await login(page);
    await page.getByRole('link', { name: 'Job pool' }).click();
    await expect(page.getByRole('heading', { name: 'Job pool', level: 1 })).toBeVisible();
    await expect(page.getByText('3 open jobs')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Original posting' }).first()).toHaveAttribute('href', /job-boards\.greenhouse\.io\/demorobotics/);
    await page.getByLabel('Work mode').selectOption('remote');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(page.getByText('1 open job')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Pool Backend Developer' })).toBeVisible();
    await expect(page.getByText(/does not say from which countries/)).toBeVisible();
  });

  test('jobs for me ranks analyzed jobs and states the coverage', async ({ page }) => {
    await login(page);
    await page.getByRole('link', { name: 'For me' }).click();
    await expect(page.getByText(/Ranked 3 of 3 open jobs/)).toBeVisible();
    const first = page.getByRole('listitem').filter({ has: page.getByRole('heading') }).first();
    await expect(first.getByRole('link', { name: 'Pool Backend Developer' })).toBeVisible();
    await expect(first.getByText('Eligible', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Pool Platform Engineer (German)' })).toHaveCount(0);
    await page.getByLabel('Also show jobs I am not eligible for').check();
    await expect(page.getByRole('link', { name: 'Pool Platform Engineer (German)' })).toBeVisible();
    await expect(page.getByText(/required_language_missing/)).toBeVisible();
  });

  test('importing an unsupported link falls back to pasting the text', async ({ page }) => {
    await login(page);
    await page.goto('/jobs/new');
    await page.getByLabel('Job link').fill('https://www.linkedin.com/jobs/view/123');
    await page.getByRole('button', { name: 'Read posting' }).click();
    await expect(page.getByText('Paste the job title, link and text in the form below instead.')).toBeVisible();
    await expect(page.getByLabel('Job title')).toHaveValue('');
  });

  test('pool, jobs for me and import fit a phone screen', async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    await login(page);
    for (const path of ['/catalog', '/for-you', '/jobs/new']) {
      await page.goto(path);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      await page.waitForLoadState('networkidle');
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, path).toBeLessThanOrEqual(0);
    }
    await context.close();
  });
});
