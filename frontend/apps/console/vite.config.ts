import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// The one web app: the applicant's journey, the live view of what AWS does with it, the review
// queue, every case, and the demo controls, behind one sign-in on one port. `strictPort` because
// the runbook and the presenter both expect http://localhost:5173: if something else holds the
// port, failing is better than quietly serving on another one.
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  cacheDir: fileURLToPath(new URL("../../node_modules/.vite-console", import.meta.url)),
  plugins: [react()],
  resolve: {
    alias: {
      "@shared": fileURLToPath(new URL("../../shared", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // One local app, not a public site: the Face Liveness detector and its face-detection model
    // are most of the bundle, and splitting them buys nothing on a presenter's laptop.
    chunkSizeWarningLimit: 2600,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
