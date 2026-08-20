import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev (`npm run dev`), API calls proxy to the FastAPI server so both
// hot reload and the real DB work together. In production the API server
// serves the built files itself, same origin, no proxy needed.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
