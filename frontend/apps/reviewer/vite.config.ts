import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  // Per-app dep-optimizer cache. All three apps share one `frontend/node_modules`, so with the
  // default `node_modules/.vite` every app writes to the same `deps/` directory — running two
  // dev servers at once makes the second one's re-optimization delete the hashed files the first
  // is still serving, which surfaces as "The file does not exist at .../deps/index.browser-
  // XXXX.js which is in the optimize deps directory" and a half-loaded app. Separate cache dirs
  // make concurrent dev servers independent.
  cacheDir: fileURLToPath(new URL("../../node_modules/.vite-reviewer", import.meta.url)),
  plugins: [react()],
  resolve: {
    alias: {
      "@shared": fileURLToPath(new URL("../../shared", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5174,
  },
});
