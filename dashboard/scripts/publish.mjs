// Copy the static export into the Python package, where `tollgate-dashboard` serves it.
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const built = resolve(here, "..", "out");
const target = resolve(here, "..", "..", "src", "tollgate", "dashboard", "static");

if (!existsSync(built)) {
  console.error("No export found. Run `next build` first.");
  process.exit(1);
}
rmSync(target, { recursive: true, force: true });
cpSync(built, target, { recursive: true });
console.log(`Copied the dashboard to ${target}`);
