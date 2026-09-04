import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "@/components/settings/SettingsView";
import { ClientProvider } from "@/providers/ClientProvider";
import type { SettingsPayload } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

function settingsPayload(): SettingsPayload {
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
        model_preset: null,
        active_hours: null,
        light_context: false,
        isolated_session: false,
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

const installedBrowserbase = {
  name: "browserbase",
  display_name: "Browserbase",
  category: "browser",
  description: "Cloud browser automation for agents",
  requires: "BROWSERBASE_API_KEY",
  install_supported: true,
  installed: true,
  configured: true,
  available: true,
  status: "configured",
  logo_url: null,
  brand_color: "#6366F1",
};

function renderSettingsView(
  options: {
    initialSection?: "advanced" | "models" | "apps";
    onSettingsChange?: (payload: SettingsPayload) => void;
  } = {},
) {
  render(
    <ClientProvider client={{} as never} token="tok">
      <SettingsView
        themeMode="system"
        initialSection={options.initialSection ?? "advanced"}
        onSetThemeMode={() => {}}
        onBackToChat={() => {}}
        onModelNameChange={() => {}}
        onSettingsChange={options.onSettingsChange}
      />
    </ClientProvider>,
  );
}

describe("SettingsView Apps catalog", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a visible remove button for configured MCP presets and calls remove", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") {
        return jsonResponse(settingsPayload());
      }
      if (url === "/api/settings/mcp-presets") {
        return jsonResponse({
          presets: [installedBrowserbase],
          installed_count: 1,
        });
      }
      if (url === "/api/settings/mcp-presets/remove?name=browserbase") {
        return jsonResponse({
          presets: [{ ...installedBrowserbase, installed: false, configured: false, status: "available" }],
          installed_count: 0,
          last_action: {
            ok: true,
            message: "Removed MCP preset Browserbase.",
          },
        });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({ initialSection: "apps" });

    expect(await screen.findByRole("heading", { name: "Apps" })).toBeInTheDocument();
    expect(await screen.findByText("Browserbase")).toBeInTheDocument();
    const remove = screen.getByRole("button", { name: "Remove" });

    fireEvent.click(remove);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/mcp-presets/remove?name=browserbase",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok" },
        }),
      ),
    );
    expect(await screen.findByText("Removed MCP preset Browserbase.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByText("Removed MCP preset Browserbase.")).not.toBeInTheDocument();
  });

  it("publishes the latest settings payload to the shell", async () => {
    const payload = settingsPayload();
    const onSettingsChange = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0 });
        }
        if (url === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ onSettingsChange });

    await waitFor(() => expect(onSettingsChange).toHaveBeenCalledWith(payload));
  });

  it("saves network safety without exposing technical SSRF copy", async () => {
    const payload = settingsPayload();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") return jsonResponse(payload);
      if (url === "/api/settings/mcp-presets") {
        return jsonResponse({ presets: [], installed_count: 0 });
      }
      if (url === "/api/settings/network-safety/update?webui_allow_local_service_access=false&webui_default_access_mode=default") {
        return jsonResponse({
          ...payload,
          advanced: { ...payload.advanced, webui_allow_local_service_access: false },
          requires_restart: true,
          restart_required_sections: ["runtime"],
        });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({ initialSection: "advanced" });

    expect(await screen.findByText("Web safety")).toBeInTheDocument();
    expect(screen.queryByText(/SSRF/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Private Service Protection")).not.toBeInTheDocument();
    expect(screen.getByText("Default access")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restricted" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Default Permission" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Full Access" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: "Local services" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/network-safety/update?webui_allow_local_service_access=false&webui_default_access_mode=default",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok" },
        }),
      ),
    );
  });

  it("uses native host safety copy on the native surface", async () => {
    const payload = {
      ...settingsPayload(),
      surface: "native" as const,
      runtime_surface: "native" as const,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") return jsonResponse({ apps: [], installed_count: 0 });
        if (url === "/api/settings/mcp-presets") return jsonResponse({ presets: [], installed_count: 0 });
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "advanced" });

    expect(await screen.findByText("App safety")).toBeInTheDocument();
    expect(screen.queryByText("Web safety")).not.toBeInTheDocument();
    expect(screen.getByText("Allow Full Access shell commands to reach services on this Mac.")).toBeInTheDocument();
  });
});
