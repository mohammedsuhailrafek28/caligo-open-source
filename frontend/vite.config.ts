/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dev server proxies /api and /health to the backend so the browser sees ONE origin (cookies are SameSite=Lax and the CSRF
// origin check passes). Override the target with CALIGO_API_URL; nothing here is a secret.
const target = process.env.CALIGO_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { port: 3000, proxy: { "/api": { target, changeOrigin: false }, "/health": { target, changeOrigin: false } } },
  preview: { port: 3000, proxy: { "/api": { target, changeOrigin: false }, "/health": { target, changeOrigin: false } } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test-setup.ts"], css: false },
});
