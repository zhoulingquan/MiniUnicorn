import { describe, expect, it } from "vitest";

import { reduceStream, type StreamAction } from "@/hooks/stream-reducer";
import {
  createInitialStreamState,
  type StreamState,
} from "@/hooks/stream-state";

const NOW = 1_000;

function dispatchAll(state: StreamState, actions: StreamAction[]): StreamState {
  return actions.reduce((prev, action) => reduceStream(prev, action), state);
}

describe("reduceStream", () => {
  it("appends reasoning_delta text to a new assistant placeholder", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "reasoning_delta",
      chatId: "chat-1",
      text: "thinking",
      receivedAt: NOW,
    });
    expect(next.messages.at(-1)?.reasoning).toBe("thinking");
    expect(next.messages.at(-1)?.reasoningStreaming).toBe(true);
    expect(next.streaming).toBe(true);
  });

  it("closes the reasoning stream on reasoning_end", () => {
    const state = createInitialStreamState("chat-1");
    const reasoning = reduceStream(state, {
      type: "reasoning_delta",
      chatId: "chat-1",
      text: "thinking",
      receivedAt: NOW,
    });
    const next = reduceStream(reasoning, {
      type: "reasoning_end",
      chatId: "chat-1",
      receivedAt: NOW + 1,
    });
    expect(next.messages.at(-1)?.reasoning).toBe("thinking");
    expect(next.messages.at(-1)?.reasoningStreaming).toBe(false);
  });

  it("appends answer delta text to a new assistant message", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "Hello",
      receivedAt: NOW,
    });
    expect(next.messages.at(-1)?.content).toBe("Hello");
    expect(next.messages.at(-1)?.isStreaming).toBe(true);
    expect(next.streaming).toBe(true);
  });

  it("closes the active answer segment on stream_end", () => {
    const state = createInitialStreamState("chat-1");
    const delta = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "Hello",
      receivedAt: NOW,
    });
    expect(delta.cursor).not.toBeNull();
    const next = reduceStream(delta, {
      type: "stream_end",
      chatId: "chat-1",
      receivedAt: NOW + 1,
    });
    expect(next.cursor).toBeNull();
    expect(next.buffer).toBeNull();
  });

  it("replaces streamed content with final stream_end text", () => {
    const state = createInitialStreamState("chat-1");
    const delta = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "draft",
      receivedAt: NOW,
    });
    const next = reduceStream(delta, {
      type: "stream_end",
      chatId: "chat-1",
      finalAnswerText: "final",
      receivedAt: NOW + 1,
    });
    expect(next.messages.at(-1)?.content).toBe("final");
  });

  it("finalizes the turn on turn_end with latency and clears streaming", () => {
    const state = createInitialStreamState("chat-1");
    const delta = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "answer",
      receivedAt: NOW,
    });
    const next = reduceStream(delta, {
      type: "turn_end",
      chatId: "chat-1",
      latencyMs: 2400,
      receivedAt: NOW + 100,
    });
    expect(next.streaming).toBe(false);
    expect(next.messages.at(-1)?.isStreaming).toBe(false);
    expect(next.messages.at(-1)?.latencyMs).toBe(2400);
  });

  it("prunes reasoning-only placeholders on turn_end", () => {
    const state = createInitialStreamState("chat-1");
    const reasoning = reduceStream(state, {
      type: "reasoning_delta",
      chatId: "chat-1",
      text: "thinking without final text",
      receivedAt: NOW,
    });
    const ended = reduceStream(reasoning, {
      type: "reasoning_end",
      chatId: "chat-1",
      receivedAt: NOW + 1,
    });
    const next = reduceStream(ended, {
      type: "turn_end",
      chatId: "chat-1",
      receivedAt: NOW + 2,
    });
    expect(next.messages).toHaveLength(0);
    expect(next.streaming).toBe(false);
  });

  it("creates a trace row for tool_hint messages", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "tool_progress",
      chatId: "chat-1",
      text: 'write_file({"path":"foo.txt"})',
      kind: "tool_hint",
      receivedAt: NOW,
    });
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].kind).toBe("trace");
    expect(next.messages[0].traces).toEqual(['write_file({"path":"foo.txt"})']);
  });

  it("upgrades a pending file_edit placeholder when the path arrives", () => {
    const state = createInitialStreamState("chat-1");
    const pending = reduceStream(state, {
      type: "file_edit",
      chatId: "chat-1",
      edits: [
        {
          call_id: "call-write",
          tool: "write_file",
          path: "",
          phase: "start",
          added: 1,
          deleted: 0,
          approximate: true,
          status: "editing",
          pending: true,
        },
      ],
      receivedAt: NOW,
    });
    expect(pending.messages).toHaveLength(1);
    expect(pending.messages[0].fileEdits?.[0]?.path).toBe("");

    const next = reduceStream(pending, {
      type: "file_edit",
      chatId: "chat-1",
      edits: [
        {
          call_id: "call-write",
          tool: "write_file",
          path: "foo.txt",
          phase: "start",
          added: 12,
          deleted: 0,
          approximate: true,
          status: "editing",
        },
      ],
      receivedAt: NOW + 1,
    });
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].fileEdits?.[0]?.path).toBe("foo.txt");
    expect(next.messages[0].fileEdits?.[0]?.added).toBe(12);
  });

  it("demotes interrupted pre-tool text to reasoning when a tool trace arrives", () => {
    const state = createInitialStreamState("chat-1");
    const delta = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "I will inspect the project first.",
      receivedAt: NOW,
    });
    const streamEnd = reduceStream(delta, {
      type: "stream_end",
      chatId: "chat-1",
      receivedAt: NOW + 1,
    });
    const next = reduceStream(streamEnd, {
      type: "tool_progress",
      chatId: "chat-1",
      text: 'exec({"cmd":"ls"})',
      kind: "tool_hint",
      receivedAt: NOW + 2,
    });
    expect(next.messages).toHaveLength(2);
    expect(next.messages[0]).toMatchObject({
      role: "assistant",
      content: "",
      reasoning: "I will inspect the project first.",
      isStreaming: false,
    });
    expect(next.messages[1]).toMatchObject({
      role: "tool",
      kind: "trace",
      traces: ['exec({"cmd":"ls"})'],
    });
  });

  it("attaches media to a complete assistant message", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "assistant_message",
      chatId: "chat-1",
      text: "video ready",
      media: [{ kind: "video", url: "/api/media/sig/payload", name: "demo.mp4" }],
      receivedAt: NOW,
    });
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].media).toEqual([
      { kind: "video", url: "/api/media/sig/payload", name: "demo.mp4" },
    ]);
  });

  it("resets state on session_switch", () => {
    const state = createInitialStreamState("chat-1");
    const delta = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "some text",
      receivedAt: NOW,
    });
    expect(delta.messages).toHaveLength(1);

    const next = reduceStream(delta, {
      type: "session_switch",
      chatId: "chat-2",
      messages: [],
      runStartedAt: null,
      goalState: undefined,
      hasPendingToolCalls: false,
    });
    expect(next.chatId).toBe("chat-2");
    expect(next.messages).toEqual([]);
    expect(next.streaming).toBe(false);
    expect(next.cursor).toBeNull();
    expect(next.buffer).toBeNull();
  });

  it("restores goal_state on goal_state action", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "goal_state",
      chatId: "chat-1",
      goalState: { active: true, ui_summary: "Alpha" },
    });
    expect(next.goalState).toEqual({ active: true, ui_summary: "Alpha" });
  });

  it("tracks goal_status running and clears on idle", () => {
    const state = createInitialStreamState("chat-1");
    expect(state.runStartedAt).toBeNull();

    const running = reduceStream(state, {
      type: "goal_status",
      chatId: "chat-1",
      status: "running",
      startedAt: 1700,
    });
    expect(running.runStartedAt).toBe(1700);

    const idle = reduceStream(running, {
      type: "goal_status",
      chatId: "chat-1",
      status: "idle",
    });
    expect(idle.runStartedAt).toBeNull();
  });

  it("clears runStartedAt on turn_end even without idle", () => {
    const state = createInitialStreamState("chat-1");
    const running = reduceStream(state, {
      type: "goal_status",
      chatId: "chat-1",
      status: "running",
      startedAt: 1700,
    });
    expect(running.runStartedAt).toBe(1700);

    const next = reduceStream(running, {
      type: "turn_end",
      chatId: "chat-1",
      receivedAt: NOW,
    });
    expect(next.runStartedAt).toBeNull();
  });

  it("stamps latency on the last assistant bubble from turn_end", () => {
    const state = createInitialStreamState("chat-1");
    const delta = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "Hi",
      receivedAt: NOW,
    });
    const next = reduceStream(delta, {
      type: "turn_end",
      chatId: "chat-1",
      latencyMs: 2400,
      receivedAt: NOW + 1,
    });
    const lastAssistant = [...next.messages].reverse().find((m) => m.role === "assistant");
    expect(lastAssistant?.latencyMs).toBe(2400);
  });

  it("accumulates reasoning_delta chunks on a placeholder until reasoning_end", () => {
    const state = createInitialStreamState("chat-1");
    const next = dispatchAll(state, [
      { type: "reasoning_delta", chatId: "chat-1", text: "Let me think ", receivedAt: NOW },
      { type: "reasoning_delta", chatId: "chat-1", text: "step by step.", receivedAt: NOW + 1 },
      { type: "reasoning_end", chatId: "chat-1", receivedAt: NOW + 2 },
    ]);
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].reasoning).toBe("Let me think step by step.");
    expect(next.messages[0].reasoningStreaming).toBe(false);
  });

  it("absorbs a streaming reasoning placeholder into the answer turn that follows", () => {
    const state = createInitialStreamState("chat-1");
    const next = dispatchAll(state, [
      { type: "reasoning_delta", chatId: "chat-1", text: "Plan first.", receivedAt: NOW },
      { type: "reasoning_end", chatId: "chat-1", receivedAt: NOW + 1 },
      { type: "delta", chatId: "chat-1", text: "The answer is 42.", receivedAt: NOW + 2 },
      { type: "stream_end", chatId: "chat-1", receivedAt: NOW + 3 },
    ]);
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].content).toBe("The answer is 42.");
    expect(next.messages[0].reasoning).toBe("Plan first.");
    expect(next.messages[0].reasoningStreaming).toBe(false);
  });

  it("treats legacy kind=reasoning messages as a complete delta + end pair", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "reasoning_message",
      chatId: "chat-1",
      text: "one-shot reasoning",
      receivedAt: NOW,
    });
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].reasoning).toBe("one-shot reasoning");
    expect(next.messages[0].reasoningStreaming).toBe(false);
  });

  it("creates an assistant bubble from final stream_end text without prior delta", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "stream_end",
      chatId: "chat-1",
      finalAnswerText: "![Diagram](/api/media/sig/payload)",
      receivedAt: NOW,
    });
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].content).toBe("![Diagram](/api/media/sig/payload)");
    expect(next.messages[0].isStreaming).toBe(true);
  });

  it("collapses consecutive tool_hint frames into one trace row", () => {
    const state = createInitialStreamState("chat-1");
    const next = dispatchAll(state, [
      { type: "tool_progress", chatId: "chat-1", text: 'weather("get")', kind: "tool_hint", receivedAt: NOW },
      { type: "tool_progress", chatId: "chat-1", text: 'search "hk weather"', kind: "tool_hint", receivedAt: NOW + 1 },
    ]);
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].kind).toBe("trace");
    expect(next.messages[0].traces).toEqual(['weather("get")', 'search "hk weather"']);
  });

  it("keeps streaming alive across stream_end and completes on turn_end", () => {
    const state = createInitialStreamState("chat-1");
    const mid = dispatchAll(state, [
      { type: "delta", chatId: "chat-1", text: "Hello", receivedAt: NOW },
      { type: "stream_end", chatId: "chat-1", receivedAt: NOW + 1 },
    ]);
    expect(mid.streaming).toBe(true);
    expect(mid.messages[0].isStreaming).toBe(true);

    const next = reduceStream(mid, {
      type: "turn_end",
      chatId: "chat-1",
      receivedAt: NOW + 2,
    });
    expect(next.streaming).toBe(false);
    expect(next.messages.every((m) => !m.isStreaming)).toBe(true);
  });

  it("suppresses redundant stream confirmation after assistant media", () => {
    const state = createInitialStreamState("chat-1");
    const next = dispatchAll(state, [
      {
        type: "assistant_message",
        chatId: "chat-1",
        text: "image ready",
        media: [{ kind: "image", url: "/api/media/sig/image", name: "generated.png" }],
        receivedAt: NOW,
      },
      { type: "tool_progress", chatId: "chat-1", text: "message()", kind: "tool_hint", receivedAt: NOW + 1 },
      { type: "delta", chatId: "chat-1", text: "发送成功", receivedAt: NOW + 2 },
      { type: "stream_end", chatId: "chat-1", receivedAt: NOW + 3 },
      { type: "turn_end", chatId: "chat-1", receivedAt: NOW + 4 },
    ]);
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].content).toBe("image ready");
    expect(next.messages[0].media).toHaveLength(1);
  });

  it("does not attach reasoning across a tool trace boundary", () => {
    const state = createInitialStreamState("chat-1");
    const next = dispatchAll(state, [
      { type: "reasoning_delta", chatId: "chat-1", text: "First reasoning.", receivedAt: NOW },
      { type: "reasoning_end", chatId: "chat-1", receivedAt: NOW + 1 },
      { type: "tool_progress", chatId: "chat-1", text: 'read_file({"path":"OpenClaw/README.md"})', kind: "tool_hint", receivedAt: NOW + 2 },
      { type: "reasoning_delta", chatId: "chat-1", text: "Second reasoning.", receivedAt: NOW + 3 },
    ]);
    expect(next.messages).toHaveLength(3);
    expect(next.messages.map((m) => m.kind ?? "message")).toEqual(["message", "trace", "message"]);
    expect(next.messages[0].reasoning).toBe("First reasoning.");
    expect(next.messages[2].reasoning).toBe("Second reasoning.");
  });

  it("adds a user message on send_message", () => {
    const state = createInitialStreamState("chat-1");
    const next = reduceStream(state, {
      type: "send_message",
      content: "hello world",
      receivedAt: NOW,
    });
    expect(next.messages).toHaveLength(1);
    expect(next.messages[0].role).toBe("user");
    expect(next.messages[0].content).toBe("hello world");
    expect(next.streaming).toBe(true);
  });

  it("stops streaming and marks all messages as not streaming on stop", () => {
    const state = createInitialStreamState("chat-1");
    const delta = reduceStream(state, {
      type: "delta",
      chatId: "chat-1",
      text: "streaming text",
      receivedAt: NOW,
    });
    expect(delta.streaming).toBe(true);
    const next = reduceStream(delta, {
      type: "stop",
      receivedAt: NOW + 1,
    });
    expect(next.streaming).toBe(false);
    expect(next.messages.every((m) => !m.isStreaming)).toBe(true);
  });
});
