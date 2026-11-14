export type Ref = { id: string; name: string };
export type Event = {
  id: string;
  title: string;
  category: string;
  date_start: string;
  date_end: string | null;
  city_id: string;
  city: string;
  venue_id: string;
  venue: string;
  lat: number | null;
  lon: number | null;
  address: string;
  description: string;
  price_min: number | null;
  price_max: number | null;
  is_free: boolean | null;
  age_limit: number | null;
  language: string | null;
  tags: string[];
  status: string;
  version: number;
  source_url: string | null;
  verified_at: string | null;
  org_link: string | null;
  sources: {
    source_url: string;
    source_name: string;
    verified_at: string | null;
  }[];
  data_warnings: string[];
  demo_fields?: string[];
  recommendation_reason?: string;
};
export type Filters = Record<string, string | number | boolean>;
export type Result = {
  items: Event[];
  pagination: { page: number; total: number; has_more: boolean };
  relaxations: { label: string; filters: Filters }[];
};
export type Action = {
  id: string;
  action_type: string;
  payload: unknown;
  expires_at: string;
};
export type Answer = {
  message: string;
  items: Event[];
  pending_actions: Action[];
  mode: string;
  question: string | null;
  trace_id: string;
  details?: Event[];
  relaxations?: { label: string; filters: Filters }[];
};
export type Turn = { id: string; message: string; answer: Answer | null };
let csrf = "";
let initializing: Promise<{ csrf_token: string; role: string }> | null = null;
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public requestId: string,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (csrf) headers.set("X-CSRF-Token", csrf);
  const response = await fetch("/api/v1" + path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error?.message || "Не удалось выполнить запрос",
      response.headers.get("X-Request-ID") || "",
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export function session() {
  if (!initializing)
    initializing = api<{ csrf_token: string; role: string }>("/sessions", {
      method: "POST",
    })
      .then((data) => {
        csrf = data.csrf_token;
        return data;
      })
      .catch((error) => {
        initializing = null;
        throw error;
      });
  return initializing;
}
export async function login(key: string) {
  const data = await api<{ csrf_token: string; role: string }>(
    "/admin/auth/login",
    { method: "POST", body: JSON.stringify({ api_key: key }) },
  );
  csrf = data.csrf_token;
  initializing = Promise.resolve(data);
  return data;
}
export async function logout() {
  await api("/sessions/current", { method: "DELETE" });
  csrf = "";
  initializing = null;
}
export const post = <T>(path: string, data: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(data) });
export function safeUrl(value: string | null | undefined) {
  try {
    const url = new URL(value || "");
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
