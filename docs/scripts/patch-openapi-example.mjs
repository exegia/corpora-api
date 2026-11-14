import { readFile, writeFile } from "node:fs/promises";

// Fumadocs OpenAPI 12.6.2 leaves sampled schema examples as magic proxies.
// Its playground later calls structuredClone on the example and blanks the page.
const packageRoot = new URL("../node_modules/fumadocs-openapi/", import.meta.url);
const manifest = JSON.parse(await readFile(new URL("package.json", packageRoot), "utf8"));
if (manifest.version !== "12.6.2") {
  throw new Error(`Review the OpenAPI example patch for fumadocs-openapi ${manifest.version}`);
}

const sourceFile = new URL("dist/utils/get-example-requests.js", packageRoot);
const source = await readFile(sourceFile, "utf8");
const originalStart = "else result.body = sample(bodyOfType?.schema ?? {}, {";
const patchedStart = "else result.body = getRaw(sample(bodyOfType?.schema ?? {}, {";
const originalEnd = "skipNonRequired: true\n\t\t});";
const patchedEnd = "skipNonRequired: true\n\t\t}));";

if (source.includes(patchedStart) && source.includes(patchedEnd)) {
  process.exit(0);
}
if (
  source.split(originalStart).length !== 2 ||
  source.split(originalEnd).length !== 2
) {
  throw new Error("fumadocs-openapi example code changed; review the patch before building");
}
await writeFile(
  sourceFile,
  source.replace(originalStart, patchedStart).replace(originalEnd, patchedEnd),
);
