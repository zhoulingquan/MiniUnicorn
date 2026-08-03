import { describe, expect, it } from "vitest";

import { describeTraceLine } from "@/components/thread/activity/trace-format";
import { collectCliRuns } from "@/components/thread/activity/cli-runs";
import { collectMcpRuns } from "@/components/thread/activity/mcp-runs";
import { summarizeFileEdits, hasVisibleDiffStats } from "@/components/thread/activity/file-edits";
import type { UIFileEdit, UIMessage } from "@/lib/types";

describe("describeTraceLine", () => {
  it("exposes url and host for public web fetch traces", () => {
    const trace = describeTraceLine(
      'web_fetch({"url":"https://auth0.com/blog/jwt-security-best-practices"})',
    );
    expect(trace.kind).toBe("tool");
    expect(trace.label).toBe("Reading");
    expect(trace.url).toBe("https://auth0.com/blog/jwt-security-best-practices");
    expect(trace.host).toBe("auth0.com");
    expect(trace.detail).toBe("auth0.com/blog/jwt-security-best-practices");
  });

  it("suppresses url and host for private hostname traces", () => {
    const trace = describeTraceLine(
      'web_fetch({"url":"http://localhost:3000/dashboard"})',
    );
    expect(trace.url).toBeUndefined();
    expect(trace.host).toBeUndefined();
    // Detail falls back to the raw arg preview (no public URL extracted).
    expect(trace.detail).toContain("localhost:3000");
  });

  it("redacts secrets inside shell command traces", () => {
    const trace = describeTraceLine(
      'exec({"command":"SECRET_TOKEN=sk-test echo hello"})',
    );
    expect(trace.kind).toBe("tool");
    expect(trace.label).toBe("Shell");
    expect(trace.detail).toContain("SECRET_TOKEN=••••");
    expect(trace.detail).not.toContain("sk-test");
  });

  it("redacts bearer tokens in shell command traces", () => {
    const trace = describeTraceLine(
      'shell({"command":"curl -H \\"Authorization: Bearer abc.def-ghi_jkl\\" https://example.com"})',
    );
    expect(trace.detail).toContain("Bearer ••••");
    expect(trace.detail).not.toContain("abc.def-ghi_jkl");
  });

  it("labels search traces as searching", () => {
    const trace = describeTraceLine('search({"query":"rust async"})');
    expect(trace.kind).toBe("search");
    expect(trace.label).toBe("Searching");
  });

  it("labels plain done traces", () => {
    const trace = describeTraceLine("done");
    expect(trace.kind).toBe("done");
    expect(trace.label).toBe("Done");
  });
});

describe("collectCliRuns", () => {
  it("keeps CLI runs in chronological order", () => {
    const messages: UIMessage[] = [
      {
        id: "t-shell",
        role: "tool",
        kind: "trace",
        content: 'exec({"cmd":"ls -la"})',
        traces: ['exec({"cmd":"ls -la"})'],
        createdAt: 1,
      },
      {
        id: "t-cli",
        role: "tool",
        kind: "trace",
        content: 'run_cli_app({"name":"blender","args":["project"],"json":true})',
        traces: ['run_cli_app({"name":"blender","args":["project"],"json":true})'],
        createdAt: 2,
      },
    ];
    const runs = collectCliRuns(messages);
    expect(runs).toHaveLength(1);
    expect(runs[0].name).toBe("blender");
    expect(runs[0].args).toEqual(["project"]);
    expect(runs[0].json).toBe(true);
  });

  it("merges CLI run phases by call_id with success winning over running", () => {
    const messages: UIMessage[] = [
      {
        id: "t-cli-start",
        role: "tool",
        kind: "trace",
        content: 'run_cli_app({"name":"blender"})',
        traces: [],
        toolEvents: [
          {
            phase: "start",
            call_id: "call-blender",
            name: "run_cli_app",
            arguments: { name: "blender", args: ["render"], json: false },
          },
        ],
        createdAt: 1,
      },
      {
        id: "t-cli-end",
        role: "tool",
        kind: "trace",
        content: "",
        traces: [],
        toolEvents: [
          {
            phase: "end",
            call_id: "call-blender",
            name: "run_cli_app",
            arguments: { name: "blender", args: ["render"], json: false },
          },
        ],
        createdAt: 2,
      },
    ];
    const runs = collectCliRuns(messages);
    expect(runs).toHaveLength(1);
    expect(runs[0].status).toBe("done");
    expect(runs[0].name).toBe("blender");
  });

  it("marks rejected CLI calls as error", () => {
    const messages: UIMessage[] = [
      {
        id: "t-cli-fail",
        role: "tool",
        kind: "trace",
        content: "",
        traces: [],
        toolEvents: [
          {
            phase: "error",
            call_id: "call-github",
            name: "run_cli_app",
            arguments: { name: "github", args: ["repo", "view"], json: "true" },
            error: "Error: CLI app 'github' not found",
          },
        ],
        createdAt: 1,
      },
    ];
    const runs = collectCliRuns(messages);
    expect(runs).toHaveLength(1);
    expect(runs[0].status).toBe("error");
    expect(runs[0].error).toBe("Error: CLI app 'github' not found");
  });
});

describe("collectMcpRuns", () => {
  it("parses MCP preset tool calls from trace lines", () => {
    const line = 'mcp_browserbase_browser_navigate({"url":"https://example.com"})';
    const messages: UIMessage[] = [
      {
        id: "t-mcp",
        role: "tool",
        kind: "trace",
        content: line,
        traces: [line],
        createdAt: 1,
      },
    ];
    const runs = collectMcpRuns(messages);
    expect(runs).toHaveLength(1);
    expect(runs[0].presetName).toBe("browserbase");
    expect(runs[0].displayName).toBe("Browserbase");
    expect(runs[0].toolName).toBe("browser_navigate");
    expect(runs[0].argsPreview).toContain("url: https://example.com");
    expect(runs[0].status).toBe("running");
  });

  it("merges MCP run phases by call_id", () => {
    const messages: UIMessage[] = [
      {
        id: "t-mcp-start",
        role: "tool",
        kind: "trace",
        content: "",
        traces: [],
        toolEvents: [
          {
            phase: "start",
            call_id: "call-browserbase",
            name: "mcp_browserbase_browser_navigate",
            arguments: { url: "https://example.com" },
          },
        ],
        createdAt: 1,
      },
      {
        id: "t-mcp-end",
        role: "tool",
        kind: "trace",
        content: "",
        traces: [],
        toolEvents: [
          {
            phase: "end",
            call_id: "call-browserbase",
            name: "mcp_browserbase_browser_navigate",
            arguments: { url: "https://example.com" },
          },
        ],
        createdAt: 2,
      },
    ];
    const runs = collectMcpRuns(messages);
    expect(runs).toHaveLength(1);
    expect(runs[0].status).toBe("done");
  });
});

describe("summarizeFileEdits", () => {
  it("lets successful edits win over failures for the same path", () => {
    const edits: UIFileEdit[] = [
      {
        call_id: "call-edit-1",
        tool: "edit_file",
        path: "minecraft-fps/index.html",
        phase: "end",
        added: 2,
        deleted: 1,
        approximate: false,
        status: "done",
      },
      {
        call_id: "call-edit-2",
        tool: "edit_file",
        path: "minecraft-fps/index.html",
        phase: "error",
        added: 0,
        deleted: 0,
        approximate: false,
        status: "error",
        error: "patch failed",
      },
      {
        call_id: "call-edit-3",
        tool: "edit_file",
        path: "minecraft-fps/index.html",
        phase: "end",
        added: 6,
        deleted: 6,
        approximate: false,
        status: "done",
      },
    ];
    const summary = summarizeFileEdits(edits, false);
    expect(summary).toHaveLength(1);
    // Success wins: status is "done" so the failure is not surfaced visually.
    expect(summary[0].status).toBe("done");
    expect(summary[0].added).toBe(8);
    expect(summary[0].deleted).toBe(7);
  });

  it("labels whole-file deletes with the delete operation", () => {
    const edits: UIFileEdit[] = [
      {
        call_id: "call-delete",
        tool: "apply_patch",
        path: "angry-birds.html",
        phase: "end",
        added: 0,
        deleted: 590,
        approximate: false,
        status: "done",
        operation: "delete",
      },
    ];
    const summary = summarizeFileEdits(edits, false);
    expect(summary).toHaveLength(1);
    expect(summary[0].operation).toBe("delete");
    expect(summary[0].added).toBe(0);
    expect(summary[0].deleted).toBe(590);
  });

  it("drops pathless pending edits after the turn completes", () => {
    const edits: UIFileEdit[] = [
      {
        call_id: "call-pending",
        tool: "edit_file",
        path: "",
        phase: "start",
        added: 98,
        deleted: 0,
        approximate: true,
        status: "editing",
        pending: true,
      },
    ];
    const summary = summarizeFileEdits(edits, false);
    expect(summary).toHaveLength(0);
  });

  it("keeps pathless pending edits while the turn is still active", () => {
    const edits: UIFileEdit[] = [
      {
        call_id: "call-pending",
        tool: "edit_file",
        path: "",
        phase: "start",
        added: 0,
        deleted: 0,
        approximate: true,
        status: "editing",
        pending: true,
      },
    ];
    const summary = summarizeFileEdits(edits, true);
    expect(summary).toHaveLength(1);
    expect(summary[0].pending).toBe(true);
  });

  it("suppresses zero diff totals for completed edits via hasVisibleDiffStats", () => {
    const edits: UIFileEdit[] = [
      {
        call_id: "call-zero",
        tool: "edit_file",
        path: "src/app.tsx",
        phase: "end",
        added: 0,
        deleted: 0,
        approximate: false,
        status: "done",
      },
    ];
    const summary = summarizeFileEdits(edits, false);
    expect(summary).toHaveLength(1);
    expect(summary[0].added).toBe(0);
    expect(summary[0].deleted).toBe(0);
    expect(hasVisibleDiffStats(summary[0])).toBe(false);
  });

  it("reports visible diff totals for completed edits with changes", () => {
    const edits: UIFileEdit[] = [
      {
        call_id: "call-edit",
        tool: "edit_file",
        path: "src/app.tsx",
        phase: "end",
        added: 12,
        deleted: 3,
        approximate: false,
        status: "done",
      },
    ];
    const summary = summarizeFileEdits(edits, false);
    expect(summary).toHaveLength(1);
    expect(hasVisibleDiffStats(summary[0])).toBe(true);
  });

  it("formats file edit error messages cleanly", () => {
    const edits: UIFileEdit[] = [
      {
        call_id: "call-fail",
        tool: "apply_patch",
        path: "angry-birds.html",
        phase: "error",
        added: 0,
        deleted: 0,
        approximate: false,
        status: "error",
        error: "Error applying patch: old_text not found in angry-birds.html",
      },
    ];
    const summary = summarizeFileEdits(edits, false);
    expect(summary).toHaveLength(1);
    expect(summary[0].status).toBe("error");
    // The raw error is preserved on the summary; formatting happens in the view layer.
    expect(summary[0].error).toContain("old_text not found");
  });
});
