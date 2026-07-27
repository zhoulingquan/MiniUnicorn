import type { BootstrapResponse } from "./types";
import { STORAGE_KEYS } from "./storage";

const SECRET_STORAGE_KEY = STORAGE_KEYS.bootstrapSecret;

/** Error thrown by ``fetchBootstrap`` when the request fails.
 *
 * Carries the HTTP ``status`` so callers can distinguish authentication
 * failures (401/403 — permanent, must re-prompt) from transient errors
 * (network, 5xx — retryable with backoff). ``status`` is ``0`` for
 * network-level failures where no HTTP response was received.
 */
export class BootstrapError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "BootstrapError";
    this.status = status;
  }

  /** Auth failure (401/403) — caller should transition to auth state. */
  get isAuth(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /** Transient failure (network error or 5xx) — caller may retry. */
  get isTransient(): boolean {
    return this.status === 0 || (this.status >= 500 && this.status < 600);
  }
}

/** Read a previously saved bootstrap secret from localStorage. */
export function loadSavedSecret(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(SECRET_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

/** Persist the bootstrap secret so page reloads don't re-prompt. */
export function saveSecret(secret: string): void {
  try {
    window.localStorage.setItem(SECRET_STORAGE_KEY, secret);
  } catch {
    // ignore storage errors (private mode, etc.)
  }
}

/** Clear the saved bootstrap secret (sign out). */
export function clearSavedSecret(): void {
  try {
    window.localStorage.removeItem(SECRET_STORAGE_KEY);
  } catch {
    // ignore
  }
}

/**
 * Fetch a short-lived token + the WebSocket path from the gateway's
 * ``/webui/bootstrap`` endpoint.
 *
 * Throws ``BootstrapError`` with a ``status`` field so callers can
 * distinguish auth failures (401/403) from transient errors (network, 5xx).
 */
export async function fetchBootstrap(
  baseUrl: string = "",
  secret: string = "",
): Promise<BootstrapResponse> {
  const headers: Record<string, string> = {};
  if (secret) {
    headers["x-miniunicorn-Auth"] = secret;
  }
  let res: Response;
  try {
    res = await fetch(`${baseUrl}/webui/bootstrap`, {
      method: "GET",
      credentials: "same-origin",
      headers,
    });
  } catch (e) {
    // Network error (DNS, connection refused, CORS, etc.) — status 0.
    throw new BootstrapError(
      `bootstrap failed: network error — ${(e as Error).message}`,
      0,
    );
  }
  if (!res.ok) {
    throw new BootstrapError(`bootstrap failed: HTTP ${res.status}`, res.status);
  }
  const body = (await res.json()) as BootstrapResponse;
  if (!body.token || !body.ws_path) {
    throw new BootstrapError("bootstrap response missing token or ws_path", 0);
  }
  return body;
}

/** Retry a bootstrap fetch with capped exponential backoff.
 *
 * - 401/403 (auth): immediately rejected — caller must re-prompt.
 * - Network/5xx (transient): retried up to ``maxAttempts`` times with
 *   exponential backoff (``baseDelayMs * 2^attempt``), capped at
 *   ``maxDelayMs``.
 *
 * Returns the successful ``BootstrapResponse``. If all attempts are
 * exhausted, re-throws the last ``BootstrapError``.
 */
export async function fetchBootstrapWithRetry(
  baseUrl: string = "",
  secret: string = "",
  options: {
    maxAttempts?: number;
    baseDelayMs?: number;
    maxDelayMs?: number;
    signal?: AbortSignal;
  } = {},
): Promise<BootstrapResponse> {
  const maxAttempts = options.maxAttempts ?? 4;
  const baseDelayMs = options.baseDelayMs ?? 500;
  const maxDelayMs = options.maxDelayMs ?? 8_000;
  const signal = options.signal;

  let lastError: BootstrapError | null = null;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (signal?.aborted) {
      throw new BootstrapError("bootstrap aborted", 0);
    }
    try {
      return await fetchBootstrap(baseUrl, secret);
    } catch (e) {
      if (!(e instanceof BootstrapError)) {
        // Shouldn't happen — wrap defensively.
        lastError = new BootstrapError((e as Error).message, 0);
      } else {
        lastError = e;
      }
      // Auth errors are not retryable.
      if (lastError.isAuth) {
        throw lastError;
      }
      // Transient: back off before the next attempt (skip on the last one).
      if (attempt < maxAttempts - 1) {
        const delay = Math.min(maxDelayMs, baseDelayMs * 2 ** attempt);
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(resolve, delay);
          const onAbort = () => {
            clearTimeout(timer);
            reject(new BootstrapError("bootstrap aborted", 0));
          };
          signal?.addEventListener("abort", onAbort, { once: true });
        });
      }
    }
  }
  throw lastError ?? new BootstrapError("bootstrap failed: unknown", 0);
}

/** Derive a WebSocket URL from the current window location and the server-provided path.
 *
 * Keeps the path segment exactly as the server registered it: the root ``/``
 * stays ``/`` and non-root paths are not given an extra trailing slash. This
 * matters because some WS servers dispatch handshakes based on the literal
 * path, not a normalised form.
 */
export function deriveWsUrl(
  wsPath: string,
  token: string,
  wsUrl?: string | null,
): string {
  const query = `?token=${encodeURIComponent(token)}`;
  if (wsUrl && /^(wss?|miniunicorn-host):\/\//i.test(wsUrl)) {
    const join = wsUrl.includes("?") ? "&" : "?";
    return `${wsUrl}${join}token=${encodeURIComponent(token)}`;
  }
  const path = wsPath && wsPath.startsWith("/") ? wsPath : `/${wsPath || ""}`;
  if (typeof window === "undefined") {
    return `ws://127.0.0.1:8765${path}${query}`;
  }
  if (window.location.port === "5173") {
    const host = window.location.hostname.includes(":")
      ? `[${window.location.hostname}]`
      : window.location.hostname;
    return `ws://${host}:8765${path}${query}`;
  }
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  return `${scheme}://${host}${path}${query}`;
}
