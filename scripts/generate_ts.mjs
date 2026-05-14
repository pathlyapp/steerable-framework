import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");

async function collectSchemas(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await collectSchemas(full)));
    } else if (entry.isFile() && entry.name.endsWith(".schema.json")) {
      files.push(full);
    }
  }
  return files;
}

async function generateFor(pkg) {
  const schemaDir = path.join(root, "spec");
  const outDir = path.join(root, "packages", pkg, "ts", "src", "generated");
  await fs.mkdir(outDir, { recursive: true });
  const schemaFiles = await collectSchemas(schemaDir);
  for (const file of schemaFiles) {
    const schema = JSON.parse(await fs.readFile(file, "utf8"));
    if (!schema.title) continue;
    const ts = await compile(schema, schema.title, {
      bannerComment: "",
      unknownAny: false,
    });
    const target = path.join(outDir, `${schema.title}.ts`);
    await fs.writeFile(target, ts, "utf8");
  }
}

await generateFor("agent-protocol");
await generateFor("agent-harness");
