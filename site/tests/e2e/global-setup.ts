import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";

export default function globalSetup(): void {
  const root = join(import.meta.dirname, "..", "..");
  if (!existsSync(join(root, "dist", "index.html"))) {
    throw new Error("dist/ is missing: run `npm run build` before the end-to-end tests.");
  }
  mkdirSync(join(root, "screenshots"), { recursive: true });
}
