import { join } from 'node:path';

/** The isolated backend's data settings for a sandbox directory: shared by
 * playwright.config.ts (the server) and the specs that seed it, so both
 * always point at the same throwaway database and upload directory. */
export function sandboxDataEnv(sandbox: string): Record<string, string> {
  return {
    APPLY101_LOAD_DOTENV: '0',
    APPLY101_DATABASE_URL: `sqlite:///${join(sandbox, 'e2e.db').replace(/\\/g, '/')}`,
    APPLY101_UPLOAD_DIR: join(sandbox, 'uploads'),
    APPLY101_ANALYSIS_PROVIDER: 'fake',
    OPENAI_API_KEY: '',
  };
}
