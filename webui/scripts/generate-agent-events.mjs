import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import process from "node:process";

import { compileFromFile } from "json-schema-to-typescript";

const webuiRoot = fileURLToPath(new URL("../", import.meta.url));
const schemaPath = path.join(
  webuiRoot,
  "src",
  "generated",
  "agent-events.schema.json",
);
const outputPath = path.join(
  webuiRoot,
  "src",
  "generated",
  "agent-events.ts",
);
const generated = await compileFromFile(schemaPath, {
  bannerComment: "/* Generated from Python Pydantic models. Do not edit. */",
});
const normalized = `${generated.trimEnd()}\n`;
const checkOnly = process.argv.includes("--check");

if (checkOnly) {
  let current = "";
  try {
    current = await readFile(outputPath, "utf8");
  } catch {
    process.stderr.write("generated agent event types are missing\n");
    process.exitCode = 1;
  }
  if (current && current !== normalized) {
    process.stderr.write("generated agent event types are stale\n");
    process.exitCode = 1;
  }
} else {
  await mkdir(path.dirname(outputPath), { recursive: true });
  await writeFile(outputPath, normalized, "utf8");
}
