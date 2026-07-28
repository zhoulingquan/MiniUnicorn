// Typed save-action primitive shared by every settings section.
//
// One instance per save button. Owns:
//   - a ref-based in-flight guard (stronger than a state boolean — survives
//     stale closures during rapid double-clicks);
//   - the local `saving` flag for UI display;
//   - error normalization (string message → shared setError);
//   - response payload application (applyPayload);
//   - per-section restart marking (setPendingRestartSections[key] = true);
//   - optional host-engine restart dialog (maybeRestartHostEngine);
//   - optional shared mutex (externalLock) for mutually-exclusive actions
//     such as model-preset activate/delete sharing `providerSaving`;
//   - guaranteed cleanup in `finally`.
//
// Each previously independent action receives its own instance so unrelated
// buttons never block one another. The hook is generic over the argument
// type `TArg` (defaults to `void` for no-arg saves) and the payload type
// `TPayload`.

import { useCallback, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import type { SettingsPayload } from "@/lib/types";

import type { PendingRestartSections, RestartAwarePayload } from "../types";

export type PendingRestartSectionKey = "runtime" | "browser" | "images";

/** Shared dependencies injected from the settings orchestrator. */
export interface SaveActionSharedDeps {
  applyPayload: (payload: SettingsPayload) => void;
  setError: (msg: string | null) => void;
  setPendingRestartSections: Dispatch<SetStateAction<PendingRestartSections>>;
  maybeRestartHostEngine: (payload: RestartAwarePayload) => Promise<void>;
}

/** Optional shared mutex for actions that must be mutually exclusive. */
export interface SaveActionExternalLock {
  /** Returns true if any action currently holds the lock. */
  isHeld: () => boolean;
  /** Claim the lock for this action. */
  acquire: () => void;
  /** Release the lock. */
  release: () => void;
}

export interface UseSaveActionOptions<TArg, TPayload> {
  /** Shared orchestrator deps. */
  shared: SaveActionSharedDeps;
  /** Auth token for the API call. */
  token: string;
  /**
   * Whether the save may start (settings exists, form is dirty, etc.).
   * Checked before acquiring any lock. The in-flight guard is always
   * applied regardless of this value.
   */
  enabled: boolean;
  /** Build the API request payload from the argument. */
  buildPayload: (arg: TArg) => TPayload;
  /** Perform the API call. */
  apiCall: (token: string, payload: TPayload) => Promise<SettingsPayload>;
  /** Which restart-section flag to set when the response requires restart. */
  restartSectionKey: PendingRestartSectionKey;
  /** Optional side effect after a successful apply (e.g., onModelNameChange). */
  onApplied?: (payload: SettingsPayload) => void;
  /** Optional shared mutex for mutually-exclusive actions. */
  externalLock?: SaveActionExternalLock;
}

export interface SaveActionInstance<TArg> {
  /** True while this action's save is in flight. */
  saving: boolean;
  /** Trigger the save. No-op if already in flight, disabled, or lock is held. */
  save: (arg: TArg) => Promise<void>;
}

/**
 * One small typed save-action primitive.
 *
 * The `save` callback has stable identity (never changes across renders) and
 * always reads the latest options via a ref, so callers can pass it to
 * `useEffect` dependency arrays without causing re-subscriptions.
 */
export function useSaveAction<TArg = void, TPayload = unknown>(
  options: UseSaveActionOptions<TArg, TPayload>,
): SaveActionInstance<TArg> {
  const [saving, setSaving] = useState(false);
  const inFlightRef = useRef(false);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const save = useCallback(async (arg: TArg) => {
    const opts = optionsRef.current;
    if (inFlightRef.current) return;
    if (!opts.enabled) return;
    if (opts.externalLock && opts.externalLock.isHeld()) return;
    inFlightRef.current = true;
    setSaving(true);
    if (opts.externalLock) opts.externalLock.acquire();
    try {
      const payload = opts.buildPayload(arg);
      const result = await opts.apiCall(opts.token, payload);
      opts.shared.applyPayload(result);
      if (result.requires_restart) {
        const key = opts.restartSectionKey;
        opts.shared.setPendingRestartSections((prev) => ({ ...prev, [key]: true }));
      }
      await opts.shared.maybeRestartHostEngine(result);
      opts.onApplied?.(result);
      opts.shared.setError(null);
    } catch (err) {
      opts.shared.setError((err as Error).message);
    } finally {
      if (opts.externalLock) opts.externalLock.release();
      setSaving(false);
      inFlightRef.current = false;
    }
    // `save` intentionally has stable identity; latest options are read via ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { saving, save };
}
