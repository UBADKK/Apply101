import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';
import { fakePdf } from '../e2e/fakePdf';

/**
 * The single-service test deployment started by playwright.deploy.config.ts:
 * nothing is reachable before the access key, then the whole app works on
 * one origin with its data in the "volume" directory.
 */
const RUN = Date.now().toString(36);
const USER = { name: 'Dee Deploy', mail: `dee.${RUN}@example.com`, password: `dee-deploy-password-${RUN}` };
const ACCESS_KEY = process.env.APPLY101_DEPLOY_E2E_ACCESS_KEY!;
const DATA_DIR = join(process.env.APPLY101_DEPLOY_E2E_SANDBOX!, 'data');

test.describe.serial('test deployment', () => {
  test('nothing but the health check answers without the access key', async ({ page, request }) => {
    expect((await request.get('/healthz')).status()).toBe(200);
    const api = await request.get('/api/meta');
    expect(api.status()).toBe(401);
    await page.goto('/catalog');
    await expect(page.getByRole('heading', { name: 'Apply101 test environment' })).toBeVisible();
    await page.getByLabel('Access key').fill('wrong-key');
    await page.getByRole('button', { name: 'Continue' }).click();
    await expect(page.getByRole('alert')).toHaveText('Wrong access key.');
  });

  test('with the key the app works on one origin and stores data on the volume', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('Access key').fill(ACCESS_KEY);
    await page.getByRole('button', { name: 'Continue' }).click();

    await page.goto('/register');
    await page.getByLabel('Name').fill(USER.name);
    await page.getByLabel('Email').fill(USER.mail);
    await page.getByLabel('Password', { exact: false }).first().fill(USER.password);
    await page.getByLabel('Repeat password').fill(USER.password);
    await page.getByRole('button', { name: 'Create account' }).click();
    await expect(page.getByRole('heading', { name: `Hello, ${USER.name}` })).toBeVisible();
    await expect(page.getByText('Demo mode.')).toBeVisible();

    await page.goto('/profile');
    await page.getByLabel('Target role', { exact: true }).fill('Backend Developer');
    await page.getByRole('button', { name: 'Create profile' }).click();
    await expect(page.getByText('Profile saved.')).toBeVisible();
    await page.getByLabel('PDF file (up to 10 MB)').setInputFiles({
      name: 'deploy_cv.pdf',
      mimeType: 'application/pdf',
      buffer: fakePdf('Deploy CV Python FastAPI'),
    });
    await page.getByRole('button', { name: 'Upload CV' }).click();
    await expect(page.getByText(/deploy_cv\.pdf: \d+ characters of text extracted\./)).toBeVisible();
    await page.getByRole('button', { name: 'Analyze profile' }).click();
    await expect(page.getByText(/Fake demo analysis/)).toBeVisible();

    // A deep link reloads into the app, not the API.
    await page.goto('/catalog');
    await expect(page.getByRole('heading', { name: 'Job pool', level: 1 })).toBeVisible();

    expect(existsSync(join(DATA_DIR, 'apply101-test.db'))).toBe(true);
    expect(readdirSync(join(DATA_DIR, 'uploads')).some((name) => name.endsWith('_cv.pdf'))).toBe(true);
  });
});
