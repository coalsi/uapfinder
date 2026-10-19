/** Indexable pages, used by the sitemap and the end-to-end tests. */
export const PAGES = [
  { path: "/", name: "home", changefreq: "weekly", priority: 1.0 },
  { path: "/how-it-works/", name: "how-it-works", changefreq: "monthly", priority: 0.9 },
  { path: "/host/", name: "host", changefreq: "monthly", priority: 0.9 },
  { path: "/get-involved/", name: "get-involved", changefreq: "monthly", priority: 0.8 },
  { path: "/download/", name: "download", changefreq: "monthly", priority: 0.8 },
  { path: "/faq/", name: "faq", changefreq: "monthly", priority: 0.7 },
  { path: "/privacy/", name: "privacy", changefreq: "yearly", priority: 0.4 },
] as const;
