import type { UIFileEdit, UIMessage } from "@/lib/types";

import { traceLines } from "@/components/thread/activity/types";
import type { FileEditSummary } from "@/components/thread/activity/types";

export function isFileEditTraceLine(line: string): boolean {
  return /^(write_file|edit_file|apply_patch)\(/.test(line.trim());
}

export function shortFileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

export function messageHasOnlyFileActivity(message: UIMessage): boolean {
  if (message.kind !== "trace" || !message.fileEdits?.length) return false;
  return traceLines(message).every((line) => !line.trim() || isFileEditTraceLine(line));
}

export function fileActivityVerb(editing: boolean, failed: boolean, deleted: boolean): string {
  if (failed) return "Failed";
  if (deleted) return editing ? "Deleting" : "Deleted";
  return editing ? "Editing" : "Edited";
}

export function fileActivitySummaryKey(editing: boolean, failed: boolean, deleted: boolean): string {
  if (failed) return "message.fileActivityFailedOne";
  if (deleted) return editing ? "message.fileActivityDeletingOne" : "message.fileActivityDeletedOne";
  return editing ? "message.fileActivityEditingOne" : "message.fileActivityEditedOne";
}

export function fileActivityManySummaryKey(editing: boolean, failed: boolean, deleted: boolean): string {
  if (failed) return "message.fileActivityFailedMany";
  if (deleted) return editing ? "message.fileActivityDeletingMany" : "message.fileActivityDeletedMany";
  return editing ? "message.fileActivityEditingMany" : "message.fileActivityEditedMany";
}

function fileEditCallKey(edit: UIFileEdit): string {
  if (edit.call_id) return `${edit.call_id}|${edit.tool}`;
  return `${edit.tool}|${edit.path}`;
}

export function collectFileEdits(messages: UIMessage[]): UIFileEdit[] {
  const edits: UIFileEdit[] = [];
  for (const message of messages) {
    if (message.kind === "trace" && message.fileEdits?.length) {
      edits.push(...message.fileEdits);
    }
  }
  return edits;
}

function latestFileEditEvents(edits: UIFileEdit[]): UIFileEdit[] {
  const order: string[] = [];
  const byKey = new Map<string, UIFileEdit>();
  for (const edit of edits) {
    const key = fileEditCallKey(edit);
    if (!byKey.has(key)) order.push(key);
    byKey.set(key, edit);
  }
  return order.map((key) => byKey.get(key)).filter(Boolean) as UIFileEdit[];
}

export function summarizeFileEdits(edits: UIFileEdit[], active: boolean): FileEditSummary[] {
  interface MutableSummary {
    key: string;
    path: string;
    absolute_path?: string | null;
    added: number;
    deleted: number;
    approximate: boolean;
    binary: boolean;
    pending: boolean;
    hasSuccessfulChange: boolean;
    hasActiveEditing: boolean;
    hasFailed: boolean;
    operation?: UIFileEdit["operation"];
    error?: string;
  }

  const order: string[] = [];
  const byPath = new Map<string, MutableSummary>();
  for (const edit of latestFileEditEvents(edits)) {
    const key = edit.path || edit.call_id || edit.tool;
    const existing = byPath.get(key);
    const summary: MutableSummary = existing ?? {
      key,
      path: edit.path || "",
      absolute_path: edit.absolute_path,
      added: 0,
      deleted: 0,
      approximate: false,
      binary: false,
      pending: false,
      hasSuccessfulChange: false,
      hasActiveEditing: false,
      hasFailed: false,
      operation: undefined,
    };
    if (!existing) {
      byPath.set(key, summary);
      order.push(key);
    }

    if (edit.path && !summary.path) {
      summary.path = edit.path;
    }
    if (edit.absolute_path) {
      summary.absolute_path = edit.absolute_path;
    }
    if (edit.operation === "delete") {
      summary.operation = "delete";
    }
    summary.pending = summary.pending || !!edit.pending || !edit.path;
    if (!edit.path && edit.pending) {
      if (active && edit.status === "editing") {
        summary.hasActiveEditing = true;
        summary.approximate = summary.approximate || !!edit.approximate;
        if (!edit.binary) {
          summary.added += edit.added ?? 0;
          summary.deleted += edit.deleted ?? 0;
        }
      }
      continue;
    }
    if (active && edit.status === "editing") {
      summary.hasActiveEditing = true;
      summary.binary = summary.binary || !!edit.binary;
      summary.approximate = summary.approximate || !!edit.approximate;
      if (!edit.binary) {
        summary.added += edit.added ?? 0;
        summary.deleted += edit.deleted ?? 0;
      }
      continue;
    }

    if (edit.status === "error") {
      summary.hasFailed = true;
      summary.error = edit.error ?? summary.error;
      continue;
    }

    summary.hasSuccessfulChange = true;
    summary.binary = summary.binary || !!edit.binary;
    summary.approximate = active && (summary.approximate || !!edit.approximate);
    if (!edit.binary) {
      summary.added += edit.added ?? 0;
      summary.deleted += edit.deleted ?? 0;
    }
  }

  return order.flatMap((key) => {
    const summary = byPath.get(key)!;
    if (
      !summary.path
      && !summary.hasActiveEditing
      && !summary.hasSuccessfulChange
      && !summary.hasFailed
    ) {
      return [];
    }
    const status: UIFileEdit["status"] = summary.hasActiveEditing
      ? "editing"
      : summary.hasSuccessfulChange
        ? "done"
        : summary.hasFailed
          ? "error"
          : "done";
    return [{
      key: summary.key,
      path: summary.path,
      absolute_path: summary.absolute_path,
      added: summary.added,
      deleted: summary.deleted,
      approximate: summary.approximate,
      binary: summary.binary,
      status,
      operation: summary.operation,
      pending: summary.pending && !summary.path,
      error: summary.error,
    }];
  });
}

export function hasVisibleDiffStats(edit: Pick<FileEditSummary, "added" | "deleted">): boolean {
  return edit.added > 0 || edit.deleted > 0;
}

export function formatFileEditError(error?: string): string {
  const firstLine = (error || "").replace(/\s+/g, " ").trim();
  if (!firstLine) return "";
  const cleaned = firstLine
    .replace(/^Error applying patch:\s*/i, "")
    .replace(/^Error writing file:\s*/i, "")
    .replace(/^Error editing file:\s*/i, "")
    .replace(/^Error:\s*/i, "");

  return cleaned
    .replace(/^old_text not found in (.+)$/i, "Target text was not found in $1.")
    .replace(/^old_text appears multiple times in (.+)$/i, "Target text matched multiple places in $1.")
    .replace(/^file to (?:update|delete) does not exist: (.+)$/i, "File does not exist: $1.")
    .replace(/^path to (?:update|delete) is not a file: (.+)$/i, "Path is not a file: $1.")
    .slice(0, 180);
}
