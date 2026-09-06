import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRestartFlow } from "@/hooks/useRestartFlow";
import { STORAGE_KEYS } from "@/lib/storage";
import type { ErzaClient } from "@/lib/erza-client";

const RESTART_KEY = STORAGE_KEYS.restartStartedAt;

/** Minimal fake client that exposes only the surface ``useRestartFlow`` needs. */
function makeFakeClient(): ErzaClient & {
  statusListeners: ((status: string) => void)[];
  emitStatus(status: string): void;
} {
  const statusListeners: ((status: string) => void)[] = [];
  const client = {
    statusListeners,
    defaultChatId: "chat-1",
    sendMessage: vi.fn(),
    onStatus(cb: (status: string) => void): () => void {
      statusListeners.push(cb);
      return () => {
        const idx = statusListeners.indexOf(cb);
        if (idx >= 0) statusListeners.splice(idx, 1);
      };
    },
    emitStatus(status: string): void {
      for (const cb of statusListeners) cb(status);
    },
  };
  return client as unknown as ErzaClient & {
    statusListeners: ((status: string) => void)[];
    emitStatus(status: string): void;
  };
}

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

afterEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("useRestartFlow — sessionStorage isolation (设计 §4.5)", () => {
  it("writes the restart timestamp to sessionStorage, not localStorage", () => {
    const client = makeFakeClient();
    const { result } = renderHook(() =>
      useRestartFlow({ client, activeChatId: "chat-1" }),
    );

    act(() => result.current.onRestart());

    expect(window.sessionStorage.getItem(RESTART_KEY)).toMatch(/^\d+$/);
    // localStorage must NOT carry the key — that would leak the in-progress
    // state to other tabs and let them clear it (the bug we're fixing).
    expect(window.localStorage.getItem(RESTART_KEY)).toBeNull();
    expect(client.sendMessage).toHaveBeenCalledWith("chat-1", "/restart");
  });

  it("does not surface 'restart completed' when another tab triggered the restart", async () => {
    // Simulate the original bug: another tab wrote localStorage. Our hook
    // reads sessionStorage only, so the foreign timestamp must be ignored.
    window.localStorage.setItem(RESTART_KEY, String(Date.now() - 5_000));
    // sessionStorage is empty for this tab.

    const client = makeFakeClient();
    const { result } = renderHook(() =>
      useRestartFlow({ client, activeChatId: "chat-1" }),
    );

    // Connection drops and reopens. Old code would read the foreign
    // localStorage timestamp, treat this as our own restart, and show toast.
    act(() => client.emitStatus("closed"));
    act(() => client.emitStatus("open"));

    // No toast — we never initiated a restart in this tab.
    expect(result.current.restartToast).toBeNull();
    expect(result.current.isRestarting).toBe(false);
    // The foreign localStorage key must NOT be cleared by this tab.
    expect(window.localStorage.getItem(RESTART_KEY)).toMatch(/^\d+$/);
  });

  it("completes the restart cycle only in the initiating tab", async () => {
    const client = makeFakeClient();
    const { result } = renderHook(() =>
      useRestartFlow({ client, activeChatId: "chat-1" }),
    );

    act(() => result.current.onRestart());
    expect(result.current.isRestarting).toBe(true);

    // Connection drops then reconnects after > 1.5s.
    act(() => client.emitStatus("closed"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
    act(() => client.emitStatus("open"));

    await waitFor(() => expect(result.current.isRestarting).toBe(false));
    expect(result.current.restartToast).toBeTruthy();
    // sessionStorage key is cleared after completion.
    expect(window.sessionStorage.getItem(RESTART_KEY)).toBeNull();
  });
});
