import { randomBytes } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from '@playwright/test';
import { sandboxDataEnv } from './e2e/sandbox';

const frontendDir = dirname(fileURLToPath(import.meta.url));

/**
 * End-to-end tests against real servers in an isolated demo setup:
 * - the FastAPI backend with a throwaway SQLite database and upload dir in a
 *   new temp directory, .env loading off, a random JWT secret, the fake
 *   analysis provider and no OpenAI key -- never the real apply101.db,
 *   .env or uploads/;
 * - the production build of the web client (vite preview).
 * Runs in the installed Microsoft Edge by default (no browser download).
 * APPLY101_E2E_BROWSER=chromium runs in Chromium instead, and
 * APPLY101_E2E_CHROMIUM_PATH points it at an installed Chromium binary.
 * Override the backend interpreter with APPLY101_PYTHON.
 */
const repoRoot = resolve(frontendDir, '..');
// Created once: worker processes load this config again but inherit the env.
// An empty value counts as unset, so it never resolves to the working directory.
const sandbox = process.env.APPLY101_E2E_SANDBOX || mkdtempSync(join(tmpdir(), 'apply101-e2e-'));
process.env.APPLY101_E2E_SANDBOX = sandbox;
const browser = process.env.APPLY101_E2E_BROWSER || 'msedge';
if (browser !== 'msedge' && browser !== 'chromium') {
  throw new Error(`APPLY101_E2E_BROWSER must be msedge or chromium, not ${browser}`);
}
const chromiumPath = process.env.APPLY101_E2E_CHROMIUM_PATH;
if (chromiumPath && browser !== 'chromium') {
  throw new Error('APPLY101_E2E_CHROMIUM_PATH needs APPLY101_E2E_BROWSER=chromium');
}
const backendPort = 8011;
const frontendPort = 4173;
const python =
  process.env.APPLY101_PYTHON ??
  join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');

const backendEnv: Record<string, string> = {
  ...(process.env as Record<string, string>),
  ...sandboxDataEnv(sandbox),
  APPLY101_CORS_ORIGINS: `http://localhost:${frontendPort}`,
  JWT_SECRET_KEY: randomBytes(48).toString('base64url'),
  // Short per-user cooldown so the 429 path is exercised without a long wait.
  JOB_ANALYSIS_USER_SUCCESS_COOLDOWN_SECONDS: '30',
};

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: `http://localhost:${frontendPort}`,
    browserName: 'chromium',
    channel: browser === 'msedge' ? 'msedge' : undefined,
    launchOptions: chromiumPath ? { executablePath: chromiumPath } : {},
    headless: true,
    viewport: { width: 1280, height: 900 },
    locale: 'en-GB',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: `"${python}" -m uvicorn backend.app.main:app --port ${backendPort}`,
      cwd: repoRoot,
      env: backendEnv,
      url: `http://localhost:${backendPort}/meta`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `npm run build && npm run preview -- --port ${frontendPort}`,
      cwd: frontendDir,
      env: { ...(process.env as Record<string, string>), VITE_API_BASE_URL: `http://localhost:${backendPort}` },
      url: `http://localhost:${frontendPort}`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
