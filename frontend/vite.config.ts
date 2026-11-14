import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
export default defineConfig(({ mode }) => ({
  server: {
    proxy: {
      "/api": {
        target: process.env.API_PROXY_TARGET || "http://127.0.0.1:8000",
        headers: {
          Origin:
            process.env.API_PROXY_ORIGIN ||
            loadEnv(mode, "..", "PUBLIC_BASE_URL").PUBLIC_BASE_URL ||
            "http://localhost:8080",
        },
      },
      "/health": "http://127.0.0.1:8000",
    },
  },
  test: { include: ["src/**/*.test.ts"] },
}));
