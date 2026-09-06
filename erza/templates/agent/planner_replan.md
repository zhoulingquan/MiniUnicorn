You are replanning a task after a step failed. Given the original task, completed steps, and the failed step with its reason, produce a NEW plan for the remaining work.

Output STRICT JSON only (no prose, no markdown fences). Same schema as before:
{
  "goal": "<restated goal>",
  "steps": [
    {"id": 1, "action": "...", "tool_hint": "...", "done_criteria": "..."}
  ]
}

Rules:
- Do NOT repeat completed steps.
- Avoid the approach that failed.
- Keep steps 2-4.
- Reuse step ids starting from 1.
