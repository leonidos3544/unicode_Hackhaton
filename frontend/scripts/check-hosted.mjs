import assert from "node:assert/strict";
import { createServer } from "node:http";
import { chromium } from "@playwright/test";

const base = process.argv[2] || "http://localhost:8081";
const widget = new URL("/widget?height=480", base);
for (const path of ["/widget", "/admin", "/events/example"]) {
  const response = await fetch(new URL(path, base));
  assert.equal(response.status, 200);
  const policy = response.headers.get("content-security-policy");
  assert.ok(
    policy?.includes(
      path === "/widget" ? "frame-ancestors *" : "frame-ancestors 'none'",
    ),
  );
  assert.ok(response.headers.get("content-type")?.includes("text/html"));
}
const host = createServer((request, response) => {
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  response.end(
    `<!doctype html><html lang="ru"><title>Iframe check</title><iframe title="Афиша" src="${widget.href}" width="400" height="500"></iframe></html>`,
  );
});
await new Promise((resolve) => host.listen(0, "127.0.0.1", resolve));
const address = host.address();
const executablePath =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ||
  (process.platform === "win32"
    ? "C:/Program Files/Google/Chrome/Application/chrome.exe"
    : undefined);
const browser = await chromium.launch({
  headless: true,
  executablePath,
  args: ["--test-third-party-cookie-phaseout"],
});
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  const sessionCalls = [];
  page.on("request", (request) => {
    if (request.url().includes("/sessions")) sessionCalls.push(request.url());
  });
  // Remove cookies from public API calls even if the browser has an existing profile policy.
  await page.route("**/api/**", async (route) => {
    const headers = { ...route.request().headers() };
    delete headers.cookie;
    await route.continue({ headers });
  });
  await page.goto(`http://127.0.0.1:${address.port}/`);
  await page.frameLocator("iframe").locator(".event-card").first().waitFor();
  assert.equal(sessionCalls.length, 0);
  const cards = await page
    .frameLocator("iframe")
    .locator(".event-card")
    .count();
  assert.ok(cards > 0);
  console.log(
    JSON.stringify({
      base,
      iframeCards: cards,
      sessionCalls: sessionCalls.length,
      publicApiWithoutCookies: true,
      framePolicyVerified: true,
    }),
  );
} finally {
  await browser.close();
  await new Promise((resolve) => host.close(resolve));
}
