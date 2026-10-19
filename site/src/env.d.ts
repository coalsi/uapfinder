/// <reference types="astro/client" />

interface ImportMetaEnv {
  readonly PUBLIC_REPO_URL?: string;
  readonly PUBLIC_CONTACT_EMAIL?: string;
  readonly PUBLIC_API_BASE?: string;
  readonly PUBLIC_MAP_URL?: string;
  readonly PUBLIC_SITE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
