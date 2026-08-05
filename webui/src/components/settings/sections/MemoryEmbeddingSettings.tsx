import { useState } from "react";

import { searchEmbeddingMemory, startEmbeddingOperation } from "@/lib/api";
import type { EmbeddingSearchResult, EmbeddingStatusPayload } from "@/lib/types";

import { useEmbeddingStatus } from "../hooks/useEmbeddingStatus";

type StateColor = "green" | "blue" | "yellow" | "red" | "gray";

function stateColor(state: string): StateColor {
  if (state === "ready" || state === "active") return "green";
  if (state === "building" || state === "downloading" || state === "verifying") return "blue";
  if (state === "missing" || state === "stale" || state === "not_downloaded") return "yellow";
  if (state === "corrupt" || state === "failed") return "red";
  return "gray";
}

const COLOR_CLASS: Record<StateColor, string> = {
  green: "text-green-500",
  blue: "text-blue-500",
  yellow: "text-yellow-500",
  red: "text-red-500",
  gray: "text-gray-500",
};

function StatusCard({
  label,
  state,
  detail,
}: {
  label: string;
  state: string;
  detail?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className={`mt-1 text-xl font-bold ${COLOR_CLASS[stateColor(state)]}`}>
        {state}
      </div>
      {detail && <div className="mt-1 text-xs text-muted-foreground">{detail}</div>}
    </div>
  );
}

export function MemoryEmbeddingSettings({ token }: { token: string }) {
  const { status, error, refresh } = useEmbeddingStatus(token);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<EmbeddingSearchResult[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  const operationRunning = status?.operation?.state === "running";

  const handleOperation = async (kind: "setup" | "verify" | "rebuild") => {
    try {
      await startEmbeddingOperation(token, kind);
      await refresh();
    } catch (err) {
      setSearchError((err as Error).message);
    }
  };

  const handleSearch = async () => {
    const query = searchQuery.trim();
    if (!query) return;
    setSearching(true);
    setSearchError(null);
    try {
      const payload = await searchEmbeddingMemory(token, query);
      setSearchResults(payload.results);
    } catch (err) {
      setSearchError((err as Error).message);
      setSearchResults(null);
    } finally {
      setSearching(false);
    }
  };

  if (error) {
    return <div className="p-4 text-red-500">{error}</div>;
  }

  if (!status) {
    return <div className="p-4 text-muted-foreground">Loading...</div>;
  }

  const modelDetail = [status.model.model_id, status.model.dimension ? `${status.model.dimension}d` : null]
    .filter(Boolean)
    .join(" ");
  const indexDetail = status.index.bytes ? `${status.index.bytes}B` : undefined;
  const sourcesDetail = `${status.sources.indexed}/${status.sources.discovered} indexed`;
  const recallDetail = status.recall.last_latency_ms
    ? `${status.recall.last_latency_ms.toFixed(1)}ms`
    : undefined;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <StatusCard label="模型" state={status.model.state} detail={modelDetail} />
        <StatusCard label="索引" state={status.index.state} detail={indexDetail} />
        <StatusCard label="来源同步" state={status.sources.indexed > 0 ? "ready" : "missing"} detail={sourcesDetail} />
        <StatusCard
          label="实际检索"
          state={status.recall.active ? "active" : status.recall.fallback_reason ?? "inactive"}
          detail={recallDetail}
        />
      </div>

      {operationRunning && status.operation && (
        <div className="rounded-lg border border-blue-500/30 bg-blue-500/5 p-4">
          <div className="text-sm font-medium text-blue-500">
            {`${status.operation.kind} 进行中… ${status.operation.completed} / ${status.operation.total}`}
          </div>
          {status.operation.message && (
            <div className="mt-1 text-xs text-muted-foreground">{status.operation.message}</div>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50"
          disabled={operationRunning}
          onClick={() => void handleOperation("setup")}
        >
          重新下载模型
        </button>
        <button
          type="button"
          className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50"
          disabled={operationRunning}
          onClick={() => void handleOperation("verify")}
        >
          校验模型
        </button>
        <button
          type="button"
          className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50"
          disabled={operationRunning}
          onClick={() => void handleOperation("rebuild")}
        >
          重建索引
        </button>
      </div>

      <details className="rounded-lg border border-border">
        <summary className="cursor-pointer p-4 text-sm font-medium">技术详情</summary>
        <div className="space-y-3 p-4 pt-0 text-xs">
          <div>
            <strong>Model:</strong> {status.model.model_id ?? "-"} / rev {status.model.revision ?? "-"} /{" "}
            {status.model.dimension ?? "-"}d / {status.model.bytes}B / path {status.model.cache_path ?? "-"}
          </div>
          <div>
            <strong>Index:</strong> {status.index.path ?? "-"} / {status.index.bytes}B / rebuild{" "}
            {status.index.last_rebuild ?? "-"}
          </div>
          <div>
            <strong>Sources:</strong> discovered={status.sources.discovered} indexed={status.sources.indexed}{" "}
            pending={status.sources.pending} stale={status.sources.stale} invalid={status.sources.invalid}
          </div>
          <div>
            <strong>Recall:</strong> configured={String(status.recall.configured)} active={String(status.recall.active)}{" "}
            fallback={status.recall.fallback_reason ?? "-"} latency={status.recall.last_latency_ms ?? "-"}
          </div>
        </div>
      </details>

      <div className="space-y-3">
        <div className="flex gap-2">
          <input
            type="search"
            role="searchbox"
            className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
            placeholder="搜索记忆…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleSearch();
            }}
          />
          <button
            type="button"
            className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground"
            disabled={searching || !searchQuery.trim()}
            onClick={() => void handleSearch()}
          >
            搜索记忆
          </button>
        </div>
        {searchError && <div className="text-sm text-red-500">{searchError}</div>}
        {searchResults && (
          <div className="space-y-2">
            {searchResults.length === 0 && (
              <div className="text-sm text-muted-foreground">未找到相关记忆。</div>
            )}
            {searchResults.map((row) => (
              <div key={row.source_id} className="rounded-md border border-border p-3 text-sm">
                <div className="font-medium">
                  {row.source_file} · {row.source_id}
                </div>
                <div className="mt-1 text-muted-foreground">{row.text}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  similarity={row.similarity.toFixed(3)} score={row.score.toFixed(3)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
