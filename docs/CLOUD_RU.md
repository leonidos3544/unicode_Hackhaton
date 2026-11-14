# Подключение ИИ через Cloud.ru

Каталог запускается без ключа модели. Чтобы включить настоящий ИИ в веб-чате и Telegram, получите свой ключ сервиса Foundation Models и заполните локальный `.env`.

Проверенная конфигурация:

```dotenv
LLM_API_URL=https://foundation-models.api.cloud.ru/v1/chat/completions
LLM_MODEL=ai-sage/GigaChat3-10B-A1.8B
LLM_REQUIRE_API_KEY=true
LLM_API_KEY=вставьте_свой_Key_Secret
LLM_FREE=false
LLM_INPUT_RUB_PER_MILLION=12.20
LLM_OUTPUT_RUB_PER_MILLION=12.20
LLM_TURN_BUDGET_RUB=1
LLM_DIALOGUE_BUDGET_RUB=3
LLM_DAILY_BUDGET_RUB=20
```

Ключ — **Key Secret** сервисного аккаунта Cloud.ru с доступом к Foundation Models. Backend отправляет его как `Authorization: Bearer …`; браузеру и Telegram-пользователю ключ не передаётся. Реальные ключи в репозиторий не включаются.

12,20 ₽ за миллион входных и генерируемых токенов — опубликованный тариф этой модели, проверенный 14 ноября 2026 года. Перед использованием проверьте тариф своего аккаунта. При смене модели замените её название и обе стоимости. `LLM_FREE=true` используется только для действительно бесплатного провайдера; учёт платного Cloud.ru включён при `false`.

После редактирования `.env` пересоздайте процессы:

```bash
docker compose up -d api bot
```

Откройте http://localhost:8081/chat и напишите «Спектакли в Стерлитамаке». В Telegram отправьте `/chat`. Диалог использует город, дату, возраст и бюджет из проверенных условий, а карточки получает из каталога.

Если модель не настроена, исчерпан бюджет или провайдер задерживается, приложение сообщает причину и использует обычный поиск. `/health` показывает наличие настроек, но само по себе не подтверждает успешный платный вызов.

При установленном окружении разработчика можно проверить реальный сценарий:

```powershell
uv run python -m scripts.check_demo --with-ai
```

Будет создан локальный `reports/demo/preflight.json`; программа не подтверждает подписки и не отправляет сообщения Telegram. Полные условия проверки и ограничения: [VALIDATION.md](VALIDATION.md).

Официальная документация: [API](https://cloud.ru/docs/foundation-models/ug/topics/api-ref__specs), [аутентификация](https://cloud.ru/docs/foundation-models/ug/topics/api-ref__authentication), [модели](https://cloud.ru/docs/foundation-models/ug/topics/overview__available__models), [тарифы](https://cdn.cloud.ru/docs/legal/tariffs/evolution/current-version/foundation-models.pdf).
