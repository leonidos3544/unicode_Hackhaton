import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Link,
  NavLink,
  Route,
  Routes,
  useParams,
  useSearchParams,
  useLocation,
} from "react-router-dom";
import {
  api,
  post,
  session,
  login,
  logout,
  safeUrl,
  ApiError,
  type Ref,
  type Event,
  type Filters,
  type Result,
  type Answer,
  type Turn,
  type Action,
} from "./api";
import { period, readFilters, writeFilters } from "./filters";

const categories: Record<string, string> = {
  concert: "Концерты",
  theatre: "Театр",
  exhibition: "Выставки",
  festival: "Фестивали",
  cinema: "Кино",
  excursion: "Экскурсии",
  other: "Другое",
};
const date = (value: string) =>
  new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Yekaterinburg",
  }).format(new Date(value));
function money(event: Event) {
  if (event.is_free) return "Бесплатно";
  if (event.price_min === null) return "Цена неизвестна";
  if (event.price_max === null) return `от ${event.price_min} ₽`;
  return event.price_min === event.price_max
    ? `${event.price_min} ₽`
    : `${event.price_min}–${event.price_max} ₽`;
}
function ErrorBox({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  return (
    <div role="alert" className="error">
      <p>
        {error instanceof Error ? error.message : "Не удалось загрузить данные"}
      </p>
      {error instanceof ApiError && (
        <small>Номер запроса: {error.requestId}</small>
      )}
      {retry && <button onClick={retry}>Повторить</button>}
    </div>
  );
}
function useData<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null),
    [error, setError] = useState<unknown>(null),
    [loading, setLoading] = useState(true),
    [version, reload] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    setData(null);
    if (!path) {
      setLoading(false);
      return;
    }
    api<T>(path)
      .then((value) => {
        if (active) setData(value);
      })
      .catch((value) => {
        if (active) setError(value);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [path, version]);
  return { data, error, loading, reload: () => reload((v) => v + 1), setData };
}
function Status({
  loading,
  children,
}: {
  loading: boolean;
  children?: ReactNode;
}) {
  return loading ? (
    <p role="status" className="muted">
      Загружаем…
    </p>
  ) : (
    <>{children}</>
  );
}
function useCities() {
  return useData<Ref[]>("/cities");
}
function CategoryOptions() {
  return (
    <>
      <option value="">Все категории</option>
      {Object.entries(categories).map(([id, name]) => (
        <option key={id} value={id}>
          {name}
        </option>
      ))}
    </>
  );
}
function Sources({ event }: { event: Event }) {
  return (
    <div className="sources">
      <span>Источники</span>
      {event.sources.map((source, i) => (
        <a
          key={i}
          href={safeUrl(source.source_url)}
          target="_blank"
          rel="noreferrer"
        >
          {source.source_name} ↗
        </a>
      ))}
      <small>
        Проверено:{" "}
        {event.verified_at
          ? date(event.verified_at)
          : "время проверки не указано"}
      </small>
    </div>
  );
}
function DemoNotice({ event }: { event: Event }) {
  if (!event.demo_fields?.length) return null;
  return (
    <p className="demo-notice">
      <strong>Учебный пример.</strong> Часть значений придумана для проверки
      плана и не подтверждена организатором.
    </p>
  );
}
function EventCard({
  event,
  compact = false,
}: {
  event: Event;
  compact?: boolean;
}) {
  return (
    <article
      className={
        "event-card " + (event.status === "cancelled" ? "cancelled" : "")
      }
    >
      <div className={"card-art art-" + event.category} aria-hidden="true">
        <span>{categories[event.category] || "Культура"}</span>
        <b>{event.title.slice(0, 1)}</b>
        <i>Башкортостан</i>
      </div>
      <div className="card-body">
        <div className="eyebrow">
          {date(event.date_start)} ·{" "}
          {event.age_limit === null
            ? "Возраст неизвестен"
            : event.age_limit + "+"}
        </div>
        {event.status === "cancelled" && (
          <strong className="error-label">Событие отменено</strong>
        )}
        <DemoNotice event={event} />
        <h3>
          <Link
            to={"/events/" + event.id}
            target={compact ? "_blank" : undefined}
          >
            {event.title}
          </Link>
        </h3>
        <p>
          {event.city} · {event.venue}
        </p>
        {event.recommendation_reason && <p>{event.recommendation_reason}</p>}
        <div className="card-bottom">
          <strong>{money(event)}</strong>
          <Link
            to={"/events/" + event.id}
            target={compact ? "_blank" : undefined}
          >
            Подробнее ↗
          </Link>
        </div>
      </div>
    </article>
  );
}
function FilterForm({
  filters,
  onApply,
}: {
  filters: Filters;
  onApply: (filters: Filters) => void;
}) {
  const cities = useCities();
  return (
    <form
      className="filters"
      key={JSON.stringify(filters)}
      onSubmit={(event) => {
        event.preventDefault();
        const values = new FormData(event.currentTarget);
        const next: Filters = {
          ...period(
            String(values.get("from") || ""),
            String(values.get("to") || ""),
          ),
        };
        ["city_id", "category", "query", "price_max", "age", "sort"].forEach(
          (key) => {
            const value = String(values.get(key) || "");
            if (value)
              next[key] = ["price_max", "age"].includes(key)
                ? Number(value)
                : value;
          },
        );
        if (values.get("is_free")) next.is_free = true;
        onApply(next);
      }}
    >
      <label>
        Город
        <select
          name="city_id"
          aria-label="Город"
          defaultValue={String(filters.city_id || "")}
        >
          <option value="">Все города</option>
          {cities.data?.map((city) => (
            <option key={city.id} value={city.id}>
              {city.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Что интересно
        <select name="category" defaultValue={String(filters.category || "")}>
          <CategoryOptions />
        </select>
      </label>
      <label className="wide">
        Название
        <input
          name="query"
          maxLength={200}
          placeholder="Спектакль, концерт, выставка…"
          defaultValue={String(filters.query || "")}
        />
      </label>
      <label>
        С даты
        <input
          name="from"
          type="date"
          defaultValue={String(filters.date_from || "").slice(0, 10)}
        />
      </label>
      <label>
        До даты, не включая
        <input
          name="to"
          type="date"
          defaultValue={String(filters.date_to || "").slice(0, 10)}
        />
      </label>
      <label>
        Бюджет на человека, ₽
        <input
          name="price_max"
          type="number"
          min="0"
          step="0.01"
          defaultValue={String(filters.price_max ?? "")}
        />
      </label>
      <label>
        Возраст посетителя
        <input
          name="age"
          type="number"
          min="0"
          max="120"
          defaultValue={String(filters.age ?? "")}
        />
      </label>
      <label>
        Порядок
        <select name="sort" defaultValue={String(filters.sort || "date_asc")}>
          <option value="date_asc">По дате</option>
          <option value="price_asc">Сначала дешевле</option>
        </select>
      </label>
      <label className="check">
        <input
          type="checkbox"
          name="is_free"
          defaultChecked={filters.is_free === true}
        />
        Только бесплатно
      </label>
      <button type="submit">Найти события →</button>
      <button type="button" className="secondary" onClick={() => onApply({})}>
        Сбросить
      </button>
      <ErrorBox error={cities.error} retry={cities.reload} />
    </form>
  );
}
function Catalog({ widget = false }: { widget?: boolean }) {
  const [params, setParams] = useSearchParams();
  const filters = readFilters(params);
  const key = writeFilters(filters).toString();
  const result = useData<Result>("/events?" + key);
  const page = Number(filters.page || 1);
  const applyFilters = (next: Filters) => {
    const nextParams = writeFilters(next);
    if (widget && params.has("height"))
      nextParams.set("height", params.get("height")!);
    setParams(nextParams);
  };
  return (
    <>
      <div className="page-title">
        <div>
          <p className="eyebrow">КУЛЬТУРА БАШКОРТОСТАНА</p>
          <h1>{widget ? "Культурная афиша" : "Выберите свой повод выйти"}</h1>
          <p>Театр, музыка и новые впечатления — ближе, чем кажется.</p>
        </div>
        {!widget && (
          <Link className="text-link" to="/chat">
            Подобрать с помощником ↗
          </Link>
        )}
      </div>
      <FilterForm filters={filters} onApply={applyFilters} />
      <ErrorBox error={result.error} retry={result.reload} />
      <Status loading={result.loading}>
        {result.data && (
          <>
            <div className="section-top">
              <h2>Ближайшие события</h2>
              <span>
                {result.data.pagination.total} найдено · время Уфы, UTC+5
              </span>
            </div>
            {!result.data.items.length && (
              <div className="empty">
                <h3>По этим условиям событий нет</h3>
                <p>
                  Попробуйте другую дату или измените фильтры. Неизвестные цены
                  и возраст не считаются подходящими.
                </p>
                {result.data.relaxations.map((item, i) => (
                  <button
                    className="secondary"
                    key={i}
                    onClick={() => applyFilters(item.filters)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            )}
            <div className="cards">
              {result.data.items.map((event) => (
                <EventCard event={event} compact={widget} key={event.id} />
              ))}
            </div>
            <div className="pagination">
              <button
                className="secondary"
                disabled={page === 1}
                onClick={() => applyFilters({ ...filters, page: page - 1 })}
              >
                ← Назад
              </button>
              <span>Страница {page}</span>
              <button
                className="secondary"
                disabled={!result.data.pagination.has_more}
                onClick={() => applyFilters({ ...filters, page: page + 1 })}
              >
                Далее →
              </button>
            </div>
          </>
        )}
      </Status>
    </>
  );
}
function FeedbackForm({ eventId }: { eventId?: string }) {
  const [error, setError] = useState<unknown>(null),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <form
      className="panel form-stack"
      onSubmit={async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const data = new FormData(form);
        setBusy(true);
        setError(null);
        try {
          await session();
          const response = await post<{ ticket_id: string }>("/feedback", {
            kind: data.get("kind"),
            text: data.get("text"),
            website: data.get("website"),
            event_id: eventId || undefined,
          });
          setSuccess("Обращение сохранено. Номер: " + response.ticket_id);
          form.reset();
        } catch (error) {
          setError(error);
        } finally {
          setBusy(false);
        }
      }}
    >
      <h2>Обратная связь</h2>
      <label>
        Тип обращения
        <select name="kind">
          <option value="question">Вопрос</option>
          <option value="correction">Исправление карточки</option>
          <option value="organizer">Я организатор</option>
        </select>
      </label>
      <label>
        Сообщение
        <textarea
          name="text"
          required
          minLength={5}
          maxLength={2000}
          rows={4}
        />
      </label>
      <label className="honeypot" aria-hidden="true">
        Ваш сайт
        <input name="website" tabIndex={-1} autoComplete="off" />
      </label>
      <button disabled={busy}>Отправить обращение</button>
      <ErrorBox error={error} />
      <p role="status">{success}</p>
    </form>
  );
}
function EventPage() {
  const { id } = useParams();
  const result = useData<Event>("/events/" + id);
  return (
    <>
      <Link to="/">← К афише</Link>
      <ErrorBox error={result.error} retry={result.reload} />
      <Status loading={result.loading}>
        {result.data && (
          <>
            <div className="detail-layout">
              <section>
                <p className="eyebrow">
                  {categories[result.data.category]} · {result.data.city}
                </p>
                <h1>{result.data.title}</h1>
                <DemoNotice event={result.data} />
                {result.data.status === "cancelled" && (
                  <p className="error">Событие отменено</p>
                )}
                <p className="lead">
                  {date(result.data.date_start)} ·{" "}
                  {result.data.age_limit === null
                    ? "Возраст не указан"
                    : result.data.age_limit + "+"}
                </p>
                <p className="description">{result.data.description}</p>
                <h2>{result.data.venue}</h2>
                <p>{result.data.address}</p>
                <p>
                  Окончание:{" "}
                  {result.data.date_end
                    ? date(result.data.date_end)
                    : "неизвестно"}
                </p>
              </section>
              <aside className="panel">
                <h2>{money(result.data)}</h2>
                {result.data.org_link && (
                  <a
                    className="button"
                    href={safeUrl(result.data.org_link)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    На сайт организатора ↗
                  </a>
                )}
                <Sources event={result.data} />
                {result.data.data_warnings.map((text, i) => (
                  <p className="muted" key={i}>
                    {text}
                  </p>
                ))}
                <small>Версия {result.data.version}</small>
              </aside>
            </div>
            <FeedbackForm eventId={id} />
          </>
        )}
      </Status>
    </>
  );
}
function ConfirmAction({ action }: { action: Action }) {
  const cities = useCities();
  const payload = action.payload as {
    city_id?: string;
    category?: string;
    channel?: string;
    period_filter?: Record<string, string>;
    text?: string;
    kind?: string;
    event_id?: string;
  };
  const [done, setDone] = useState(""),
    [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  const decide = async (confirm: boolean) => {
    setBusy(true);
    try {
      await post("/actions/" + action.id + "/decision", { confirm });
      setDone(confirm ? "Подтверждено" : "Отклонено");
    } catch (error) {
      setError(error);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="panel">
      <h3>
        {action.action_type === "subscribe"
          ? "Подписка на события"
          : "Обращение модератору"}
      </h3>
      <dl className="action-payload">
        {payload.city_id && (
          <>
            <dt>Город</dt>
            <dd>
              {cities.data?.find((city) => city.id === payload.city_id)?.name ||
                payload.city_id}
            </dd>
          </>
        )}
        {action.action_type === "subscribe" && (
          <>
            <dt>Категория</dt>
            <dd>
              {payload.category
                ? categories[payload.category]
                : "Все категории"}
            </dd>
            <dt>Канал</dt>
            <dd>
              {payload.channel === "telegram"
                ? "Telegram"
                : "Уведомления на сайте"}
            </dd>
            <dt>Период</dt>
            <dd>
              {payload.period_filter?.date_from
                ? date(payload.period_filter.date_from)
                : "Ближайшие события"}
              {payload.period_filter?.date_to
                ? " — до " + date(payload.period_filter.date_to)
                : ""}
            </dd>
          </>
        )}
        {payload.kind && (
          <>
            <dt>Тип</dt>
            <dd>
              {{
                question: "Вопрос",
                correction: "Исправление",
                organizer: "От организатора",
              }[payload.kind] || payload.kind}
            </dd>
          </>
        )}
        {payload.text && (
          <>
            <dt>Сообщение</dt>
            <dd>{payload.text}</dd>
          </>
        )}
        {payload.event_id && (
          <>
            <dt>Событие</dt>
            <dd>
              <Link to={"/events/" + payload.event_id}>Открыть карточку</Link>
            </dd>
          </>
        )}
      </dl>
      <small>Действует до {date(action.expires_at)}</small>
      {done ? (
        <p role="status">{done}</p>
      ) : (
        <div className="actions">
          <button
            disabled={busy || new Date(action.expires_at) < new Date()}
            onClick={() => decide(true)}
          >
            Подтвердить
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => decide(false)}
          >
            Отказаться
          </button>
        </div>
      )}
      <ErrorBox error={error} />
    </div>
  );
}
function Chat() {
  const [params, setParams] = useSearchParams();
  const id = params.get("id");
  const history = useData<{ turns: Turn[] }>(
    id ? "/conversations/" + id : null,
  );
  const [ready, setReady] = useState(false);
  useEffect(() => {
    session()
      .then(() => setReady(true))
      .catch(setError);
  }, []);
  const conversations = useData<{ id: string; created_at: string }[]>(
    ready ? "/conversations" : null,
  );
  const [turns, setTurns] = useState<Turn[]>([]),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const sending = useRef(false);
  const chatInput = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (!busy) chatInput.current?.focus();
  }, [busy]);
  const pending = useRef<{
    id: string;
    text: string;
    conversation: string;
  } | null>(null);
  useEffect(() => {
    if (history.data) setTurns(history.data.turns);
    else setTurns([]);
  }, [history.data, id]);
  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (sending.current) return;
    if (!message.trim() && !pending.current) return;
    sending.current = true;
    setBusy(true);
    setError(null);
    try {
      await session();
      let conversation = id;
      if (!conversation) {
        conversation = (await post<{ id: string }>("/conversations", {})).id;
        setParams({ id: conversation });
      }
      const request = pending.current || {
        id: crypto.randomUUID(),
        text: message,
        conversation,
      };
      pending.current = request;
      const answer = await post<Answer>(
        "/conversations/" + request.conversation + "/messages",
        { message: request.text, client_request_id: request.id },
      );
      setTurns((previous) => [
        ...previous,
        { id: request.id, message: request.text, answer },
      ]);
      pending.current = null;
      setMessage("");
      history.reload();
      conversations.reload();
    } catch (error) {
      setError(error);
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };
  return (
    <>
      <p className="eyebrow">ВАШ КУЛЬТУРНЫЙ ПОМОЩНИК</p>
      <h1>Что вам интересно?</h1>
      <p>
        Например: «Концерты в Уфе завтра до 1000 рублей». Помощник уточнит
        условия и покажет записи из каталога.
      </p>
      <div className="actions">
        <button
          className="secondary"
          disabled={busy}
          onClick={() => {
            setParams({});
            setTurns([]);
            pending.current = null;
          }}
        >
          Новый диалог
        </button>
        <Link to="/">Открыть ручные фильтры ↗</Link>
      </div>
      {conversations.data && conversations.data.length > 0 && (
        <label>
          Предыдущие диалоги
          <select
            value={id || ""}
            disabled={busy}
            onChange={(e) => {
              pending.current = null;
              setParams(e.target.value ? { id: e.target.value } : {});
            }}
          >
            <option value="">Новый диалог</option>
            {conversations.data.map((item) => (
              <option key={item.id} value={item.id}>
                {date(item.created_at)} · {item.id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
      )}
      <ErrorBox error={history.error} retry={history.reload} />
      <div aria-live="polite" className="chat-log">
        {turns.map((turn, index) => (
          <section key={turn.id}>
            <p className="user-message">{turn.message}</p>
            {turn.answer ? (
              <>
                <p className="assistant-message">
                  <span className="eyebrow">
                    {turn.answer.mode === "fallback"
                      ? "ОБЫЧНЫЙ ПОИСК"
                      : "ПОМОЩНИК"}
                  </span>
                  <br />
                  {turn.answer.message}
                </p>
                {index === turns.length - 1 && !busy && (
                  <div className="cards">
                    {turn.answer.items.map((item) => (
                      <EventCard event={item} key={item.id} />
                    ))}
                  </div>
                )}
                {index === turns.length - 1 &&
                  !busy &&
                  turn.answer.details &&
                  turn.answer.details.length > 0 && (
                    <section>
                      <h3>Карточки вне текущей подборки</h3>
                      <p>Эти записи не подтверждают все выбранные условия.</p>
                      <div className="cards">
                        {turn.answer.details.map((item) => (
                          <EventCard event={item} key={item.id} />
                        ))}
                      </div>
                    </section>
                  )}
                {turn.answer.pending_actions.map((action) => (
                  <ConfirmAction action={action} key={action.id} />
                ))}
              </>
            ) : (
              <p>Ход ещё выполняется или был прерван.</p>
            )}
          </section>
        ))}
      </div>
      <form className="chat-form" onSubmit={send}>
        <label>
          Ваш запрос
          <textarea
            ref={chatInput}
            aria-label="Ваш запрос"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                if (!sending.current) event.currentTarget.form?.requestSubmit();
              }
            }}
            aria-describedby="chat-input-hint"
            required={!pending.current}
            maxLength={2000}
            rows={3}
            disabled={busy}
          />
          <small id="chat-input-hint">
            Enter — отправить, Shift+Enter — новая строка.
          </small>
        </label>
        <button disabled={busy}>
          {busy
            ? "Подбираем…"
            : pending.current
              ? "Повторить отправку"
              : "Найти →"}
        </button>
      </form>
      <ErrorBox error={error} />
    </>
  );
}
type Subscription = {
  id: string;
  city: string;
  city_id: string;
  category: string | null;
  channel: string;
  delivery_ready: boolean;
  period_filter: Record<string, string>;
};
function Personal() {
  const [editing, setEditing] = useState<string | null>(null);
  const [ready, setReady] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState("");
  const cities = useCities();
  useEffect(() => {
    session()
      .then(() => setReady(true))
      .catch(setError);
  }, []);
  const subscriptions = useData<Subscription[]>(
      ready ? "/subscriptions" : null,
    ),
    inbox = useData<{ id: string; event: Event }[]>(
      ready ? "/notifications" : null,
    );
  const mutate = async (action: () => Promise<unknown>) => {
    setError(null);
    try {
      await action();
      subscriptions.reload();
      inbox.reload();
    } catch (error) {
      setError(error);
    }
  };
  return (
    <>
      <p className="eyebrow">ЛИЧНЫЙ РАЗДЕЛ</p>
      <h1>Не пропустите интересное</h1>
      <p>
        Подписки сохраняются в текущем браузере. Привяжите Telegram, чтобы
        использовать тот же профиль в боте.
      </p>
      <ErrorBox error={error} />
      <button
        className="secondary"
        onClick={() =>
          mutate(async () => {
            const result = await post<{ url: string | null }>(
              "/sessions/telegram-link",
              {},
            );
            setNotice(result.url || "Telegram-бот пока не настроен");
          })
        }
      >
        Привязать Telegram
      </button>
      {notice &&
        (safeUrl(notice) ? (
          <p>
            <a href={safeUrl(notice)} target="_blank" rel="noreferrer">
              Открыть бота для привязки ↗
            </a>
          </p>
        ) : (
          <p role="status">{notice}</p>
        ))}
      <h2>Мои подписки</h2>
      <ErrorBox error={subscriptions.error} retry={subscriptions.reload} />
      <Status loading={subscriptions.loading}>
        {subscriptions.data?.length === 0 && <p>Подписок пока нет.</p>}
        {subscriptions.data?.map((item) => (
          <div key={item.id} className="panel subscription">
            <span>
              <strong>{item.city}</strong> ·{" "}
              {item.category ? categories[item.category] : "Все категории"} ·{" "}
              {item.channel === "inbox" ? "На сайте" : "Telegram"}
              {!item.delivery_ready && " · требуется привязка Telegram"}
            </span>
            <button
              className="secondary"
              onClick={() =>
                mutate(() =>
                  api("/subscriptions/" + item.id, { method: "DELETE" }),
                )
              }
            >
              Отписаться
            </button>
            <button
              className="secondary"
              onClick={() => setEditing(editing === item.id ? null : item.id)}
            >
              Изменить
            </button>
            {editing === item.id && (
              <SubscriptionEditor
                item={item}
                onSave={(payload) =>
                  mutate(async () => {
                    await api("/subscriptions/" + item.id, {
                      method: "PATCH",
                      body: JSON.stringify(payload),
                    });
                    setEditing(null);
                  })
                }
              />
            )}
          </div>
        ))}
      </Status>
      <form
        className="panel filters"
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          void mutate(() =>
            post("/subscriptions", {
              city_id: data.get("city_id"),
              category: data.get("category") || null,
              channel: data.get("channel"),
              period_filter: period(
                String(data.get("from") || ""),
                String(data.get("to") || ""),
              ),
            }),
          );
        }}
      >
        <label>
          Город
          <select name="city_id" aria-label="Город" required>
            <option value="">Выберите город</option>
            {cities.data?.map((city) => (
              <option key={city.id} value={city.id}>
                {city.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Категория
          <select name="category">
            <CategoryOptions />
          </select>
        </label>
        <label>
          Канал
          <select name="channel">
            <option value="inbox">На сайте</option>
            <option value="telegram">Telegram</option>
          </select>
        </label>
        <label>
          С даты
          <input name="from" type="date" />
        </label>
        <label>
          До даты, не включая
          <input name="to" type="date" />
        </label>
        <button disabled={!ready}>Добавить подписку</button>
      </form>
      <h2>Уведомления</h2>
      <ErrorBox error={inbox.error} retry={inbox.reload} />
      {inbox.data?.length === 0 && <p>Новых уведомлений пока нет.</p>}
      <div className="cards">
        {inbox.data?.map((item) => (
          <EventCard event={item.event} key={item.id} />
        ))}
      </div>
      <FeedbackForm />
    </>
  );
}
function SubscriptionEditor({
  item,
  onSave,
}: {
  item: Subscription;
  onSave: (payload: unknown) => Promise<void>;
}) {
  const cities = useCities();
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="filters subscription-editor"
      onSubmit={async (event) => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        setBusy(true);
        try {
          await onSave({
            city_id: data.get("city_id"),
            category: data.get("category") || null,
            channel: data.get("channel"),
            period_filter: period(
              String(data.get("from") || ""),
              String(data.get("to") || ""),
            ),
          });
        } finally {
          setBusy(false);
        }
      }}
    >
      <label>
        Город
        <select
          name="city_id"
          aria-label="Город"
          defaultValue={item.city_id}
          required
        >
          {cities.data?.map((city) => (
            <option key={city.id} value={city.id}>
              {city.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Категория
        <select name="category" defaultValue={item.category || ""}>
          <CategoryOptions />
        </select>
      </label>
      <label>
        Канал
        <select name="channel" defaultValue={item.channel}>
          <option value="inbox">На сайте</option>
          <option value="telegram">Telegram</option>
        </select>
      </label>
      <label>
        С даты
        <input
          type="date"
          name="from"
          defaultValue={item.period_filter.date_from?.slice(0, 10) || ""}
        />
      </label>
      <label>
        До даты, не включая
        <input
          type="date"
          name="to"
          defaultValue={item.period_filter.date_to?.slice(0, 10) || ""}
        />
      </label>
      <button disabled={busy}>Сохранить подписку</button>
    </form>
  );
}

type AdminRow = Record<string, unknown> & { id: string };
function DataView({ value }: { value: unknown }) {
  return <pre className="data-view">{JSON.stringify(value, null, 2)}</pre>;
}
function Admin() {
  const [authorized, setAuthorized] = useState(false),
    [error, setError] = useState<unknown>(null),
    [tab, setTab] = useState("events"),
    [page, setPage] = useState(1),
    [selection, setSelection] = useState<unknown>(null),
    [editing, setEditing] = useState<Event | null | undefined>(undefined),
    [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const resource = useData<AdminRow[]>(
    authorized
      ? "/admin/" +
          tab +
          (tab === "events"
            ? "?page=" + page + "&query=" + encodeURIComponent(search)
            : "")
      : null,
  );
  useEffect(() => {
    session()
      .then((data) => setAuthorized(data.role === "admin"))
      .catch(setError);
  }, []);
  const run = async (task: () => Promise<unknown>, refresh = true) => {
    setBusy(true);
    setError(null);
    try {
      await task();
      if (refresh) resource.reload();
    } catch (error) {
      setError(error);
    } finally {
      setBusy(false);
    }
  };
  if (!authorized)
    return (
      <div className="narrow">
        <h1>Вход администратора</h1>
        <form
          className="panel form-stack"
          onSubmit={(event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const key = String(new FormData(form).get("key") || "");
            form.reset();
            void run(async () => {
              await login(key);
              setAuthorized(true);
            });
          }}
        >
          <label>
            Ключ доступа
            <input name="key" type="password" required autoComplete="off" />
          </label>
          <button disabled={busy}>Войти</button>
          <ErrorBox error={error} />
        </form>
      </div>
    );
  return (
    <>
      <div className="section-top">
        <h1>Управление афишей</h1>
        <button
          className="secondary"
          onClick={() =>
            run(async () => {
              await logout();
              setAuthorized(false);
            })
          }
        >
          Выйти
        </button>
      </div>
      <nav className="tabs" aria-label="Разделы администратора">
        {Object.entries({
          events: "Карточки",
          imports: "Импорты",
          moderation: "Модерация",
          feedback: "Обращения",
          audit: "Аудит",
          "source-runs": "Сборы",
          traces: "Трассы",
        }).map(([key, title]) => (
          <button
            className={tab === key ? "" : "secondary"}
            key={key}
            onClick={() => {
              setTab(key);
              setSelection(null);
              setEditing(undefined);
              setPage(1);
            }}
          >
            {title}
          </button>
        ))}
      </nav>
      <ErrorBox error={error} />
      <ErrorBox error={resource.error} retry={resource.reload} />
      {tab === "events" && (
        <>
          <label>
            Поиск карточек
            <input
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setPage(1);
              }}
              maxLength={200}
            />
          </label>
          <button onClick={() => setEditing(null)}>Создать карточку</button>
          {editing !== undefined && (
            <EventEditor
              event={editing}
              onSaved={() => {
                setEditing(undefined);
                resource.reload();
              }}
              onClose={() => setEditing(undefined)}
            />
          )}
          <MergeForm onDone={resource.reload} />
        </>
      )}
      {tab === "imports" && (
        <form
          className="panel filters"
          onSubmit={(event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            void run(async () => {
              setSelection(
                await api("/admin/imports", { method: "POST", body: data }),
              );
            });
          }}
        >
          <label>
            Название источника
            <input name="source_name" required maxLength={200} />
          </label>
          <label>
            CSV или JSON
            <input name="file" type="file" required accept=".csv,.json" />
          </label>
          <button disabled={busy}>Импортировать</button>
        </form>
      )}
      <Status loading={resource.loading}>
        <div className="admin-list">
          {resource.data?.length === 0 && <p>Записей нет.</p>}
          {resource.data?.map((row) => (
            <article className="panel" key={row.id}>
              <h3>
                {String(
                  row.title ||
                    row.source_name ||
                    row.kind ||
                    row.action ||
                    row.id,
                )}
              </h3>
              <p>
                {String(row.status || row.mode || row.reason || "")}
                {row.created_at ? " · " + date(String(row.created_at)) : ""}
              </p>
              {tab === "events" ? (
                <div className="actions">
                  <Link to={"/events/" + row.id}>Открыть</Link>
                  <button
                    className="secondary"
                    onClick={() => setEditing(row as unknown as Event)}
                  >
                    Редактировать
                  </button>
                  <button
                    className="secondary"
                    onClick={() =>
                      run(async () =>
                        setSelection(
                          await api("/admin/events/" + row.id + "/evidence"),
                        ),
                      )
                    }
                  >
                    Источники
                  </button>
                  <StatusForm row={row} disabled={busy} run={run} />
                </div>
              ) : (
                <>
                  <button
                    className="secondary"
                    onClick={() =>
                      run(async () =>
                        setSelection(
                          ["imports", "moderation", "traces"].includes(tab)
                            ? await api("/admin/" + tab + "/" + row.id)
                            : row,
                        ),
                      )
                    }
                  >
                    Подробности
                  </button>
                  {tab === "moderation" && (
                    <ModerationDecision row={row} run={run} />
                  )}{" "}
                  {tab === "feedback" && (
                    <>
                      <p>{String(row.text)}</p>
                      <label>
                        Статус
                        <select
                          value={String(row.status)}
                          disabled={busy}
                          onChange={(event) =>
                            run(() =>
                              api("/admin/feedback/" + row.id, {
                                method: "PATCH",
                                body: JSON.stringify({
                                  status: event.target.value,
                                }),
                              }),
                            )
                          }
                        >
                          <option value="new">Новое</option>
                          <option value="reviewed">Просмотрено</option>
                          <option value="resolved">Решено</option>
                        </select>
                      </label>
                    </>
                  )}
                </>
              )}
            </article>
          ))}
        </div>
      </Status>
      {tab === "events" && (
        <div className="pagination">
          <button disabled={page === 1} onClick={() => setPage((v) => v - 1)}>
            Назад
          </button>
          <span>Страница {page}</span>
          <button
            disabled={(resource.data?.length || 0) < 50}
            onClick={() => setPage((v) => v + 1)}
          >
            Далее
          </button>
        </div>
      )}
      {selection !== null && (
        <section className="panel">
          <div className="section-top">
            <h2>Подробности</h2>
            <button className="secondary" onClick={() => setSelection(null)}>
              Закрыть
            </button>
          </div>
          <DataView value={selection} />
        </section>
      )}
    </>
  );
}
function StatusForm({
  row,
  disabled,
  run,
}: {
  row: AdminRow;
  disabled: boolean;
  run: (action: () => Promise<unknown>) => Promise<void>;
}) {
  return (
    <form
      className="actions"
      onSubmit={(event) => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        void run(() =>
          post("/admin/events/" + row.id + "/" + data.get("action"), {
            version: row.version,
            reason: data.get("reason"),
          }),
        );
      }}
    >
      <label>
        Действие
        <select name="action">
          <option value="publish">Опубликовать</option>
          <option value="cancel">Отменить</option>
        </select>
      </label>
      <label>
        Причина
        <input name="reason" required minLength={3} maxLength={1000} />
      </label>
      <button disabled={disabled}>Применить</button>
    </form>
  );
}
function ModerationDecision({
  row,
  run,
}: {
  row: AdminRow;
  run: (action: () => Promise<unknown>, refresh?: boolean) => Promise<void>;
}) {
  const [detail, setDetail] = useState<{
    canonical: Event;
    candidate: unknown;
  } | null>(null);
  return (
    <>
      <button
        className="secondary"
        onClick={() =>
          run(
            async () => setDetail(await api("/admin/moderation/" + row.id)),
            false,
          )
        }
      >
        Сравнить
      </button>
      {detail && (
        <>
          <div className="comparison">
            <div>
              <h4>Сохранено</h4>
              <DataView value={detail.canonical} />
            </div>
            <div>
              <h4>Предложение источника</h4>
              <DataView value={detail.candidate} />
            </div>
          </div>
          <form
            className="actions"
            onSubmit={(event) => {
              event.preventDefault();
              const data = new FormData(event.currentTarget);
              void run(() =>
                post(
                  "/admin/moderation/" + row.id + "/" + data.get("decision"),
                  {
                    version: detail.canonical.version,
                    reason: data.get("reason"),
                  },
                ),
              );
            }}
          >
            <label>
              Решение
              <select name="decision">
                <option value="accept">Принять изменения</option>
                <option value="reject">Отклонить</option>
              </select>
            </label>
            <label>
              Причина
              <input name="reason" required minLength={3} />
            </label>
            <button>Сохранить решение</button>
            <p>
              После принятия результат публикуется отдельно в разделе карточек.
            </p>
          </form>
        </>
      )}
    </>
  );
}
function MergeForm({ onDone }: { onDone: () => void }) {
  const [error, setError] = useState<unknown>(null);
  return (
    <details className="panel">
      <summary>Объединить дубли</summary>
      <form
        className="filters"
        onSubmit={async (event) => {
          event.preventDefault();
          const form = event.currentTarget;
          const data = new FormData(form);
          try {
            setError(null);
            await post("/admin/events/merge", {
              canonical_id: data.get("canonical_id"),
              duplicate_id: data.get("duplicate_id"),
              canonical_version: Number(data.get("canonical_version")),
              duplicate_version: Number(data.get("duplicate_version")),
              reason: data.get("reason"),
            });
            form.reset();
            onDone();
          } catch (error) {
            setError(error);
          }
        }}
      >
        <label>
          ID основной карточки
          <input name="canonical_id" required />
        </label>
        <label>
          Её версия
          <input type="number" min="1" name="canonical_version" required />
        </label>
        <label>
          ID дубля
          <input name="duplicate_id" required />
        </label>
        <label>
          Его версия
          <input type="number" min="1" name="duplicate_version" required />
        </label>
        <label>
          Причина
          <input name="reason" minLength={3} required />
        </label>
        <button>Объединить</button>
        <ErrorBox error={error} />
      </form>
    </details>
  );
}
function EventEditor({
  event,
  onSaved,
  onClose,
}: {
  event: Event | null;
  onSaved: () => void;
  onClose: () => void;
}) {
  const [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  return (
    <form
      className="panel form-stack"
      onSubmit={async (submit) => {
        submit.preventDefault();
        const form = submit.currentTarget;
        const values = new FormData(form);
        const body: Record<string, unknown> = {
          external_id: event?.id || crypto.randomUUID(),
          tags: event?.tags || [],
        };
        [
          "title",
          "category",
          "city",
          "venue",
          "address",
          "description",
          "source_url",
        ].forEach((key) => (body[key] = values.get(key)));
        ["date_start", "date_end"].forEach(
          (key) =>
            (body[key] = values.get(key)
              ? String(values.get(key)) + ":00+05:00"
              : null),
        );
        ["price_min", "price_max", "age_limit"].forEach(
          (key) =>
            (body[key] =
              values.get(key) === "" ? null : Number(values.get(key))),
        );
        body.is_free = values.get("is_free") === "on" ? true : null;
        body.language = event?.language || null;
        body.org_link = event?.org_link || null;
        body.lat = event?.lat ?? null;
        body.lon = event?.lon ?? null;
        setBusy(true);
        setError(null);
        try {
          if (event)
            await api("/admin/events/" + event.id, {
              method: "PATCH",
              body: JSON.stringify({
                version: event.version,
                event: body,
                reason: values.get("reason"),
              }),
            });
          else await post("/admin/events", body);
          onSaved();
        } catch (error) {
          setError(error);
        } finally {
          setBusy(false);
        }
      }}
    >
      <h2>
        {event ? "Редактирование · версия " + event.version : "Новая карточка"}
      </h2>
      {["title", "city", "venue", "address"].map((key) => (
        <label key={key}>
          {
            {
              title: "Название",
              city: "Город",
              venue: "Площадка",
              address: "Адрес",
            }[key]
          }
          <input
            name={key}
            required
            defaultValue={String(event?.[key as keyof Event] || "")}
          />
        </label>
      ))}
      <label>
        Категория
        <select
          name="category"
          required
          defaultValue={event?.category || "concert"}
        >
          {Object.entries(categories).map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <div className="filters">
        {["date_start", "date_end"].map((key) => (
          <label key={key}>
            {key === "date_start" ? "Начало" : "Окончание"} · время Уфы
            <input
              type="datetime-local"
              name={key}
              required={key === "date_start"}
              defaultValue={
                event?.[key as "date_start" | "date_end"]
                  ? new Date(
                      new Date(
                        event[key as "date_start" | "date_end"]!,
                      ).getTime() +
                        5 * 3600000,
                    )
                      .toISOString()
                      .slice(0, 16)
                  : ""
              }
            />
          </label>
        ))}
        {["price_min", "price_max"].map((key) => (
          <label key={key}>
            {key === "price_min" ? "Цена от" : "Цена до"}
            <input
              type="number"
              step="0.01"
              min="0"
              name={key}
              defaultValue={event?.[key as "price_min" | "price_max"] ?? ""}
            />
          </label>
        ))}
        <label>
          Возраст
          <select name="age_limit" defaultValue={event?.age_limit ?? ""}>
            <option value="">Неизвестно</option>
            {[0, 6, 12, 16, 18].map((value) => (
              <option key={value} value={value}>
                {value}+
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            name="is_free"
            type="checkbox"
            defaultChecked={event?.is_free === true}
          />
          Бесплатно
        </label>
      </div>
      <label>
        Описание
        <textarea
          name="description"
          required
          maxLength={10000}
          rows={5}
          defaultValue={event?.description}
        />
      </label>
      <label>
        Ссылка на источник
        <input
          type="url"
          name="source_url"
          required
          defaultValue={event?.source_url || ""}
        />
      </label>
      {event && (
        <label>
          Причина изменения
          <input name="reason" minLength={3} required />
        </label>
      )}
      <ErrorBox error={error} />
      {error instanceof ApiError && error.status === 409 && (
        <p>
          Карточка изменилась. Введённый текст сохранён в форме; откройте
          актуальную карточку в новой вкладке перед повторным редактированием.
        </p>
      )}
      <div className="actions">
        <button disabled={busy}>Сохранить</button>
        <button className="secondary" type="button" onClick={onClose}>
          Закрыть
        </button>
      </div>
    </form>
  );
}
type PlanResult = {
  demo_mode?: boolean;
  assumptions_notice?: string | null;
  message: string;
  plans: {
    events: Event[];
    total_cost: number;
    travel_minutes: number[];
    pricing_note: string;
  }[];
  missing_data: string[];
  travel_scope: string;
  calculated_at: string;
  suggestions: { message: string; value: number }[];
  search_complete: boolean;
};
function Planner() {
  const cities = useCities();
  const [result, setResult] = useState<PlanResult | null>(null),
    [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  return (
    <>
      <p className="eyebrow">МАРШРУТ НА ДЕНЬ</p>
      <h1>День с культурой</h1>
      <p>
        План строится только при наличии подтверждённых цен и времени окончания.
      </p>
      <form
        className="panel filters"
        onSubmit={async (event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          setBusy(true);
          setError(null);
          try {
            setResult(
              await post<PlanResult>("/plans/day", {
                city_id: data.get("city_id"),
                date_from:
                  data.get("date") + "T" + data.get("time_from") + ":00+05:00",
                date_to:
                  data.get("date") + "T" + data.get("time_to") + ":00+05:00",
                budget: Number(data.get("budget")),
                adults: Number(data.get("adults")),
                children_ages: String(data.get("children") || "")
                  .split(",")
                  .filter(Boolean)
                  .map(Number),
                travel_mode: data.get("transport"),
                max_travel_min: Number(data.get("max_travel_min")),
              }),
            );
          } catch (error) {
            setError(error);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          Город
          <select name="city_id" aria-label="Город" required>
            <option value="">Выберите город</option>
            {cities.data?.map((city) => (
              <option key={city.id} value={city.id}>
                {city.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          День
          <input type="date" name="date" required />
        </label>
        <label>
          С<input type="time" name="time_from" defaultValue="10:00" required />
        </label>
        <label>
          До
          <input type="time" name="time_to" defaultValue="22:00" required />
        </label>
        <label>
          Общий бюджет, ₽<input type="number" name="budget" min="0" required />
        </label>
        <label>
          Взрослых
          <input
            type="number"
            name="adults"
            min="0"
            max="10"
            defaultValue="1"
          />
        </label>
        <label>
          Возраст детей, через запятую
          <input name="children" placeholder="6, 12" />
        </label>
        <label>
          Перемещение
          <select name="transport">
            <option value="walk">Пешком</option>
            <option value="car">Автомобиль</option>
            <option value="transit">Общественный транспорт</option>
          </select>
        </label>
        <label>
          Переезд, не более минут
          <input
            type="number"
            name="max_travel_min"
            min="0"
            defaultValue="30"
          />
        </label>
        <button disabled={busy}>Построить план</button>
      </form>
      <ErrorBox error={error} />
      {result !== null && (
        <section aria-live="polite">
          <h2>{result.message}</h2>
          {result.demo_mode && (
            <p className="demo-notice">
              {result.assumptions_notice ||
                "Учебный пример с неподтверждёнными значениями."}
            </p>
          )}
          {result.missing_data.map((text) => (
            <p key={text}>{text}</p>
          ))}
          {result.plans.map((plan, index) => (
            <article className="panel" key={index}>
              <h3>
                Вариант {index + 1} · до {plan.total_cost} ₽
              </h3>
              <p>{plan.pricing_note}</p>
              <div className="cards">
                {plan.events.map((event, i) => (
                  <div key={event.id}>
                    <EventCard event={event} />
                    {i < plan.travel_minutes.length && (
                      <p>
                        До следующего события: около {plan.travel_minutes[i]}{" "}
                        мин.
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </article>
          ))}
          {result.suggestions.length > 0 && (
            <>
              <h3>Проверенные варианты изменения условий</h3>
              {result.suggestions.map((suggestion, index) => (
                <p key={index}>
                  {suggestion.message}: {suggestion.value}
                </p>
              ))}
            </>
          )}
          <p className="muted">
            {result.travel_scope}. Рассчитано {date(result.calculated_at)}.
            {!result.search_complete && " Проверена только часть вариантов."}
          </p>
        </section>
      )}
    </>
  );
}
function Widget() {
  const [params] = useSearchParams();
  const raw = Number(params.get("height") || 600);
  const height = Number.isFinite(raw)
    ? Math.max(240, Math.min(1600, raw))
    : 600;
  return (
    <main className="widget" style={{ maxHeight: height, overflow: "auto" }}>
      <Catalog widget />
    </main>
  );
}
export default function App() {
  const location = useLocation();
  const widget = location.pathname === "/widget";
  return widget ? (
    <Widget />
  ) : (
    <>
      <a className="skip" href="#main">
        К содержимому
      </a>
      <header>
        <div className="header-inner">
          <Link to="/" className="brand">
            <span className="brand-symbol" aria-hidden="true">
              ✳
            </span>
            <span>
              афиша культуры<small>БАШКОРТОСТАН</small>
            </span>
          </Link>
          <nav aria-label="Главная навигация">
            <NavLink end to="/">
              Афиша
            </NavLink>
            <NavLink to="/chat">Помощник</NavLink>
            <NavLink to="/planner">План на день</NavLink>
            <NavLink to="/personal">Мои подписки</NavLink>
          </nav>
        </div>
      </header>
      <main id="main" className="container" key={location.pathname}>
        <Routes>
          <Route path="/" element={<Catalog />} />
          <Route path="/events/:id" element={<EventPage />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/personal" element={<Personal />} />
          <Route path="/admin" element={<Admin />} />
          <Route path="/planner" element={<Planner />} />
          <Route
            path="*"
            element={
              <>
                <h1>Страница не найдена</h1>
                <Link to="/">Открыть афишу</Link>
              </>
            }
          />
        </Routes>
      </main>
      <footer>
        <span>Культура рядом. Впечатления надолго.</span>
        <Link to="/admin">Администратору</Link>
        <a href="/widget" target="_blank" rel="noreferrer">
          Виджет
        </a>
        <small>
          Даты и время — Уфа, UTC+5. Перед посещением проверьте афишу
          организатора.
        </small>
      </footer>
    </>
  );
}
