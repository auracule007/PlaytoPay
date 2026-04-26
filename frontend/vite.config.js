import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev we proxy /api to the Django backend so we don't have to fight CORS.
// In production the bundle is served as a static site and points at the
// VITE_API_BASE env var.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
