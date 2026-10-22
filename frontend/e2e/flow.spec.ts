import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Browser, type Page } from '@playwright/test';

/**
 * The full demo flow in a real browser against the isolated servers started
 * by playwright.config.ts. Fake data only; screenshots go to e2e/screenshots/
 * (gitignored).
 */
const SCREENSHOTS = join(dirname(fileURLToPath(import.meta.url)), 'screenshots');
mkdirSync(SCREENSHOTS, { recursive: true });

const RUN = Date.now().toString(36);
const USER_A = { name: 'Ada Demo', mail: `ada.${RUN}@example.com`, password: `ada-demo-password-${RUN}` };
const USER_B = { name: 'Ben Demo', mail: `ben.${RUN}@example.com`, password: `ben-demo-password-${RUN}` };

const FIT_JOB = {
  title: 'Demo Backend Developer',
  url: `https://example.com/jobs/${RUN}-fit`,
  company: 'Example GmbH',
  location: 'Berlin',
  description:
    'Demo posting. You build APIs with Python, FastAPI and SQL. Fluent English (C1) is required. At least 2 years of professional experience.',
};
const GERMAN_JOB = {
  title: 'Demo Backend Developer (German team)',
  url: `https://example.com/jobs/${RUN}-german`,
  company: 'Example GmbH',
  location: 'Munich',
  description: 'Demo posting. You build APIs with Python and SQL. Fluent German (C1) is required for this role.',
};

const shared: { fitJobPath?: string; germanJobPath?: string; ineligibleMatchPath?: string } = {};

function fakePdf(text: string): Buffer {
  const content = `BT /F1 12 Tf 72 720 Td (${text}) Tj ET`;
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let out = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((body, index) => {
    offsets.push(out.length);
    out += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) out += `${String(offset).padStart(10, '0')} 00000 n \n`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}

async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(SCREENSHOTS, `${name}.png`), fullPage: true });
}

async function register(page: Page, user: typeof USER_A) {
  await page.goto('/register');
  await page.getByLabel('Name').fill(user.name);
  await page.getByLabel('Email').fill(user.mail);
  await page.getByLabel('Password', { exact: false }).first().fill(user.password);
  await page.getByLabel('Repeat password').fill(user.password);
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.getByRole('heading', { name: `Hello, ${user.name}` })).toBeVisible();
}

async function login(page: Page, user: typeof USER_A) {
  await page.goto('/login');
  await page.getByLabel('Email').fill(user.mail);
  await page.getByLabel('Password').fill(user.password);
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.getByRole('heading', { name: `Hello, ${user.name}` })).toBeVisible();
}

async function addJob(page: Page, job: typeof FIT_JOB) {
  await page.goto('/jobs/new');
  await page.getByLabel('Job title').fill(job.title);
  await page.getByLabel('Link to the posting').fill(job.url);
  await page.getByLabel('Company').fill(job.company);
  await page.getByLabel('Location').fill(job.location);
  await page.getByLabel('Job description').fill(job.description);
  await page.getByRole('button', { name: 'Save job' }).click();
  await expect(page.getByRole('heading', { name: job.title, level: 1 })).toBeVisible();
  return new URL(page.url()).pathname;
}

async function newPage(browser: Browser, options: Parameters<Browser['newContext']>[0] = {}) {
  const context = await browser.newContext(options);
  return context.newPage();
}

test.describe.serial('Apply101 web client', () => {
  test('user A goes from sign-up to an eligible and an ineligible reasoned match', async ({ page }) => {
    await register(page, USER_A);
    await expect(page.getByText('Demo mode.')).toBeVisible();
    await shot(page, '01-desktop-overview-new-user');

    // Profile with the fields matching uses.
    await page.goto('/profile');
    await page.getByLabel('Target role', { exact: true }).fill('Backend Developer');
    await page.getByLabel('Preferred location').fill('Berlin');
    await page.getByLabel('Work type').selectOption('hybrid');
    await page.getByLabel('Preferred technologies').fill('Python, FastAPI, SQL');
    await page.getByLabel('Years of professional experience').fill('3');
    await page.getByLabel('Seniority you are aiming for').selectOption('mid');
    await page.getByLabel('About you').fill('Demo backend developer who builds APIs with Python.');
    await page.getByLabel('Work authorization').selectOption('eu_eea');
    await page.getByLabel('Visa sponsorship').selectOption('no');
    await page.getByLabel('Country you live in').fill('Germany');
    await page.getByLabel('Student status').selectOption('not_enrolled');
    await page.getByRole('button', { name: 'Add a language' }).click();
    await page.getByLabel('Language 1').selectOption('en');
    await page.getByLabel('Level').selectOption('C1');
    await page.getByRole('button', { name: 'Create profile' }).click();
    await expect(page.getByText('Profile saved.')).toBeVisible();

    // PDF CV.
    await page.getByLabel('PDF file (up to 10 MB)').setInputFiles({
      name: 'demo_cv.pdf',
      mimeType: 'application/pdf',
      buffer: fakePdf('Demo CV Python FastAPI SQL Docker'),
    });
    await page.getByRole('button', { name: 'Upload CV' }).click();
    await expect(page.getByText(/demo_cv\.pdf: \d+ characters of text extracted\./)).toBeVisible();

    // Profile analysis (fake provider).
    await page.getByRole('button', { name: 'Analyze profile' }).click();
    await expect(page.getByText(/Fake demo analysis/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Re-run analysis' })).toBeVisible();
    await shot(page, '02-desktop-profile-analyzed');

    // A fitting job: analysis, then an eligible match.
    shared.fitJobPath = await addJob(page, FIT_JOB);
    await page.getByRole('button', { name: 'Analyze job' }).click();
    await expect(page.getByText('English C1 (required)')).toBeVisible();
    await page.getByRole('button', { name: 'Match with my profile' }).click();
    const fitResult = page.getByRole('article', { name: 'Match result' });
    await expect(fitResult.getByText('Eligible', { exact: true })).toBeVisible();
    await expect(fitResult.getByLabel(/Overall score \d+ out of 100/)).toBeVisible();
    await expect(fitResult.getByRole('heading', { name: 'Strengths' })).toBeVisible();
    await shot(page, '03-desktop-eligible-match');

    // A job requiring German: the per-user cooldown answers 429 first.
    shared.germanJobPath = await addJob(page, GERMAN_JOB);
    const analyzeRequests: string[] = [];
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/analyze')) analyzeRequests.push(request.url());
    });
    await page.getByRole('button', { name: 'Analyze job' }).click();
    await expect(page.getByText(/You can try again in \d+ seconds/)).toBeVisible();
    await expect(page.getByRole('button', { name: /Try again in \d+s/ })).toBeDisabled();
    await shot(page, '04-desktop-cooldown-429');
    // The countdown ends without any automatic retry; the user starts it again.
    await expect(page.getByRole('button', { name: 'Analyze job' })).toBeEnabled({ timeout: 40_000 });
    expect(analyzeRequests).toHaveLength(1);
    await page.getByRole('button', { name: 'Analyze job' }).click();
    await expect(page.getByText('German C1 (required)')).toBeVisible();
    expect(analyzeRequests).toHaveLength(2);
    await page.getByRole('button', { name: 'Match with my profile' }).click();
    const germanResult = page.getByRole('article', { name: 'Match result' });
    await expect(germanResult.getByText('Not eligible', { exact: true })).toBeVisible();
    await expect(germanResult.getByRole('heading', { name: 'Why you are not eligible' })).toBeVisible();
    await expect(germanResult.getByText(/Required language is missing: german\./).first()).toBeVisible();
    await shot(page, '05-desktop-ineligible-match');

    // Duplicate URL.
    await page.goto('/jobs/new');
    await page.getByLabel('Job title').fill('Duplicate');
    await page.getByLabel('Link to the posting').fill(FIT_JOB.url);
    await page.getByLabel('Job description').fill(FIT_JOB.description);
    await page.getByRole('button', { name: 'Save job' }).click();
    await expect(page.getByText('You already have a job with this URL.')).toBeVisible();

    // Saved results: list, reopen, rematch, and survive a reload.
    await page.goto('/matches');
    await expect(page.getByRole('link', { name: FIT_JOB.title, exact: true })).toBeVisible();
    await page.getByRole('link', { name: GERMAN_JOB.title }).click();
    await expect(page.getByText(/Saved result from/)).toBeVisible();
    await expect(page.getByText(/Required language is missing: german\./).first()).toBeVisible();
    shared.ineligibleMatchPath = new URL(page.url()).pathname;
    await shot(page, '06-desktop-saved-match');
    await page.reload();
    await expect(page.getByRole('heading', { name: GERMAN_JOB.title, level: 1 })).toBeVisible();
    await page.getByRole('button', { name: 'Re-run match' }).click();
    await expect(page.getByText('Match re-run with your current profile and job analyses.')).toBeVisible();
    await expect(page.getByText('Not eligible', { exact: true })).toBeVisible();
    // A rematch stores a new current result under a new id.
    shared.ineligibleMatchPath = new URL(page.url()).pathname;
  });

  test('user B sees none of user A’s data', async ({ browser }) => {
    const page = await newPage(browser);
    await register(page, USER_B);
    await page.goto('/jobs');
    await expect(page.getByText('No jobs yet')).toBeVisible();
    await page.goto(shared.fitJobPath!);
    await expect(page.getByRole('heading', { name: 'Job not found' })).toBeVisible();
    await page.goto(shared.ineligibleMatchPath!);
    await expect(page.getByRole('heading', { name: 'Match not found' })).toBeVisible();
    await page.goto('/matches');
    await expect(page.getByText('No profile yet')).toBeVisible();
    await shot(page, '07-desktop-user-b-isolated');
  });

  test('analysis outages (503) are explained and nothing is retried', async ({ browser }) => {
    const page = await newPage(browser);
    await login(page, USER_B);
    const path = await addJob(page, { ...FIT_JOB, url: `https://example.com/jobs/${RUN}-b-503`, title: 'Outage check' });
    let calls = 0;
    await page.route('**/jobs/*/analyze', async (route) => {
      calls += 1;
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Analysis service is not configured.' }) });
    });
    await page.goto(path);
    await page.getByRole('button', { name: 'Analyze job' }).click();
    await expect(page.getByText('Service unavailable')).toBeVisible();
    await expect(page.getByText(/Nothing was saved/)).toBeVisible();
    await page.waitForTimeout(1500);
    expect(calls).toBe(1);
  });

  test('keyboard login, logout and an expired session', async ({ browser }) => {
    const page = await newPage(browser);
    await page.goto('/login');
    // Each page moves focus to its heading; Tab continues into the form and
    // Shift+Tab reaches the skip link before it.
    await expect(page.getByRole('heading', { name: 'Log in' })).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.getByLabel('Email')).toBeFocused();
    await page.keyboard.type(USER_A.mail);
    await page.keyboard.press('Tab');
    await page.keyboard.type(USER_A.password);
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { name: `Hello, ${USER_A.name}` })).toBeVisible();

    await page.getByRole('button', { name: 'Log out' }).click();
    await expect(page.getByText('You have been logged out.')).toBeVisible();
    await page.goto('/jobs');
    await expect(page.getByRole('heading', { name: 'Log in' })).toBeVisible();
    // After logging in, the user returns to the page they asked for.
    await page.getByLabel('Email').fill(USER_A.mail);
    await page.getByLabel('Password').fill(USER_A.password);
    await page.getByRole('button', { name: 'Log in' }).click();
    await expect(page.getByRole('heading', { name: 'Your jobs' })).toBeVisible();

    await page.evaluate(() => window.localStorage.setItem('apply101.accessToken', 'expired-or-forged-token'));
    await page.goto('/profile');
    await expect(page.getByText('Your session has expired. Please log in again.')).toBeVisible();
    await shot(page, '08-desktop-session-expired');
  });

  test('mobile layout has no horizontal scrolling and keeps the flow usable', async ({ browser }) => {
    const page = await newPage(browser, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await login(page, USER_A);
    await expect(page.getByRole('heading', { name: 'Recent matches' })).toBeVisible();
    await shot(page, '09-mobile-overview');
    for (const path of ['/profile', shared.fitJobPath!, shared.ineligibleMatchPath!, '/matches', '/jobs/new']) {
      await page.goto(path);
      await expect(page.locator('main h1')).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `horizontal overflow on ${path}`).toBeLessThanOrEqual(1);
    }
    await page.goto(shared.fitJobPath!);
    await expect(page.getByRole('article', { name: 'Match result' }).getByText('Eligible', { exact: true })).toBeVisible();
    await shot(page, '10-mobile-eligible-match');
    await page.goto(shared.ineligibleMatchPath!);
    await expect(page.getByRole('article', { name: 'Match result' }).getByText('Not eligible', { exact: true })).toBeVisible();
    await shot(page, '11-mobile-ineligible-match');
    await page.goto('/profile');
    await expect(page.getByRole('heading', { name: 'Profile analysis' })).toBeVisible();
    await shot(page, '12-mobile-profile');
  });
});
