import { describe, expect, it } from "vitest";

import type { InboundEvent } from "@/lib/types";

function eventName(event: InboundEvent): InboundEvent["event"] {
  return event.event;
}

describe("generated agent event contract", () => {
  it("accepts a versioned turn_end event", () => {
    const event: InboundEvent = {
      protocol_version: 1,
      event: "turn_end",
      chat_id: "chat-1",
      context_usage: {
        prompt_tokens: 12,
        completion_tokens: 3,
        total_tokens: 15,
        cached_tokens: 0,
      },
    };
    expect(eventName(event)).toBe("turn_end");
  });

  it("includes subagent activity in the discriminated union", () => {
    const event: InboundEvent = {
      protocol_version: 1,
      event: "subagent_activity",
      chat_id: "chat-1",
      content: "working",
    };
    expect(eventName(event)).toBe("subagent_activity");
  });
});
