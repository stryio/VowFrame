import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// VOWFRAME_API_TARGET / VOWFRAME_WEB_PORT let test runs use their own ports
// instead of the developer's running ./dev.sh (which may use the paid generator).
const apiTarget = process.env.VOWFRAME_API_TARGET ?? "http://127.0.0.1:8000";
const port = Number(process.env.VOWFRAME_WEB_PORT ?? 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port,
    strictPort: true,
    proxy: { "/api": apiTarget },
  },
});
