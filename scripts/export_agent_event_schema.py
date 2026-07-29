from __future__ import annotations

import argparse
import json
from pathlib import Path

from miniunicorn.bus.agent_events import AGENT_EVENT_ADAPTER

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPOSITORY_ROOT / "webui" / "src" / "generated" / "agent-events.schema.json"


def render_schema() -> str:
    schema = AGENT_EVENT_ADAPTER.json_schema()
    schema["title"] = "InboundEvent"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_schema()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != rendered:
            raise SystemExit("agent event JSON schema is stale")
        return
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
