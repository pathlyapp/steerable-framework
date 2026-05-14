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

/**
 * Walk a schema and replace any `$ref` to a sibling top-level schema with a
 * `tsType: "<Title>"` hint. This stops json-schema-to-typescript from
 * inlining the referenced type — instead we emit a cross-file `import type`
 * so the package keeps stable per-schema modules with no duplicate exports.
 *
 * Returns the set of titles that need to be imported.
 */
function externalizeRefs(node, externals) {
  if (Array.isArray(node)) {
    for (const item of node) externalizeRefs(item, externals);
    return;
  }
  if (node && typeof node === "object") {
    if (typeof node.$ref === "string") {
      const ref = node.$ref;
      let title = null;
      // URL ref: https://steerable.dev/spec/<dir>/<Title>.schema.json
      const url = ref.match(/\/([A-Za-z][A-Za-z0-9]*)\.schema\.json$/);
      if (url) title = url[1];
      if (title) {
        delete node.$ref;
        node.tsType = title;
        externals.add(title);
        return;
      }
    }
    for (const v of Object.values(node)) externalizeRefs(v, externals);
  }
}

async function generateFor(pkg) {
  const schemaDir = path.join(root, "spec");
  const outDir = path.join(root, "packages", pkg, "ts", "src", "generated");
  await fs.mkdir(outDir, { recursive: true });
  const schemaFiles = await collectSchemas(schemaDir);
  for (const file of schemaFiles) {
    const schema = JSON.parse(await fs.readFile(file, "utf8"));
    if (!schema.title) continue;
    const externals = new Set();
    externalizeRefs(schema, externals);
    externals.delete(schema.title); // never self-import
    let ts = await compile(schema, schema.title, {
      bannerComment: "",
      unknownAny: false,
    });
    if (externals.size > 0) {
      const importLines = [...externals]
        .sort()
        .map((t) => `import type { ${t} } from "./${t}.js";`)
        .join("\n");
      ts = `${importLines}\n${ts}`;
    }
    const target = path.join(outDir, `${schema.title}.ts`);
    await fs.writeFile(target, ts, "utf8");
  }
}

await generateFor("agent-protocol");
await generateFor("agent-harness");
