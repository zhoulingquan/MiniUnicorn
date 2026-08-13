import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatSummary } from "@/lib/types";

const connectSpy = vi.fn();
const refreshSpy = vi.fn();
const createChatSpy = vi.fn().mockResolvedValue("chat-1");
const deleteChatSpy = vi.fn();
const toggleThemeSpy = vi.fn();
const updateUrlSpy = vi.fn();
const attachSpy = vi.fn();
const runStatusHandlers = new Set<(chatId: string, startedAt: number | null) => void>();
let mockSessions: ChatSummary[] = [];
const HERO_GREETING_PATTERN =
  /What should we work on\?|Where should we start\?|What are we building today\?|What should we tackle together\?/;

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

function baseSettingsPayload() {
  return {
    agent: {
      model: "deepseek/deepseek-chat",
      provider: "auto",
      resolved_provider: "deepseek",
      has_api_key: true,
      model_preset: "default",
      max_tokens: 8192,
      context_window_tokens: 65536,
      temperature: 0.1,
      reasoning_effort: null,
      tool_hint_max_length: 40,
      use_planner: false,
      planner_model: null,
      planner_max_replans: 3,
    },
    model_presets: [{
      name: "default",
      label: "Default",
      active: true,
      is_default: true,
      model: "deepseek/deepseek-chat",
      provider: "auto",
      max_tokens: 8192,
      context_window_tokens: 65536,
      temperature: 0.1,
      reasoning_effort: null,
    }],
    providers: [],
    web: {
      enable: true,
      proxy: null,
      user_agent: null,
      fetch: { use_jina_reader: true },
    },
    runtime: {
      config_path: "/tmp/config.json",
      workspace_path: "/tmp/workspace",
      gateway_host: "127.0.0.1",
      gateway_port: 8765,
      heartbeat: {
        enabled: true,
        interval_s: 3600,
        keep_recent_messages: 8,
      },
      dream: {
        schedule: "cron 0 3 * * *",
        max_batch_size: 20,
      },
      unified_session: false,
    },
    advanced: {
      restrict_to_workspace: false,
      webui_allow_local_service_access: true,
      webui_default_access_mode: "default",
      private_service_protection_enabled: true,
      ssrf_whitelist_count: 0,
      mcp_server_count: 0,
      exec_enabled: true,
      exec_sandbox: null,
      exec_path_append_set: false,
    },
    requires_restart: false,
  };
}

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const React = await import("react");
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => {
      const [sessions, setSessions] = React.useState(mockSessions);
      return {
        sessions,
        loading: false,
        error: null,
        refresh: refreshSpy,
        createChat: createChatSpy,
        deleteChat: async (key: string) => {
          await deleteChatSpy(key);
          setSessions((prev: ChatSummary[]) => prev.filter((s) => s.key !== key));
        },
      };
    },
  };
});

vi.mock("@/hooks/useTheme", async () => {
  const React = await import("react");
  return {
    ThemeProvider: ({ children }: { children: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useTheme: () => ({
      theme: "light" as const,
      toggle: toggleThemeSpy,
    }),
    useThemeValue: () => "light" as const,
  };
});

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
  const fetchBootstrap = vi.fn().mockResolvedValue({
    token: "tok",
    ws_path: "/",
    expires_in: 300,
  });
  // fetchBootstrapWithRetry delegates to fetchBootstrap so tests that
  // mock fetchBootstrap's return values keep working.
  const fetchBootstrapWithRetry = vi.fn(
    async (baseUrl?: string, secret?: string) => fetchBootstrap(baseUrl ?? "", secret ?? ""),
  );
  return {
    BootstrapError,
    fetchBootstrap,
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
    onRunStatus = (handler: (chatId: string, startedAt: number | null) => void) => {
      runStatusHandlers.add(handler);
      return () => runStatusHandlers.delete(handler);
    };
    getRunStartedAt = () => null;
    getGoalState = () => undefined;
    sendMessage = vi.fn();
    newChat = vi.fn();
    attach = attachSpy;
    close = vi.fn();
    updateUrl = updateUrlSpy;
  }

  return { MiniunicornClient: MockClient };
});

import { deriveWsUrl, fetchBootstrap, fetchBootstrapWithRetry } from "@/lib/bootstrap";
import App from "@/App";

describe("App layout", () => {
  beforeEach(() => {
    mockSessions = [];
    connectSpy.mockClear();
    updateUrlSpy.mockClear();
    refreshSpy.mockReset();
    createChatSpy.mockClear();
    deleteChatSpy.mockReset();
    toggleThemeSpy.mockReset();
    attachSpy.mockReset();
    runStatusHandlers.clear();
    localStorage.removeItem("miniunicorn-webui.sidebar.completed-runs.v1");
    vi.mocked(fetchBootstrap).mockReset().mockResolvedValue({
      token: "tok",
      ws_path: "/",
      expires_in: 300,
    });
    // fetchBootstrapWithRetry delegates to fetchBootstrap by default.
    vi.mocked(fetchBootstrapWithRetry).mockReset().mockImplementation(
      async (baseUrl?: string, secret?: string) => fetchBootstrap(baseUrl ?? "", secret ?? ""),
    );
    vi.mocked(deriveWsUrl).mockReset().mockReturnValue("ws://test");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
      }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps sidebar layout out of the main thread width contract", async () => {
    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    const main = container.querySelector("main");
    expect(main).toBeInTheDocument();
    expect(main).not.toHaveAttribute("style");

    const asideClassNames = Array.from(container.querySelectorAll("aside")).map(
      (el) => el.className,
    );
    expect(asideClassNames.some((cls) => cls.includes("lg:block"))).toBe(true);
  });

  it("switches to the next session when deleting the active chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^First chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(screen.getByLabelText("Chat actions for First chat"), {
      button: 0,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));

    await waitFor(() =>
      expect(screen.getByText("Delete this chat?")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(deleteChatSpy).toHaveBeenCalledWith("websocket:chat-a"),
    );
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Second chat$/ }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Delete this chat?")).not.toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
  }, 15_000);

  it("keeps the mobile session action menu inside the sidebar sheet", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: !query.includes("1024px"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    const sheet = await screen.findByRole("dialog");
    const mobileSidebar = within(sheet).getByRole("navigation", {
      name: "Sidebar navigation",
    });
    await waitFor(() =>
      expect(
        within(mobileSidebar).getByRole("button", { name: /^Existing chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(
      within(mobileSidebar).getByLabelText("Chat actions for Existing chat"),
      { button: 0 },
    );

    const deleteItem = await within(sheet).findByRole("menuitem", {
      name: "Delete",
    });
    expect(deleteItem).toBeInTheDocument();

    fireEvent.click(deleteItem);
    await waitFor(() =>
      expect(screen.getByText("Delete this chat?")).toBeInTheDocument(),
    );
  }, 15_000);

  it("applies persisted sidebar workspace state from the gateway", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    const initialState = {
      schema_version: 1,
      pinned_keys: ["websocket:chat-b"],
      archived_keys: ["websocket:chat-a"],
      title_overrides: { "websocket:chat-b": "Roadmap" },
      tags_by_key: {},
      collapsed_groups: {},
      view: {
        density: "comfortable",
        show_previews: false,
        show_timestamps: false,
        show_archived: false,
        sort: "updated_desc",
      },
      updated_at: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL | Request) => {
        const href = String(url);
        if (href === "/api/webui/sidebar-state") {
          return { ok: true, json: async () => initialState };
        }
        if (href.startsWith("/api/webui/sidebar-state/update?")) {
          const encoded = new URLSearchParams(href.split("?", 2)[1]).get("state");
          return {
            ok: true,
            json: async () => JSON.parse(encoded ?? "{}"),
          };
        }
        return { ok: false, status: 404 };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByText("Pinned")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByRole("button", { name: /^Roadmap$/ })).toBeInTheDocument();
    expect(within(sidebar).queryByRole("button", { name: /^First chat$/ })).not.toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole("button", { name: "Show archived" }));
    await waitFor(() =>
      expect(within(sidebar).getByText("Archived")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByRole("button", { name: /^First chat$/ })).toBeInTheDocument();
    const updateUrl = vi.mocked(fetch).mock.calls
      .map(([url]) => String(url))
      .find((url) => url.startsWith("/api/webui/sidebar-state/update?"));
    expect(updateUrl).toBeTruthy();
    const encoded = new URLSearchParams(updateUrl?.split("?", 2)[1]).get("state");
    expect(JSON.parse(encoded ?? "{}").view.show_archived).toBe(true);

    expect(within(sidebar).queryByRole("button", { name: "View" })).not.toBeInTheDocument();
  });

  it("sorts chats by displayed title when A-Z is persisted", async () => {
    mockSessions = [
      {
        key: "websocket:zulu",
        channel: "websocket",
        chatId: "zulu",
        createdAt: "2026-04-16T12:00:00Z",
        updatedAt: "2026-04-16T12:00:00Z",
        title: "Zulu work",
        preview: "later",
      },
      {
        key: "websocket:new",
        channel: "websocket",
        chatId: "new",
        createdAt: "2026-04-15T12:00:00Z",
        updatedAt: "2026-04-15T12:00:00Z",
        preview: "hi MiniUnicorn",
      },
      {
        key: "websocket:alpha",
        channel: "websocket",
        chatId: "alpha",
        createdAt: "2026-04-14T12:00:00Z",
        updatedAt: "2026-04-14T12:00:00Z",
        title: "Alpha plan",
        preview: "earlier",
      },
    ];
    const initialState = {
      schema_version: 1,
      pinned_keys: [],
      archived_keys: [],
      title_overrides: {},
      tags_by_key: {},
      collapsed_groups: {},
      view: {
        density: "comfortable",
        show_previews: false,
        show_timestamps: false,
        show_archived: false,
        sort: "title_asc",
      },
      updated_at: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL | Request) => {
        const href = String(url);
        if (href === "/api/webui/sidebar-state") {
          return { ok: true, json: async () => initialState };
        }
        return { ok: false, status: 404 };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByText("Chats")).toBeInTheDocument(),
    );
    const group = within(sidebar).getByText("Chats").closest("section");
    expect(group).toBeTruthy();
    const labels = within(group as HTMLElement)
      .getAllByRole("button")
      .map((button) => button.textContent?.trim())
      .filter(Boolean);

    expect(labels).toEqual(["Alpha plan", "hi MiniUnicorn", "Zulu work"]);
  });

  it("shows running and completed session indicators in the sidebar", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Working chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Quiet chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Working chat$/ }),
      ).toBeInTheDocument(),
    );

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", 12_345);
    });
    expect(within(sidebar).getByTitle("Agent running")).toBeInTheDocument();

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", null);
    });
    expect(within(sidebar).queryByTitle("Agent running")).not.toBeInTheDocument();
    expect(within(sidebar).getByTitle("Agent finished")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Working chat$/ }));
    });
    expect(within(sidebar).queryByTitle("Agent finished")).not.toBeInTheDocument();
  });

  it("restores sidebar run indicators after a page reload", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Running after reload",
        runStartedAt: 12_345,
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Completed after reload",
      },
    ];
    localStorage.setItem(
      "miniunicorn-webui.sidebar.completed-runs.v1",
      JSON.stringify(["chat-b"]),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByTitle("Agent running")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByTitle("Agent finished")).toBeInTheDocument();
    expect(attachSpy).toHaveBeenCalledWith("chat-a");
  });

  it("opens the settings view from the sidebar footer", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "deepseek/deepseek-chat",
                provider: "auto",
                resolved_provider: "deepseek",
                has_api_key: true,
                model_preset: "default",
                max_tokens: 8192,
                context_window_tokens: 65536,
                temperature: 0.1,
                reasoning_effort: null,
                tool_hint_max_length: 40,
                use_planner: false,
                planner_model: null,
                planner_max_replans: 3,
              },
              model_presets: [
                {
                  name: "default",
                  label: "Default",
                  active: true,
                  is_default: true,
                  model: "deepseek/deepseek-chat",
                  provider: "auto",
                  max_tokens: 8192,
                  context_window_tokens: 65536,
                  temperature: 0.1,
                  reasoning_effort: null,
                },
                {
                  name: "zen",
                  label: "zen",
                  active: false,
                  is_default: false,
                  model: "opencode/big-pickle",
                  provider: "opencode",
                  max_tokens: 8192,
                  context_window_tokens: 65536,
                  temperature: 0.1,
                  reasoning_effort: null,
                },
              ],
              providers: [
                {
                  name: "deepseek",
                  label: "DeepSeek",
                  configured: true,
                  api_key_hint: "sk-••••test",
                },
                {
                  name: "opencode",
                  label: "OpenCode Zen",
                  configured: false,
                  api_key_required: false,
                  default_api_base: "https://opencode.ai/zen/v1",
                },
              ],
              web: {
                enable: true,
                proxy: null,
                user_agent: null,
                fetch: { use_jina_reader: true },
              },
              runtime: {
                config_path: "/tmp/config.json",
                workspace_path: "/tmp/workspace",
                gateway_host: "127.0.0.1",
                gateway_port: 8765,
                heartbeat: {
                  enabled: true,
                  interval_s: 3600,
                  keep_recent_messages: 8,
                },
                dream: {
                  schedule: "cron 0 3 * * *",
                  max_batch_size: 20,
                },
                unified_session: false,
              },
              advanced: {
                restrict_to_workspace: false,
                webui_allow_local_service_access: true,
                webui_default_access_mode: "default",
                private_service_protection_enabled: true,
                ssrf_whitelist_count: 0,
                mcp_server_count: 0,
                exec_enabled: true,
                exec_sandbox: null,
                exec_path_append_set: false,
              },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const searchButton = screen.getByRole("button", { name: "Search chats" });
    const appsButton = within(sidebar).getByRole("button", { name: "CLI Apps" });
    expect(searchButton.compareDocumentPosition(appsButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(sidebar).getByRole("button", { name: "Settings" }));

    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(document.title).toBe("Settings · MiniUnicorn");
    expect(screen.queryByRole("navigation", { name: "Sidebar navigation" })).not.toBeInTheDocument();
    const settingsNav = screen.getByRole("navigation", { name: "Settings sections" });
    expect(settingsNav.className).toContain("overflow-x-auto");
    expect(settingsNav.className).not.toContain("grid-cols-2");
    expect(within(settingsNav).getByRole("button", { name: "Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(settingsNav).getByRole("button", { name: /Models/ })).toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "Providers" })).not.toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "Web Search" })).toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "Apps" })).not.toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "Security" })).toBeInTheDocument();
    fireEvent.click(within(settingsNav).getByRole("button", { name: "Appearance" }));
    fireEvent.click(within(settingsNav).getByRole("button", { name: "Models" }));
    expect(screen.queryByText("AI")).not.toBeInTheDocument();
    expect(screen.getByText("Current model")).toBeInTheDocument();
    expect(screen.queryByText("Presets")).not.toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: /deepseek\/deepseek-chat/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Add configuration" }));
    const modelDialog = screen.getByRole("dialog", { name: "New model configuration" });
    expect(within(modelDialog).getByText("Save a provider and model as a one-click option.")).toBeInTheDocument();
    fireEvent.change(within(modelDialog).getByPlaceholderText("Fast writing"), {
      target: { value: "Fast writing" },
    });
    fireEvent.change(within(modelDialog).getByPlaceholderText("openai/gpt-4.1"), {
      target: { value: "opencode/big-pickle" },
    });
    expect(within(modelDialog).getByRole("button", { name: "Save" })).toBeEnabled();
    fireEvent.click(within(modelDialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "New model configuration" })).not.toBeInTheDocument();

    // Switch to "zen" preset -> dirty
    fireEvent.pointerDown(screen.getByRole("button", { name: /deepseek\/deepseek-chat/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: /big-pickle/ }));
    expect(screen.getByText("Unsaved changes.").parentElement?.className).toContain(
      "text-blue-600",
    );

    // Switch back to "default" preset -> not dirty
    fireEvent.pointerDown(screen.getByRole("button", { name: /big-pickle/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Default/ }));
    expect(screen.queryByText("Unsaved changes.")).not.toBeInTheDocument();

    expect(screen.getByText("OpenCode Zen")).toBeInTheDocument();
    expect(screen.getByTestId("provider-logo-deepseek")).toBeInTheDocument();
    expect(screen.getAllByText("Not configured").length).toBeGreaterThan(0);

    const deepseekCard = screen.getByTestId("provider-logo-deepseek").closest("button")!;
    fireEvent.click(deepseekCard);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByPlaceholderText("Leave blank to keep the current key"), {
      target: { value: "unsaved-deepseek-key" },
    });

    // Collapse DeepSeek, expand OpenCode Zen (unconfigured)
    const opencodeCard = screen.getAllByText("OpenCode Zen")[0].closest("button")!;
    fireEvent.click(opencodeCard);
    fireEvent.click(deepseekCard);
    expect(screen.getByText("sk-••••test")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("unsaved-deepseek-key")).not.toBeInTheDocument();
    fireEvent.click(opencodeCard);
    expect(screen.getByDisplayValue("https://opencode.ai/zen/v1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save provider" })).toBeEnabled();

    fireEvent.click(within(settingsNav).getByRole("button", { name: /Models/ }));
    expect(screen.getByRole("heading", { name: /Models/ })).toBeInTheDocument();

    fireEvent.click(within(settingsNav).getByRole("button", { name: "Web Search" }));
    // Web 设置页包含 web_search 工具开关
    expect(screen.getByRole("switch", { name: /Enable web_search/i })).toBeInTheDocument();

    fireEvent.click(within(settingsNav).getByRole("button", { name: "Overview" }));
    expect(screen.queryByText("Bot name")).not.toBeInTheDocument();
    expect(screen.queryByText("Tool hint length")).not.toBeInTheDocument();
    expect(screen.queryByText("Unified session")).not.toBeInTheDocument();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Heartbeat")).toBeInTheDocument();
    expect(screen.getByText("Dream")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Search timezone")).not.toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "System" })).not.toBeInTheDocument();
  });

  it("opens Apps from the main sidebar without replacing the sidebar", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const href = String(input);
        if (href === "/api/settings") {
          return jsonResponse(baseSettingsPayload());
        }
        if (href === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0, catalog_updated_at: "2026-04-18" });
        }
        if (href === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    const appsButton = within(sidebar).getByRole("button", { name: "CLI Apps" });

    fireEvent.click(appsButton);

    expect(await screen.findByRole("heading", { name: "CLI Apps" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Sidebar navigation" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Settings sections" })).not.toBeInTheDocument();
    expect(document.title).toBe("MiniUnicorn");
  });

  it("returns from settings to the blank start page when no session was active", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "deepseek/deepseek-chat",
                provider: "deepseek",
                resolved_provider: "deepseek",
                has_api_key: true,
                model_preset: "default",
                max_tokens: 8192,
                context_window_tokens: 65536,
                temperature: 0.1,
                reasoning_effort: null,
                tool_hint_max_length: 40,
                use_planner: false,
                planner_model: null,
                planner_max_replans: 3,
              },
              model_presets: [
                {
                  name: "default",
                  label: "Default",
                  active: true,
                  is_default: true,
                  model: "deepseek/deepseek-chat",
                  provider: "deepseek",
                  max_tokens: 8192,
                  context_window_tokens: 65536,
                  temperature: 0.1,
                  reasoning_effort: null,
                },
              ],
              providers: [{ name: "deepseek", label: "DeepSeek", configured: true }],
              web: {
                enable: true,
                proxy: null,
                user_agent: null,
                fetch: { use_jina_reader: true },
              },
              runtime: {
                config_path: "/tmp/config.json",
                workspace_path: "/tmp/workspace",
                gateway_host: "127.0.0.1",
                gateway_port: 8765,
                heartbeat: {
                  enabled: true,
                  interval_s: 3600,
                  keep_recent_messages: 8,
                },
                dream: {
                  schedule: "cron 0 3 * * *",
                  max_batch_size: 20,
                },
                unified_session: false,
              },
              advanced: {
                restrict_to_workspace: false,
                webui_allow_local_service_access: true,
                webui_default_access_mode: "default",
                private_service_protection_enabled: true,
                ssrf_whitelist_count: 0,
                mcp_server_count: 0,
                exec_enabled: true,
                exec_sandbox: null,
                exec_path_append_set: false,
              },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(document.title).toBe("MiniUnicorn"));

    fireEvent.click(within(sidebar).getByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));

    await waitFor(() => expect(document.title).toBe("MiniUnicorn"));
    expect(screen.getByText(HERO_GREETING_PATTERN)).toBeInTheDocument();
  });

  it("filters sessions in the centered search dialog", async () => {
    mockSessions = [
      {
        key: "websocket:chat-alpha",
        channel: "websocket",
        chatId: "chat-alpha",
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        title: "Q2 roadmap",
        preview: "Project planning notes",
      },
      {
        key: "websocket:chat-beta",
        channel: "websocket",
        chatId: "chat-beta",
        createdAt: "2026-04-15T10:00:00Z",
        updatedAt: "2026-04-15T10:00:00Z",
        preview: "Travel ideas",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    expect(within(sidebar).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(sidebar).getByText("Travel ideas")).toBeInTheDocument();
    const newChatButton = within(sidebar).getByRole("button", { name: "New chat" });
    const searchButton = screen.getByRole("button", { name: "Search chats" });
    expect(
      newChatButton.compareDocumentPosition(searchButton) &
        Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();

    fireEvent.click(searchButton);
    const dialog = await screen.findByRole("dialog", { name: "Search chats" });
    expect(dialog).toHaveClass("origin-center");
    expect(dialog.className).not.toContain("translate-x");
    expect(dialog.className).not.toContain("translate-y");
    expect(dialog.querySelector("kbd")).toBeNull();
    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).getByText("Travel ideas")).toBeInTheDocument();
    expect(within(dialog).queryByText("websocket")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("#1")).not.toBeInTheDocument();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "Search chats" }), {
      target: { value: "planning" },
    });

    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).queryByText("Travel ideas")).not.toBeInTheDocument();
    expect(within(sidebar).getByText("Travel ideas")).toBeInTheDocument();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "Search chats" }), {
      target: { value: "road q2" },
    });

    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).queryByText("Travel ideas")).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: /Q2 roadmap/ }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Search chats" })).not.toBeInTheDocument(),
    );
  });

  it("opens search from the keyboard shortcut", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: "k", metaKey: true });

    const dialog = await screen.findByRole("dialog", { name: "Search chats" });
    expect(within(dialog).queryByText("Global actions")).not.toBeInTheDocument();
    expect(within(dialog).getByText("Existing chat")).toBeInTheDocument();

    const textbox = within(dialog).getByRole("textbox", { name: "Search chats" });
    fireEvent.change(textbox, { target: { value: "missing" } });
    expect(within(dialog).queryByText("Existing chat")).not.toBeInTheDocument();

    fireEvent.change(textbox, { target: { value: "existing" } });
    expect(within(dialog).getByText("Existing chat")).toBeInTheDocument();

    fireEvent.keyDown(textbox, { key: "Enter" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Search chats" })).not.toBeInTheDocument(),
    );
    expect(createChatSpy).not.toHaveBeenCalled();
  });

  it("keeps large sidebars light while search still covers every chat", async () => {
    mockSessions = Array.from({ length: 170 }, (_, index) => {
      const chatId = `chat-${index}`;
      return {
        key: `websocket:${chatId}`,
        channel: "websocket" as const,
        chatId,
        createdAt: new Date(Date.UTC(2026, 3, 16, 12, 0 - index)).toISOString(),
        updatedAt: new Date(Date.UTC(2026, 3, 16, 12, 0 - index)).toISOString(),
        title: index === 169 ? "Hidden target" : `Bulk chat ${index}`,
        preview: "",
      };
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    await waitFor(() =>
      expect(within(sidebar).getByRole("button", { name: "Bulk chat 0" })).toBeInTheDocument(),
    );
    expect(within(sidebar).queryByText("Hidden target")).not.toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Show 10 more" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Search chats" }));
    const dialog = await screen.findByRole("dialog", { name: "Search chats" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Search chats" }), {
      target: { value: "hidden" },
    });
    expect(within(dialog).getByText("Hidden target")).toBeInTheDocument();
  });

  it("opens a blank start page without creating an empty chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    const matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("1024px"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    vi.stubGlobal("matchMedia", matchMedia);

    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Toggle theme from header" }));
    expect(toggleThemeSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    const sidebarAside = container.querySelector("aside.lg\\:block") as HTMLElement;
    await waitFor(() => expect(sidebarAside.style.width).toBe("56px"));

    expect(screen.queryByRole("button", { name: "Start a new chat" })).not.toBeInTheDocument();
    const rail = screen.getByRole("navigation", { name: "Sidebar navigation" });
    expect(within(rail).getByRole("button", { name: "New chat" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search chats" })).toBeInTheDocument();
    expect(within(rail).queryByRole("button", { name: "View" })).not.toBeInTheDocument();
    expect(within(rail).queryByText("Existing chat")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    await waitFor(() => expect(sidebarAside.style.width).toBe("272px"));

    const sidebar = screen.getByRole("navigation", { name: "Sidebar navigation" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "New chat" }));
    expect(createChatSpy).not.toHaveBeenCalled();
    expect(screen.getByText(HERO_GREETING_PATTERN)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start a new chat" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Toggle theme from header" })).toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "Settings" })).toBeInTheDocument();

    expect(within(sidebar).getByText("Existing chat")).toBeInTheDocument();
  });

  it("refreshes the bootstrap token before REST settings auth expires", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchBootstrap)
      .mockResolvedValueOnce({
        token: "tok-1",
        ws_path: "/",
        expires_in: 30,
      })
      .mockResolvedValueOnce({
        token: "tok-2",
        ws_path: "/",
        expires_in: 300,
      });
    vi.mocked(deriveWsUrl).mockImplementation(
      (_wsPath: string, token: string) => `ws://test?token=${token}`,
    );

    const { unmount } = render(<App />);
    await act(async () => {});

    expect(connectSpy).toHaveBeenCalled();
    expect(fetchBootstrap).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    expect(fetchBootstrap).toHaveBeenCalledTimes(2);
    expect(updateUrlSpy).toHaveBeenCalledWith("ws://test?token=tok-2");
    unmount();
  });
});
