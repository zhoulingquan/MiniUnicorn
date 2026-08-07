import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const connectSpy = vi.fn();

vi.mock("@/lib/bootstrap", () => {
  // Real BootstrapError class so App.tsx's `instanceof` checks work.
  class BootstrapError extends Error {
    readonly status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "BootstrapError";
      this.status = status;
    }
    get isAuth() {
      return this.status === 401 || this.status === 403;
    }
    get isTransient() {
      return this.status === 0 || (this.status >= 500 && this.status < 600);
    }
  }
  // Trigger the auth flow on first bootstrap so App renders AuthForm.
  const fetchBootstrapWithRetry = vi.fn().mockRejectedValue(
    new BootstrapError("Unauthorized", 401),
  );
  return {
    BootstrapError,
    fetchBootstrap: vi.fn(),
    fetchBootstrapWithRetry,
    deriveWsUrl: vi.fn(() => "ws://test"),
    loadSavedSecret: vi.fn(() => ""),
    saveSecret: vi.fn(),
    clearSavedSecret: vi.fn(),
  };
});

vi.mock("@/lib/miniunicorn-client", () => {
  class MockClient {
    status = "idle" as const;
    defaultChatId: string | null = null;
    connect = connectSpy;
    onStatus = () => () => {};
    onRuntimeModelUpdate = () => () => {};
    onError = () => () => {};
    onChat = () => () => {};
    onSessionUpdate = () => () => {};
    onRunStatus = () => () => {};
    getRunStartedAt = () => null;
    getGoalState = () => undefined;
    sendMessage = vi.fn();
    newChat = vi.fn();
    attach = vi.fn();
    close = vi.fn();
    updateUrl = vi.fn();
  }
  return { MiniunicornClient: MockClient };
});

import App from "@/App";

describe("App AuthForm focus management", () => {
  beforeEach(() => {
    connectSpy.mockClear();
    vi.spyOn(HTMLElement.prototype, "focus").mockRestore();
  });

  afterEach(() => {
    vi.spyOn(HTMLElement.prototype, "focus").mockRestore();
  });

  it("moves focus into the secret input when the auth form mounts", async () => {
    const focusSpy = vi.spyOn(HTMLElement.prototype, "focus");
    render(<App />);

    // Wait for the bootstrap to reject and App to transition to auth state.
    const input = await screen.findByPlaceholderText("Password");
    expect(input).toBeInTheDocument();

    await waitFor(() => {
      const focusTargets = focusSpy.mock.instances as HTMLElement[];
      expect(focusTargets).toContain(input);
    });
  });
});
