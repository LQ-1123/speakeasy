import { afterEach, expect, it, vi } from "vitest";

import {
  adEventBody,
  hideDelivery,
  impressionAlreadyReported,
  isHidden,
  markImpressionReported,
  resetHidden,
  safeLandingUrl,
  shouldReportImpression,
} from "./adEvents";

afterEach(() => {
  resetHidden();
  vi.unstubAllGlobals();
});

it("impression needs continuous visibility of at least one second", () => {
  expect(shouldReportImpression(0)).toBe(false);
  expect(shouldReportImpression(999)).toBe(false);
  expect(shouldReportImpression(1000)).toBe(true);
  expect(shouldReportImpression(1800)).toBe(true);
  expect(adEventBody("impression", 1234).payload).toEqual({ visible_ms: 1234 });
  expect(adEventBody("click").payload).toEqual({});
});

it("deduplicates impression per delivery within the session", () => {
  const data = new Map<string, string>();
  vi.stubGlobal("window", {
    sessionStorage: {
      getItem: (key: string) => data.get(key) ?? null,
      setItem: (key: string, value: string) => void data.set(key, value),
    },
  });
  expect(impressionAlreadyReported("ad_1")).toBe(false);
  markImpressionReported("ad_1");
  expect(impressionAlreadyReported("ad_1")).toBe(true);
  expect(impressionAlreadyReported("ad_2")).toBe(false);
  expect(impressionAlreadyReported("")).toBe(true);
});

it("hidden deliveries are not re-rendered after close/report", () => {
  expect(isHidden("ad_9")).toBe(false);
  hideDelivery("ad_9");
  expect(isHidden("ad_9")).toBe(true);
  resetHidden();
  expect(isHidden("ad_9")).toBe(false);
});

it("only http(s) landing urls are clickable", () => {
  expect(safeLandingUrl("https://example.test/ad")).toBe("https://example.test/ad");
  expect(safeLandingUrl("http://example.test/ad")).toBe("http://example.test/ad");
  expect(safeLandingUrl("javascript:alert(1)")).toBe("");
  expect(safeLandingUrl("")).toBe("");
  expect(safeLandingUrl(undefined)).toBe("");
});
