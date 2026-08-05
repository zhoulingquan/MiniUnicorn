import { useCallback, useEffect, useState } from "react";

import { fetchEmbeddingStatus } from "@/lib/api";
import type { EmbeddingStatusPayload } from "@/lib/types";

export interface UseEmbeddingStatusResult {
  status: EmbeddingStatusPayload | null;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useEmbeddingStatus(token: string): UseEmbeddingStatusResult {
  const [status, setStatus] = useState<EmbeddingStatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await fetchEmbeddingStatus(token));
      setError(null);
    } catch (value) {
      setError((value as Error).message);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (status?.operation?.state !== "running") return;
    const id = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(id);
  }, [refresh, status?.operation?.state]);

  return { status, error, refresh };
}
