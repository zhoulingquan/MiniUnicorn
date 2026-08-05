import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MemoryEmbeddingSettings } from "@/components/settings/sections/MemoryEmbeddingSettings";
import type { EmbeddingStatusPayload } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

function statusPayload(overrides: Partial<EmbeddingStatusPayload> = {}): EmbeddingStatusPayload {
  return {
    model: { state: "ready", model_id: "BAAI/bge-small-zh-v1.5", revision: "abc", dimension: 512, cache_path: "/cache", bytes: 100, last_self_test: "2026-08-04", last_error_code: null, message: "" },
    index: { state: "ready", path: "/ws/memory/memory.db", bytes: 200, last_rebuild: "2026-08-04", last_error_code: null, message: "" },
    sources: { discovered: 2, indexed: 2, pending: 0, stale: 0, invalid: 0, inactive: 0, errors: [] },
    recall: { configured: true, active: true, fallback_reason: null, last_self_test: "2026-08-04", last_latency_ms: 1.5 },
    operation: null,
    ...overrides,
  };
}

function renderComponent() {
  render(<MemoryEmbeddingSettings token="token" />);
}

describe("MemoryEmbeddingSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders four plain-language status cards", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/embedding/status")) return jsonResponse(statusPayload());
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    renderComponent();
    expect(await screen.findByText("模型")).toBeInTheDocument();
    expect(screen.getByText("索引")).toBeInTheDocument();
    expect(screen.getByText("来源同步")).toBeInTheDocument();
    expect(screen.getByText("实际检索")).toBeInTheDocument();
  });

  it("disables every operation while rebuild is running", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/embedding/status")) {
        return jsonResponse(
          statusPayload({
            operation: { id: "op-1", kind: "rebuild", state: "running", completed: 2, total: 8, message: "" },
          }),
        );
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    renderComponent();
    expect(await screen.findByText((content) => content.includes("2 / 8"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新下载模型" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "校验模型" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重建索引" })).toBeDisabled();
  });

  it("shows source identity but never raw vectors", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/embedding/status")) return jsonResponse(statusPayload());
      if (url.includes("/api/embedding/search")) {
        return jsonResponse({
          results: [
            {
              source_id: "user:preferences:1",
              source_type: "user",
              source_file: "USER.md",
              source_revision: "1",
              text: "早餐喝豆浆",
              content_hash: "abc",
              similarity: 0.92,
              score: 0.96,
              token_count: 10,
              synchronized: true,
            },
          ],
          fallback_reason: null,
          latency_ms: 1.2,
        });
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    renderComponent();
    const searchbox = await screen.findByRole("searchbox");
    fireEvent.change(searchbox, { target: { value: "主题" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索记忆" }));
    expect(await screen.findByText("USER.md · user:preferences:1")).toBeInTheDocument();
    expect(screen.queryByText(/embedding.*\[/i)).not.toBeInTheDocument();
  });
});
