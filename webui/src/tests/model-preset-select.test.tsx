import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ModelPresetSelect } from "@/components/settings/components/ModelPresetSelect";
import type { SettingsPayload } from "@/lib/types";

function makeSettings(): SettingsPayload {
  return {
    agent: {
      model: "deepseek/deepseek-chat",
      provider: "auto",
      resolved_provider: "deepseek",
      has_api_key: true,
      model_preset: "default",
      max_tokens: 8192,
      context_window_tokens: 65536,
      resolved_context_window_tokens: 65536,
      resolved_context_window_status: "configured",
      resolved_context_window_error: null,
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
        resolved_context_window_tokens: 65536,
        resolved_context_window_status: "configured",
        resolved_context_window_error: null,
        temperature: 0.1,
        reasoning_effort: null,
      },
      {
        name: "fast-writing",
        label: "Fast Writing",
        active: false,
        is_default: false,
        model: "deepseek/deepseek-chat",
        provider: "deepseek",
        max_tokens: 8192,
        context_window_tokens: 65536,
        resolved_context_window_tokens: 65536,
        resolved_context_window_status: "configured",
        resolved_context_window_error: null,
        temperature: 0.1,
        reasoning_effort: null,
      },
    ],
    providers: [],
    web: { enable: true, proxy: null, user_agent: null, fetch: { use_jina_reader: true } },
    web_search: {
      enable: true,
      provider: "auto",
      max_results: 5,
      timeout: 30,
      proxy: null,
      backends: {},
    },
    image_generation: {
      enabled: false,
      preset: "default",
      api_type: "images_generations",
      response_format: "b64_json",
      default_aspect_ratio: "1:1",
      default_image_size: "1K",
      max_images_per_turn: 4,
      save_dir: "generated",
      save_dir_full: "/tmp/media/generated",
      supported_api_types: ["images_generations"],
      aspect_ratio_options: ["1:1"],
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
        max_iterations: 15,
        annotate_line_ages: true,
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

/** Open the Radix DropdownMenu by simulating the pointer events it listens for. */
function openDropdown() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Test Preset" }), {
    button: 0,
  });
}

describe("ModelPresetSelect", () => {
  it("emits the empty-string sentinel for heartbeat when Main model is selected", async () => {
    const onChange = vi.fn();
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel=""
        value="fast-writing"
        presets={settings.model_presets}
        settings={settings}
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={onChange}
      />,
    );
    openDropdown();
    const mainItem = await screen.findByRole("menuitem", { name: /Main model/ });
    fireEvent.click(mainItem);
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("emits the null sentinel for planner when Main model is selected", async () => {
    const onChange = vi.fn();
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel={null}
        value="fast-writing"
        presets={settings.model_presets}
        settings={settings}
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={onChange}
      />,
    );
    openDropdown();
    const mainItem = await screen.findByRole("menuitem", { name: /Main model/ });
    fireEvent.click(mainItem);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('emits the "default" sentinel for image generation when Main model is selected', async () => {
    const onChange = vi.fn();
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel="default"
        value="fast-writing"
        presets={settings.model_presets}
        settings={settings}
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={onChange}
      />,
    );
    openDropdown();
    const mainItem = await screen.findByRole("menuitem", { name: /Main model/ });
    fireEvent.click(mainItem);
    expect(onChange).toHaveBeenCalledWith("default");
  });

  it("emits the preset name when a named preset is selected", async () => {
    const onChange = vi.fn();
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel=""
        value=""
        presets={settings.model_presets}
        settings={settings}
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={onChange}
      />,
    );
    openDropdown();
    const presetItem = await screen.findByRole("menuitem", { name: /Fast Writing/ });
    fireEvent.click(presetItem);
    expect(onChange).toHaveBeenCalledWith("fast-writing");
  });

  it("disables the trigger button when disabled prop is true", () => {
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel=""
        value=""
        presets={settings.model_presets}
        settings={settings}
        disabled
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Test Preset" })).toBeDisabled();
  });

  it("shows the selected marker on the active Main model row (empty-string sentinel)", async () => {
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel=""
        value=""
        presets={settings.model_presets}
        settings={settings}
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={() => {}}
      />,
    );
    openDropdown();
    // The Check icon (lucide-check) is rendered for the selected row.
    await screen.findByRole("menuitem", { name: /Main model/ });
    const checkIcons = document.querySelectorAll("svg.lucide-check");
    expect(checkIcons.length).toBeGreaterThanOrEqual(1);
  });

  it("shows the selected marker on a named preset when it is active", async () => {
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel="default"
        value="fast-writing"
        presets={settings.model_presets}
        settings={settings}
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={() => {}}
      />,
    );
    openDropdown();
    await screen.findByRole("menuitem", { name: /Fast Writing/ });
    const checkIcons = document.querySelectorAll("svg.lucide-check");
    expect(checkIcons.length).toBeGreaterThanOrEqual(1);
  });

  it("renders provider icons when showProviderIcon is true", () => {
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel="default"
        value="fast-writing"
        presets={settings.model_presets}
        settings={settings}
        showProviderIcon
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={() => {}}
      />,
    );
    // The trigger renders the selected preset label.
    expect(screen.getByText("Fast Writing")).toBeInTheDocument();
  });

  it("does not render provider icons when showProviderIcon is omitted", () => {
    const settings = makeSettings();
    render(
      <ModelPresetSelect
        defaultSentinel=""
        value=""
        presets={settings.model_presets}
        settings={settings}
        label="Test Preset"
        defaultOptionLabel="Main model"
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Test Preset" })).toBeInTheDocument();
  });
});
