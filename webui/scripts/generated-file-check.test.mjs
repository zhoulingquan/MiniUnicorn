import assert from "node:assert/strict";
import test from "node:test";

import { generatedTextMatches, normalizeGeneratedText } from "./generated-file-check.mjs";

test("normalizes CRLF and one trailing newline", () => {
  assert.equal(normalizeGeneratedText("a\r\nb\r\n"), "a\nb\n");
  assert.equal(generatedTextMatches("a\r\nb\r\n", "a\nb\n"), true);
});

test("does not hide semantic drift", () => {
  assert.equal(generatedTextMatches("export type A = 1;\r\n", "export type A = 2;\n"), false);
});
