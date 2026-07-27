import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BootstrapError,
  deriveWsUrl,
  fetchBootstrap,
  fetchBootstrapWithRetry,
} from "@/lib/bootstrap";

describe("bootstrap helpers", () => {
  it("prefers the server-provided websocket URL over the current dev host", () => {
    expect(deriveWsUrl("/", "tok en", "ws://127.0.0.1:8765/")).toBe(
      "ws://127.0.0.1:8765/?token=tok%20en",
    );
  });

  it("preserves the host socket bridge URL", () => {
    expect(deriveWsUrl("/", "tok en", "miniunicorn-host://engine/")).toBe(
      "miniunicorn-host://engine/?token=tok%20en",
    );
  });

  it("falls back to the current window host for legacy bootstrap payloads", () => {
    expect(deriveWsUrl("/", "tok")).toBe(
      "ws://localhost:3000/?token=tok",
    );
  });
});

describe("BootstrapError", () => {
  it("classifies 401/403 as auth errors", () => {
    expect(new BootstrapError("x", 401).isAuth).toBe(true);
    expect(new BootstrapError("x", 403).isAuth).toBe(true);
    expect(new BootstrapError("x", 500).isAuth).toBe(false);
    expect(new BootstrapError("x", 0).isAuth).toBe(false);
  });

  it("classifies network and 5xx as transient", () => {
    expect(new BootstrapError("x", 0).isTransient).toBe(true);
    expect(new BootstrapError("x", 500).isTransient).toBe(true);
    expect(new BootstrapError("x", 503).isTransient).toBe(true);
    expect(new BootstrapError("x", 401).isTransient).toBe(false);
    expect(new BootstrapError("x", 404).isTransient).toBe(false);
  });
});

describe("fetchBootstrap", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.useRealTimers();
  });

  it("wraps network errors in BootstrapError with status 0", async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new TypeError("failed to fetch"))) as unknown as typeof fetch;
    await expect(fetchBootstrap("", "")).rejects.toMatchObject({
      name: "BootstrapError",
      status: 0,
      isTransient: true,
    });
  });

  it("wraps HTTP errors in BootstrapError with the status code", async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(new Response("nope", { status: 503 })),
    ) as unknown as typeof fetch;
    await expect(fetchBootstrap("", "")).rejects.toMatchObject({
      name: "BootstrapError",
      status: 503,
      isTransient: true,
    });
  });

  it("classifies 401 as auth", async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(new Response("unauth", { status: 401 })),
    ) as unknown as typeof fetch;
    await expect(fetchBootstrap("", "")).rejects.toMatchObject({
      name: "BootstrapError",
      status: 401,
      isAuth: true,
    });
  });
});

describe("fetchBootstrapWithRetry", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.useRealTimers();
  });

  it("retries transient 5xx errors with exponential backoff", async () => {
    let calls = 0;
    globalThis.fetch = vi.fn(() => {
      calls += 1;
      if (calls < 3) {
        return Promise.resolve(new Response("down", { status: 503 }));
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ token: "tok", ws_path: "/", expires_in: 60 }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    }) as unknown as typeof fetch;

    const promise = fetchBootstrapWithRetry("", "", {
      baseDelayMs: 100,
      maxDelayMs: 1_000,
      maxAttempts: 4,
    });

    // Advance through the backoff delays (100ms, 200ms).
    await vi.advanceTimersByTimeAsync(100);
    await vi.advanceTimersByTimeAsync(200);

    const boot = await promise;
    expect(boot.token).toBe("tok");
    expect(calls).toBe(3);
  });

  it("does NOT retry 401 auth errors", async () => {
    let calls = 0;
    globalThis.fetch = vi.fn(() => {
      calls += 1;
      return Promise.resolve(new Response("unauth", { status: 401 }));
    }) as unknown as typeof fetch;

    await expect(
      fetchBootstrapWithRetry("", "", { maxAttempts: 4 }),
    ).rejects.toMatchObject({ status: 401, isAuth: true });
    // Only one attempt — auth errors are not retried.
    expect(calls).toBe(1);
  });

  it("retries network errors (status 0)", async () => {
    let calls = 0;
    globalThis.fetch = vi.fn(() => {
      calls += 1;
      if (calls < 2) {
        return Promise.reject(new TypeError("network down"));
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ token: "tok", ws_path: "/", expires_in: 60 }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    }) as unknown as typeof fetch;

    const promise = fetchBootstrapWithRetry("", "", {
      baseDelayMs: 50,
      maxDelayMs: 1_000,
      maxAttempts: 4,
    });
    await vi.advanceTimersByTimeAsync(50);
    const boot = await promise;
    expect(boot.token).toBe("tok");
    expect(calls).toBe(2);
  });

  it("rethrows the last transient error after exhausting attempts", async () => {
    let calls = 0;
    globalThis.fetch = vi.fn(() => {
      calls += 1;
      return Promise.resolve(new Response("down", { status: 503 }));
    }) as unknown as typeof fetch;

    const promise = fetchBootstrapWithRetry("", "", {
      baseDelayMs: 10,
      maxDelayMs: 100,
      maxAttempts: 3,
    });
    // Attach a no-op catch to prevent unhandled-rejection warnings between
    // the rejection firing (during advanceTimersByTimeAsync) and the
    // assertion below.
    promise.catch(() => {});
    // Advance through all backoff delays.
    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(20);
    await expect(promise).rejects.toMatchObject({ status: 503, isTransient: true });
    expect(calls).toBe(3);
  });

  it("aborts retry loop when the signal is aborted", async () => {
    let calls = 0;
    globalThis.fetch = vi.fn(() => {
      calls += 1;
      return Promise.resolve(new Response("down", { status: 503 }));
    }) as unknown as typeof fetch;

    const controller = new AbortController();
    const promise = fetchBootstrapWithRetry("", "", {
      baseDelayMs: 1000,
      maxAttempts: 10,
      signal: controller.signal,
    });
    promise.catch(() => {});
    // Abort during the first backoff delay.
    await vi.advanceTimersByTimeAsync(50);
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: "BootstrapError" });
    // Only the first attempt ran before the abort.
    expect(calls).toBe(1);
  });
});
