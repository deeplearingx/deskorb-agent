"""Run the bounded local GitHub Research Mode without starting a browser."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_runtime import GitHubResearchClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect bounded public GitHub evidence without a browser.")
    parser.add_argument("--repo", action="append", required=True,
                        help="Repository slug such as langchain-ai/langgraph; repeat up to five times.")
    parser.add_argument("--field", action="append", dest="fields", default=[],
                        help="Evidence field to require; repeat as needed.")
    parser.add_argument("--include-issues", action="store_true")
    parser.add_argument("--issue-limit", type=int, default=2)
    args = parser.parse_args(argv)
    result = GitHubResearchClient().research_repositories(
        args.repo,
        required_fields=args.fields,
        include_issues=bool(args.include_issues),
        issue_limit=max(1, min(5, int(args.issue_limit))),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
