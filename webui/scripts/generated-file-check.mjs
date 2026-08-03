/**
 * Normalize generated file text for comparison by converting CRLF to LF and
 * ensuring exactly one trailing newline. Semantic content is preserved.
 */
export function normalizeGeneratedText(value) {
  return `${value.replace(/\r\n/g, "\n").trimEnd()}\n`;
}

/**
 * Compare current and generated text after normalization. Returns true when
 * they differ only in line endings or trailing whitespace.
 */
export function generatedTextMatches(current, generated) {
  return normalizeGeneratedText(current) === normalizeGeneratedText(generated);
}
