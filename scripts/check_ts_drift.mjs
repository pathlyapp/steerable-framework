import { execSync } from "node:child_process";

const out = execSync("git status --porcelain", { encoding: "utf8" });
const dirty = out
  .split("\n")
  .filter(Boolean)
  .filter((line) => line.includes("/generated/"));
if (dirty.length > 0) {
  console.error("Generated TS files are out of date:");
  for (const line of dirty) console.error(line);
  process.exit(1);
}
