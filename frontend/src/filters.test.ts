import { describe, it, expect } from "vitest";
import { readFilters, writeFilters, period } from "./filters";
import { safeUrl } from "./api";
describe("Shareable catalog filters", () => {
  it("round-trips free, zero-price, Cyrillic and dates", () => {
    const filters = {
      city_id: "city",
      query: "Спектакль & музыка",
      price_max: 0,
      is_free: true,
      page: 2,
      ...period("2026-11-15", "2026-11-16"),
    };
    expect(readFilters(writeFilters(filters))).toEqual(filters);
  });
  it("ignores unknown, negative and invalid inputs", () => {
    expect(
      readFilters(
        new URLSearchParams(
          "page=-1&age=NaN&price_max=-3&unknown=1&is_free=maybe",
        ),
      ),
    ).toEqual({});
  });
  it("preserves false", () =>
    expect(readFilters(new URLSearchParams("is_free=false"))).toEqual({
      is_free: false,
    }));
  it("rejects executable and relative source URLs", () => {
    expect(safeUrl("javascript:alert(1)")).toBeUndefined();
    expect(safeUrl("/admin")).toBeUndefined();
    expect(safeUrl("https://example.org/event")).toBe(
      "https://example.org/event",
    );
  });
});
