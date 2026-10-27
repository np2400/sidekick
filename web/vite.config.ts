import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Local preview only for now: http://127.0.0.1:5173. Not deployed, not connected to the production backend.
export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173 },
});
