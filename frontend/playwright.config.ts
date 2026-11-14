import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:5174",
    headless: true,
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
    },
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command:
        "uv run --locked python -m uvicorn scripts.e2e_app:app --host 127.0.0.1 --port 8001",
      cwd: "..",
      url: "http://127.0.0.1:8001/health/ready",
      reuseExistingServer: false,
    },
    {
      command: "npm run dev -- --port 5174 --strictPort",
      url: "http://127.0.0.1:5174",
      env: {
        API_PROXY_TARGET: "http://127.0.0.1:8001",
        API_PROXY_ORIGIN: "http://localhost:8080",
      },
      reuseExistingServer: false,
    },
  ],
});
