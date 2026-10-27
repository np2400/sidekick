import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built for https://<domain>/app/ (Caddy serves web/dist there). Locally: http://127.0.0.1:5173, with /api and
// /auth passed to a local Sidekick (uvicorn on :8000) when one is running.
export default defineConfig(({ command }) => ({
  plugins: [react()],
  base: command === "build" ? "/app/" : "/",
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000", "/auth": "http://127.0.0.1:8000" },
  },
}));
