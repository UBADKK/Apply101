/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Apply101 FastAPI backend, e.g. http://localhost:8001. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
