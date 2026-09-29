#!/usr/bin/env python3
"""Export the StreamAI fallback tool manifest for offline review.

The native release publishes authoritative tools and prompts with `agentc`.
This utility only exports the embedded degraded-mode registry and is not a
substitute for `scripts/04d-publish-agent-catalog.sh`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ui.app.agent_service import GovernedAgentService


class _NoopCatalogue:
    def sync_agent_catalog(self, _documents):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="config/agent-catalog/streamai-agent-v1.0.0-fallback.json",
        help="Destination JSON path",
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = GovernedAgentService(_NoopCatalogue()).catalog_snapshot()
    output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    print(output)


if __name__ == "__main__":
    main()
