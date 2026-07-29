import "@testing-library/jest-dom/vitest";
import { beforeEach } from "vitest";

import i18n from "@/i18n";

// happy-dom does not emit a doctype by default, which puts the document in
// quirks mode and triggers console warnings (e.g. KaTeX). Inject a real
// HTML5 doctype once at module initialization so tests run in standards
// mode, matching the production ``index.html``.
if (!document.doctype) {
  const doctype = document.implementation.createDocumentType("html", "", "");
  document.insertBefore(doctype, document.documentElement);
}
// happy-dom does not derive ``compatMode`` from the doctype, so force
// standards mode explicitly. KaTeX checks this property at render time.
if (document.compatMode !== "CSS1Compat") {
  Object.defineProperty(document, "compatMode", {
    value: "CSS1Compat",
    configurable: true,
  });
}

function createTestStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.get(String(key)) ?? null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(String(key));
    },
    setItem(key: string, value: string) {
      store.set(String(key), String(value));
    },
  };
}

if (typeof window !== "undefined" && typeof localStorage.setItem !== "function") {
  const storage = createTestStorage();
  Object.defineProperty(window, "localStorage", {
    value: storage,
    configurable: true,
  });
  Object.defineProperty(globalThis, "localStorage", {
    value: storage,
    configurable: true,
    writable: true,
  });
}

// happy-dom doesn't ship with ``crypto.randomUUID``; shim a tiny v4-ish helper.
if (!("randomUUID" in globalThis.crypto)) {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: () =>
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      }),
    configurable: true,
  });
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  document.documentElement.lang = "en";
  document.title = "MiniUnicorn";
  localStorage.setItem("miniunicorn.locale", "en");
});
