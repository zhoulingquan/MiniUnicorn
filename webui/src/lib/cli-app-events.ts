import type { CliAppsPayload } from "@/lib/types";

export const CLI_APPS_CHANGED_EVENT = "miniunicorn:cli-apps-changed";

export function notifyCliAppsChanged(payload: CliAppsPayload): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent<CliAppsPayload>(CLI_APPS_CHANGED_EVENT, {
    detail: payload,
  }));
}
