import type { Filters } from "./api";
const keys = new Set([
  "city_id",
  "category",
  "query",
  "date_from",
  "date_to",
  "price_max",
  "age",
  "is_free",
  "page",
  "sort",
]);
export function readFilters(params: URLSearchParams): Filters {
  const result: Filters = {};
  params.forEach((value, key) => {
    if (keys.has(key) && value) {
      if (["page", "price_max", "age"].includes(key)) {
        const n = Number(value);
        if (
          Number.isFinite(n) &&
          n >= 0 &&
          (key !== "page" || (Number.isInteger(n) && n >= 1))
        )
          result[key] = n;
      } else if (key === "is_free") {
        if (value === "true" || value === "false")
          result[key] = value === "true";
      } else result[key] = value;
    }
  });
  return result;
}
export function writeFilters(filters: Filters) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (keys.has(key) && String(value) !== "") params.set(key, String(value));
  });
  return params;
}
export function period(start: string, end: string): Filters {
  return {
    ...(start ? { date_from: start + "T00:00:00+05:00" } : {}),
    ...(end ? { date_to: end + "T00:00:00+05:00" } : {}),
  };
}
