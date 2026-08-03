import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { configDefaults } from "vitest/config";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.MINIUNICORN_API_URL ?? "http://127.0.0.1:8765";
  const hmrPath = "/__miniunicorn_vite_hmr";

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    optimizeDeps: {
      // Radix dialog was introduced mid-session for the mobile sidebar sheet.
      // When Vite re-optimizes it on a running dev server, the browser can race
      // and request stale chunk paths from `.vite/deps`. Excluding it keeps dev
      // reloads stable instead of rewriting those chunk filenames under us.
      exclude: ["@radix-ui/react-dialog"],
    },
    build: {
      outDir: path.resolve(__dirname, "../miniunicorn/web/dist"),
      emptyOutDir: true,
      sourcemap: false,
      rollupOptions: {
        output: {
          manualChunks(id) {
            // Refractor language modules: group into ≤16 deterministic
            // lazy-loaded buckets based on a stable hash of the language
            // filename. This replaces the previous one-file-per-language
            // output (277 chunks) with at most 16 bucket chunks. Buckets
            // are still loaded on demand via prism-async-light's dynamic
            // imports, so pages that render no highlighted code never
            // fetch these chunks.
            if (id.includes("node_modules/refractor/lang/")) {
              const fileName = id.slice(id.lastIndexOf("/") + 1);
              const langName = fileName.replace(/\.js$/, "");
              let hash = 0;
              for (let i = 0; i < langName.length; i++) {
                hash = (Math.imul(hash, 31) + langName.charCodeAt(i)) | 0;
              }
              const bucket = Math.abs(hash) % 16;
              return `syntax-lang-${bucket.toString().padStart(2, "0")}`;
            }
            if (
              id.includes("node_modules/react-syntax-highlighter")
              || id.includes("node_modules/refractor/core")
            ) {
              return "syntax-highlight";
            }
            if (
              id.includes("node_modules/react-markdown")
              || id.includes("node_modules/remark-")
              || id.includes("node_modules/rehype-")
              || id.includes("node_modules/unified")
              || id.includes("node_modules/mdast-")
              || id.includes("node_modules/hast-")
              || id.includes("node_modules/micromark")
              || id.includes("node_modules/unist-")
            ) {
              return "markdown-vendor";
            }
            if (id.includes("node_modules/katex")) {
              return "katex";
            }
            // Split React core into its own chunk so the entry (index) chunk
            // stays under 500 KB. This is a standard vendor split and does
            // not change runtime behavior.
            if (
              id.includes("node_modules/react/")
              || id.includes("node_modules/react-dom/")
              || id.includes("node_modules/react/")
              || id.includes("node_modules/scheduler/")
            ) {
              return "react-vendor";
            }
          },
        },
      },
    },
    server: {
      // Bind to loopback only by default so the dev server is not exposed
      // on other interfaces. The design spec (§4.1) requires Vite to
      // default to 127.0.0.1; users who need remote access can override
      // via the VITE_HOST env var.
      host: env.VITE_HOST ?? "127.0.0.1",
      port: 5173,
      strictPort: true,
      // Keep Vite's HMR socket on a dedicated path. MiniUnicorn's app WebSocket is
      // opened directly from the browser to the gateway, so the dev server
      // should never proxy WebSocket upgrades.
      hmr: {
        path: hmrPath,
      },
      proxy: {
        "/webui": { target, changeOrigin: true },
        "/api": { target, changeOrigin: true },
        "/auth": { target, changeOrigin: true },
      },
    },
    test: {
      environment: "happy-dom",
      globals: true,
      setupFiles: ["./src/tests/setup.ts"],
      // scripts/generated-file-check.test.mjs is a node:test file run via
      // `npm run test:generated-check`; exclude it from vitest discovery so
      // vitest doesn't report "no test suite found" for the node:test API.
      exclude: [...configDefaults.exclude, "scripts/**"],
    },
  };
});
