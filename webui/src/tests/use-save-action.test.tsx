// Tests for the shared `useSaveAction` primitive.
//
// Covers the contract from the remediation design:
//   - unchanged request payload;
//   - unchanged request count (single API call per save);
//   - repeated-click suppression (in-flight guard);
//   - success path (applyPayload + setError(null));
//   - failure path (setError(message));
//   - restart-required handling (setPendingRestartSections[key] = true);
//   - clearing of saving state after failure;
//   - external lock (mutual exclusion + release on success/failure);
//   - disabled guard (enabled=false → no-op);
//   - onApplied side effect.

import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SettingsPayload } from "@/lib/types";

import { useSaveAction } from "@/components/settings/hooks/useSaveAction";
import type { SaveActionExternalLock, SaveActionSharedDeps } from "@/components/settings/hooks/useSaveAction";
import { EMPTY_PENDING_RESTART_SECTIONS } from "@/components/settings/types";
import type { PendingRestartSections } from "@/components/settings/types";

function makePayload(overrides: Partial<SettingsPayload> = {}): SettingsPayload {
  return {
    agent: {
      model: "m1",
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
    model_presets: [],
    providers: [],
    web: { enable: true, proxy: null, user_agent: null, fetch: { use_jina_reader: true } },
    web_search: {
      enable: true,
      provider: "auto",
      max_results: 5,
      timeout: 30,
      backends: {},
    },
    image_generation: {
      enabled: false,
      preset: "default",
      default_aspect_ratio: "1:1",
      default_image_size: "1K",
      max_images_per_turn: 4,
      save_dir: "generated",
    },
    runtime: {
      heartbeat: { interval_s: 3600, model_preset: "" },
      dream: { schedule: "cron 0 3 * * *" },
    },
    advanced: {
      webui_allow_local_service_access: true,
      webui_default_access_mode: "default",
    },
    requires_restart: false,
    restart_required_sections: [],
    surface: "native",
    runtime_surface: "native",
    runtime_capabilities: { can_start_process: false, can_restart_engine: false },
    ...overrides,
  } as unknown as SettingsPayload;
}

function makeSharedDeps(): SaveActionSharedDeps & {
  pendingRestart: PendingRestartSections;
} {
  let pendingRestart: PendingRestartSections = { ...EMPTY_PENDING_RESTART_SECTIONS };
  return {
    applyPayload: vi.fn(),
    setError: vi.fn(),
    setPendingRestartSections: vi.fn((updater) => {
      pendingRestart = typeof updater === "function" ? updater(pendingRestart) : updater;
    }),
    maybeRestartHostEngine: vi.fn().mockResolvedValue(undefined),
    get pendingRestart() {
      return pendingRestart;
    },
  };
}

describe("useSaveAction", () => {
  it("calls the API with the payload from buildPayload (unchanged payload)", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload());
    const { result } = renderHook(() =>
      useSaveAction<void, { enable: boolean }>({
        shared,
        token: "tok",
        enabled: true,
        buildPayload: () => ({ enable: true }),
        apiCall,
        restartSectionKey: "browser",
      }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(apiCall).toHaveBeenCalledTimes(1);
    expect(apiCall).toHaveBeenCalledWith("tok", { enable: true });
  });

  it("makes exactly one API call per save invocation (unchanged request count)", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload());
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "browser" }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(apiCall).toHaveBeenCalledTimes(1);
  });

  it("suppresses repeated clicks while a save is in flight", async () => {
    const shared = makeSharedDeps();
    let resolveApi: (value: SettingsPayload) => void = () => {};
    const apiCall = vi.fn().mockReturnValue(
      new Promise<SettingsPayload>((resolve) => {
        resolveApi = resolve;
      }),
    );
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "browser" }),
    );

    // Kick off the first save — it hangs on the unresolved promise.
    let firstSave: Promise<void> | undefined;
    act(() => {
      firstSave = result.current.save();
    });

    // `saving` becomes true synchronously after the act() flush.
    await waitFor(() => expect(result.current.saving).toBe(true));

    // Rapid second and third clicks — must be no-ops.
    await act(async () => {
      await result.current.save();
      await result.current.save();
    });

    expect(apiCall).toHaveBeenCalledTimes(1);

    // Complete the first save.
    await act(async () => {
      resolveApi(makePayload());
      await firstSave;
    });

    expect(apiCall).toHaveBeenCalledTimes(1);
    expect(result.current.saving).toBe(false);
  });

  it("applies the response payload and clears error on success", async () => {
    const shared = makeSharedDeps();
    const payload = makePayload();
    const apiCall = vi.fn().mockResolvedValue(payload);
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "browser" }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(shared.applyPayload).toHaveBeenCalledWith(payload);
    expect(shared.setError).toHaveBeenCalledWith(null);
    expect(shared.maybeRestartHostEngine).toHaveBeenCalledWith(payload);
    expect(result.current.saving).toBe(false);
  });

  it("sets error message and clears saving state on failure", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "browser" }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(shared.setError).toHaveBeenCalledWith("boom");
    expect(shared.applyPayload).not.toHaveBeenCalled();
    expect(result.current.saving).toBe(false);
  });

  it("marks the restart-section key when response requires restart", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload({ requires_restart: true }));
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "images" }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(shared.setPendingRestartSections).toHaveBeenCalled();
    expect(shared.pendingRestart.images).toBe(true);
    expect(shared.pendingRestart.browser).toBe(false);
    expect(shared.pendingRestart.runtime).toBe(false);
  });

  it("does not mark restart when response does not require restart", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload({ requires_restart: false }));
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "runtime" }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(shared.setPendingRestartSections).not.toHaveBeenCalled();
  });

  it("clears saving state after failure (finally block)", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockRejectedValue(new Error("fail"));
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "browser" }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(result.current.saving).toBe(false);
  });

  it("does not call the API when enabled is false", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload());
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: false, buildPayload: () => null, apiCall, restartSectionKey: "browser" }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(apiCall).not.toHaveBeenCalled();
    expect(result.current.saving).toBe(false);
  });

  it("invokes onApplied after a successful apply", async () => {
    const shared = makeSharedDeps();
    const payload = makePayload();
    const apiCall = vi.fn().mockResolvedValue(payload);
    const onApplied = vi.fn();
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "browser", onApplied }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(onApplied).toHaveBeenCalledWith(payload);
  });

  it("does not invoke onApplied on failure", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockRejectedValue(new Error("nope"));
    const onApplied = vi.fn();
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "browser", onApplied }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(onApplied).not.toHaveBeenCalled();
  });

  it("acquires, checks, and releases the external lock", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload());
    const lock: SaveActionExternalLock = {
      isHeld: vi.fn().mockReturnValue(false),
      acquire: vi.fn(),
      release: vi.fn(),
    };
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "runtime", externalLock: lock }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(lock.isHeld).toHaveBeenCalled();
    expect(lock.acquire).toHaveBeenCalledTimes(1);
    expect(lock.release).toHaveBeenCalledTimes(1);
  });

  it("does not start when the external lock is already held", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload());
    const lock: SaveActionExternalLock = {
      isHeld: vi.fn().mockReturnValue(true),
      acquire: vi.fn(),
      release: vi.fn(),
    };
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "runtime", externalLock: lock }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(apiCall).not.toHaveBeenCalled();
    expect(lock.acquire).not.toHaveBeenCalled();
    expect(result.current.saving).toBe(false);
  });

  it("releases the external lock even on failure", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockRejectedValue(new Error("fail"));
    const lock: SaveActionExternalLock = {
      isHeld: vi.fn().mockReturnValue(false),
      acquire: vi.fn(),
      release: vi.fn(),
    };
    const { result } = renderHook(() =>
      useSaveAction({ shared, token: "tok", enabled: true, buildPayload: () => null, apiCall, restartSectionKey: "runtime", externalLock: lock }),
    );

    await act(async () => {
      await result.current.save();
    });

    expect(lock.release).toHaveBeenCalledTimes(1);
    expect(result.current.saving).toBe(false);
  });

  it("passes the argument to buildPayload", async () => {
    const shared = makeSharedDeps();
    const apiCall = vi.fn().mockResolvedValue(makePayload());
    const buildPayload = vi.fn((arg: { x: number }) => ({ value: arg.x }));
    const { result } = renderHook(() =>
      useSaveAction<{ x: number }, { value: number }>({
        shared,
        token: "tok",
        enabled: true,
        buildPayload,
        apiCall,
        restartSectionKey: "runtime",
      }),
    );

    await act(async () => {
      await result.current.save({ x: 42 });
    });

    expect(buildPayload).toHaveBeenCalledWith({ x: 42 });
    expect(apiCall).toHaveBeenCalledWith("tok", { value: 42 });
  });
});
