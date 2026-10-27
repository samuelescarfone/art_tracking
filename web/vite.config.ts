import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Local dev: forward API calls to `uvicorn server:app --port 8000`.
  server: { proxy: { "/api": "http://localhost:8000" } },
});
