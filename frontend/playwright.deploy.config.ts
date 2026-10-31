import { randomBytes } from 'node:crypto';
import { mkdirSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from '@playwright/test';

/**
 * End-to-end check of the single-service test deployment
 * (backend/app/deploy_app.py, as in the Dockerfile): the web client built
 * with VITE_API_BASE_URL=/api and served by the backend, the access-key
 * gate, and the data on a "volume" -- a new temp directory, never the real
 * apply101.db, .env or uploads/. Fake analysis provider, no OpenAI key.
 * Same browser settings as playwright.config.ts.
 */
const frontendDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(frontendDir, '..');
// Created once: worker processes load this config again but inherit the env.
const sandbox = process.env.APPLY101_DEPLOY_E2E_SANDBOX || mkdtempSync(join(tmpdir(), 'apply101-deploy-e2e-'));
process.env.APPLY101_DEPLOY_E2E_SANDBOX = sandbox;
const dataDir = join(sandbox, 'data');
mkdirSync(dataDir, { recursive: true });
const webDir = join(sandbox, 'web');
process.env.APPLY101_DEPLOY_E2E_ACCESS_KEY ||= randomBytes(24).toString('base64url');
const browser = process.env.APPLY101_E2E_BROWSER || 'msedge';
if (browser !== 'msedge' && browser !== 'chromium') {
  throw new Error(`APPLY101_E2E_BROWSER must be msedge or chromium, not ${browser}`);
}
const chromiumPath = process.env.APPLY101_E2E_CHROMIUM_PATH;
const port = 8021;
const python =
  process.env.APPLY101_PYTHON ??
  join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');

const serverEnv: Record<string, string> = {
  ...(process.env as Record<string, string>),
  APPLY101_LOAD_DOTENV: '0',
  APPLY101_DATA_DIR: dataDir,
  APPLY101_DATABASE_URL: `sqlite:///${join(dataDir, 'apply101-test.db').replace(/\\/g, '/')}`,
  APPLY101_UPLOAD_DIR: join(dataDir, 'uploads'),
  APPLY101_ANALYSIS_PROVIDER: 'fake',
  OPENAI_API_KEY: '',
  APPLY101_CORS_ORIGINS: '',
  APPLY101_CATALOG_SYNC_ENABLED: '0',
  APPLY101_WEB_DIR: webDir,
  APPLY101_ACCESS_KEY: process.env.APPLY101_DEPLOY_E2E_ACCESS_KEY,
  JWT_SECRET_KEY: randomBytes(48).toString('base64url'),
};

export default defineConfig({
  testDir: './e2e-deploy',
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: `http://localhost:${port}`,
    browserName: 'chromium',
    channel: browser === 'msedge' ? 'msedge' : undefined,
    launchOptions: chromiumPath ? { executablePath: chromiumPath } : {},
    headless: true,
    viewport: { width: 1280, height: 900 },
    locale: 'en-GB',
    trace: 'retain-on-failure',
  },
  webServer: {
    command:
      `npx vite build --outDir "${webDir}" --emptyOutDir && cd "${repoRoot}" && ` +
      `"${python}" -m uvicorn --factory backend.app.deploy_app:create_app --port ${port} --workers 1`,
    cwd: frontendDir,
    env: { ...serverEnv, VITE_API_BASE_URL: '/api' },
    url: `http://localhost:${port}/healthz`,
    reuseExistingServer: false,
    timeout: 180_000,
  },
});
