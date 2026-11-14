import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
let firstId = "",
  cityId = "";
const headers = { Authorization: "Bearer browser-test-only" };
test.beforeAll(async ({ request }) => {
  const start = new Date(Date.now() + 864000000);
  start.setUTCHours(10, 0, 0, 0);
  const events = Array.from({ length: 13 }, (_, index) => ({
    external_id: "browser-" + index,
    title: "Проверка афиши " + String(index).padStart(2, "0"),
    category: "concert",
    city: "Демоград",
    venue: "Тестовая сцена",
    address: "Тестовый адрес",
    description: "Искусственное событие только для браузерных тестов.",
    date_start: new Date(+start + index * 3600000).toISOString(),
    date_end: new Date(+start + (index + 1) * 3600000).toISOString(),
    price_min: 300,
    price_max: 500,
    age_limit: 6,
    source_url: "https://example.org/browser-test/" + index,
  }));
  const response = await request.post("/api/v1/admin/imports", {
    headers,
    multipart: {
      source_name: "browser-tests",
      file: {
        name: "events.json",
        mimeType: "application/json",
        buffer: Buffer.from(JSON.stringify(events)),
      },
    },
  });
  expect(response.status()).toBe(201);
  const report = await response.json();
  for (const row of report.rows) {
    const item = await (
      await request.get("/api/v1/admin/events/" + row.event_id, { headers })
    ).json();
    cityId = item.city_id;
    await request.post("/api/v1/admin/events/" + item.id + "/publish", {
      headers,
      data: { version: item.version, reason: "Проверено в браузерном тесте" },
    });
    if (!firstId) firstId = item.id;
  }
});
test("catalog filters, URL, paging, source and refresh", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Город", { exact: true }).selectOption(cityId);
  await page.getByRole("button", { name: "Найти события" }).click();
  await expect(page.locator(".event-card")).toHaveCount(12);
  await expect(page).toHaveURL(new RegExp(cityId));
  await page.getByRole("button", { name: "Далее" }).click();
  await expect(page.locator(".event-card")).toHaveCount(1);
  await page.reload();
  await expect(page.locator(".event-card")).toHaveCount(1);
  await page.goBack();
  await expect(page.locator(".event-card")).toHaveCount(12);
  await page.locator(".event-card h3 a").first().click();
  await expect(page.locator(".sources a").first()).toHaveAttribute(
    "href",
    /^https:\/\/example.org/,
  );
  await page.reload();
  await expect(page.locator("h1")).toContainText("Проверка афиши");
});
test("subscriptions create, edit, feedback and delete", async ({ page }) => {
  await page.goto("/personal");
  const form = page.locator("form.filters").last();
  await form.getByLabel("Город", { exact: true }).selectOption(cityId);
  await form.getByRole("button", { name: "Добавить подписку" }).click();
  await expect(page.locator(".subscription")).toHaveCount(1);
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  await page
    .locator(".subscription-editor")
    .getByLabel("Категория")
    .selectOption("concert");
  await page.getByRole("button", { name: "Сохранить подписку" }).click();
  await expect(page.locator(".subscription")).toContainText("Концерты");
  await page
    .getByLabel("Сообщение", { exact: true })
    .fill("Проверка обратной связи из браузера");
  await page.getByRole("button", { name: "Отправить обращение" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Обращение сохранено" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Отписаться" }).click();
  await expect(page.locator(".subscription")).toHaveCount(0);
});
test("chat history and explicit confirmation", async ({ page }) => {
  await page.goto("/chat");
  await page.getByLabel("Ваш запрос").fill("Концерты в Демограде");
  await page.getByRole("button", { name: "Найти →", exact: true }).click();
  await expect(page.locator(".chat-log .event-card").first()).toBeVisible();
  await page.getByLabel("Ваш запрос").fill("Подпиши на концерты в Демограде");
  await page.getByRole("button", { name: "Найти →", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Подтвердить", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Подтвердить", exact: true }).click();
  await expect(page.getByText("Подтверждено", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator(".user-message")).toHaveCount(2);
});
test("chat Enter sends, Shift+Enter inserts a line and only latest cards remain", async ({
  page,
}) => {
  await page.goto("/chat");
  const input = page.getByLabel("Ваш запрос");
  await input.fill("Концерты в Демограде");
  await input.press("Enter");
  await expect(page.locator(".chat-log .event-card")).toHaveCount(5);
  await expect(input).toHaveValue("");
  await expect(input).toBeFocused();
  await input.fill("дешевле");
  await input.press("Shift+Enter");
  await expect(input).toHaveValue("дешевле\n");
  await expect(page.locator(".user-message")).toHaveCount(1);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/conversations/*/messages", async (route) => {
    await gate;
    await route.continue();
  });
  await input.press("Enter");
  await expect(input).toBeDisabled();
  await expect(page.locator(".chat-log .event-card")).toHaveCount(0);
  release();
  await expect(input).toBeEnabled();
  await expect(page.locator(".user-message")).toHaveCount(2);
  await expect(page.locator(".chat-log .event-card")).toHaveCount(5);
  await page.reload();
  await expect(page.locator(".user-message")).toHaveCount(2);
  await expect(page.locator(".chat-log .event-card")).toHaveCount(5);
});

test("admin login, edit and version conflict keeps entered text", async ({
  page,
  request,
}) => {
  await page.goto("/admin");
  await page.getByLabel("Ключ доступа").fill("browser-test-only");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Управление афишей" }),
  ).toBeVisible();
  await page.getByLabel("Поиск карточек").fill("Проверка афиши 00");
  await page
    .getByRole("button", { name: "Редактировать", exact: true })
    .click();
  const editor = page.locator("form.form-stack");
  await editor
    .getByLabel("Название", { exact: true })
    .fill("Введённый текст для проверки конфликта");
  await editor.getByLabel("Причина изменения").fill("Тест конфликта");
  const item = await (
    await request.get("/api/v1/admin/events/" + firstId, { headers })
  ).json();
  await request.post("/api/v1/admin/events/" + firstId + "/cancel", {
    headers,
    data: {
      version: item.version,
      reason: "Изменение параллельным администратором",
    },
  });
  await editor.getByRole("button", { name: "Сохранить", exact: true }).click();
  await expect(editor.getByRole("alert")).toBeVisible();
  await expect(editor.getByLabel("Название", { exact: true })).toHaveValue(
    "Введённый текст для проверки конфликта",
  );
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Вход администратора" }),
  ).toBeVisible();
});
test("mobile layout, keyboard focus and accessibility", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/");
  await page.locator(".event-card").first().waitFor();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "К содержимому" })).toBeFocused();
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(result.violations).toEqual([]);
});

test("admin import, moderation, publication and duplicate alias", async ({
  page,
  request,
}) => {
  const suffix = Date.now().toString();
  const title = "Проверка модерации " + suffix;
  const source = "moderation-ui-" + suffix;
  const start = new Date(Date.now() + 12 * 86400000).toISOString();
  const payload = {
    external_id: "item",
    title,
    category: "concert",
    city: "Демоград",
    venue: "Тестовая сцена",
    address: "Тестовый адрес",
    description: "Искусственная запись для проверки модерации.",
    date_start: start,
    date_end: new Date(Date.parse(start) + 3600000).toISOString(),
    price_min: 300,
    price_max: 500,
    age_limit: 6,
    source_url: "https://example.org/moderation-test",
  };
  await page.goto("/admin");
  await page.getByLabel("Ключ доступа").fill("browser-test-only");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await page.getByRole("button", { name: "Импорты", exact: true }).click();
  await page.getByLabel("Название источника").fill(source);
  await page.getByLabel("CSV или JSON").setInputFiles({
    name: "events.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify([payload])),
  });
  const uploaded = page.waitForResponse(
    (response) =>
      response.url().endsWith("/admin/imports") &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Импортировать", exact: true })
    .click();
  const report = await (await uploaded).json();
  expect(report.counts.added).toBe(1);
  const id = report.rows[0].event_id;
  const second = await request.post("/api/v1/admin/imports", {
    headers,
    multipart: {
      source_name: source + "-second",
      file: {
        name: "events.json",
        mimeType: "application/json",
        buffer: Buffer.from(JSON.stringify([{ ...payload, price_max: 700 }])),
      },
    },
  });
  expect((await second.json()).counts.moderation).toBe(1);
  const pending = await (
    await request.get("/api/v1/admin/moderation", { headers })
  ).json();
  const candidate = pending.find(
    (item: { event_id: string }) => item.event_id === id,
  );
  await page.getByRole("button", { name: "Модерация", exact: true }).click();
  const row = page
    .locator(".admin-list article")
    .filter({ hasText: candidate.id });
  await row.getByRole("button", { name: "Сравнить", exact: true }).click();
  await expect(
    row.getByRole("heading", { name: "Предложение источника" }),
  ).toBeVisible();
  await row
    .getByLabel("Причина", { exact: true })
    .fill("Сверено в тестовом источнике");
  await row
    .getByRole("button", { name: "Сохранить решение", exact: true })
    .click();
  await expect(row).toHaveCount(0);
  const accepted = await (
    await request.get("/api/v1/admin/events/" + id, { headers })
  ).json();
  expect(accepted.price_max).toBe(700);
  expect(accepted.status).toBe("draft");
  await page.getByRole("button", { name: "Карточки", exact: true }).click();
  await page.getByLabel("Поиск карточек").fill(title);
  const card = page.locator(".admin-list article").filter({ hasText: title });
  await card
    .getByLabel("Причина", { exact: true })
    .fill("Публикация после модерации");
  await card.getByRole("button", { name: "Применить", exact: true }).click();
  await expect
    .poll(
      async () =>
        (await (await request.get("/api/v1/events/" + id)).json()).status,
    )
    .toBe("published");
  const duplicateResponse = await request.post("/api/v1/admin/events", {
    headers,
    data: {
      ...payload,
      external_id: "duplicate-" + suffix,
      title: title + " повтор",
    },
  });
  expect(duplicateResponse.status()).toBe(201);
  const duplicate = await duplicateResponse.json();
  const current = await (
    await request.get("/api/v1/admin/events/" + id, { headers })
  ).json();
  const merge = page.locator("details").filter({ hasText: "Объединить дубли" });
  await merge.locator("summary").click();
  await merge.locator("[name=canonical_id]").fill(id);
  await merge.locator("[name=canonical_version]").fill(String(current.version));
  await merge.locator("[name=duplicate_id]").fill(duplicate.id);
  await merge
    .locator("[name=duplicate_version]")
    .fill(String(duplicate.version));
  await merge
    .getByLabel("Причина", { exact: true })
    .fill("Подтверждённый дубль в тесте");
  await merge.getByRole("button", { name: "Объединить", exact: true }).click();
  await expect
    .poll(
      async () =>
        (await (await request.get("/api/v1/events/" + duplicate.id)).json()).id,
    )
    .toBe(id);
  for (const name of [
    "Карточки",
    "Импорты",
    "Модерация",
    "Обращения",
    "Аудит",
    "Сборы",
    "Трассы",
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(page.locator(".muted[role=status]")).toHaveCount(0);
    const accessibility = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    expect(accessibility.violations).toEqual([]);
  }
});
