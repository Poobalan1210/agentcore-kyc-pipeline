import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Root is this app's own directory so `index.html` here is the entry point, while `root:
// "../.."` would serve the whole frontend/ tree — three separate apps must never share a dev
// server or a build output.
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  // Per-app dep-optimizer cache — see the reviewer app's config for the full reasoning. Short
  // version: all three apps share one `frontend/node_modules`, and a shared `.vite/deps` means
  // two concurrent dev servers delete each other's optimized dependency files mid-serve.
  cacheDir: fileURLToPath(new URL("../../node_modules/.vite-applicant", import.meta.url)),
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
    port: 5173,
  },
});
