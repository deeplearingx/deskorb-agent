import base64
import json
from urllib.parse import urlsplit
import unittest

from research_runtime import GitHubResearchClient


class _FakeResponse:
    def __init__(self, payload, *, status=200, headers=None):
        self._payload = payload
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit=-1):
        data = self._payload
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return data if limit < 0 else data[:limit]


class _FakeOpener:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append((url, timeout, dict(request.header_items())))
        parsed = urlsplit(url)
        key = parsed.path
        if parsed.query:
            key += "?" + parsed.query
        route = self.routes.get(key)
        if isinstance(route, Exception):
            raise route
        if route is None:
            raise AssertionError(f"unexpected request: {url}")
        return route


def _repository_routes(readme: str):
    slug = "langchain-ai/langgraph"
    readme_payload = {
        "content": base64.b64encode(readme.encode("utf-8")).decode("ascii"),
        "encoding": "base64",
        "html_url": f"https://github.com/{slug}#readme",
    }
    routes = {
        f"/repos/{slug}": {
            "full_name": slug,
            "html_url": f"https://github.com/{slug}",
            "stargazers_count": 37000,
            "language": "Python",
            "pushed_at": "2026-08-01T12:00:00Z",
            "updated_at": "2026-08-01T12:00:00Z",
            "description": "Build resilient agents.",
        },
        f"/repos/{slug}/readme": readme_payload,
        f"/repos/{slug}/issues?state=open&per_page=2": [
            {"title": "First issue", "html_url": f"https://github.com/{slug}/issues/1",
             "updated_at": "2026-08-02T12:00:00Z"},
            {"title": "Pull request should be ignored", "html_url": f"https://github.com/{slug}/pull/2",
             "pull_request": {"url": "https://api.github.com/repos/x/pulls/2"}},
        ],
    }
    return {
        key: value if isinstance(value, Exception) else _FakeResponse(value)
        for key, value in routes.items()
    }


class GitHubResearchClientTests(unittest.TestCase):
    def test_repository_readme_and_issues_form_composite_evidence(self):
        opener = _FakeOpener(_repository_routes(
            """# LangGraph\n\n## Installation\n```bash\npip install -U langgraph\n```\n\nSupports MCP and tool calling. Memory is available for long-running agents.\n"""
        ))
        client = GitHubResearchClient(opener=opener, token="do-not-return")

        result = client.research_repositories(
            ["langchain-ai/langgraph"],
            required_fields=("title", "url", "stars", "language", "updated_at",
                             "installation", "mcp", "memory", "tool_calling", "issue_title"),
            include_issues=True,
            issue_limit=2,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["record_count"], 1)
        records = result["evidence_ledger"]["records"]
        repository = next(item for item in records if item["kind"] == "research_repository")
        fields = repository["fields"]
        self.assertEqual(fields["stars"], "37000")
        self.assertEqual(fields["language"], "Python")
        self.assertEqual(fields["installation"], "pip install -U langgraph")
        self.assertEqual(fields["mcp"], "yes")
        self.assertEqual(fields["memory"], "yes")
        self.assertEqual(fields["tool_calling"], "yes")
        self.assertEqual(fields["issue_title"], "First issue")
        self.assertEqual(sum(item["kind"] == "list_item" for item in records), 1)
        self.assertNotIn("do-not-return", json.dumps(result, ensure_ascii=False))

    def test_missing_capability_is_unknown_not_no(self):
        opener = _FakeOpener(_repository_routes("# Demo\n\nInstallation is documented elsewhere.\n"))
        result = GitHubResearchClient(opener=opener).research_repositories(
            ["langchain-ai/langgraph"], required_fields=("title", "mcp", "memory"),
            include_issues=False,
        )
        fields = result["evidence_ledger"]["records"][0]["fields"]
        self.assertEqual(fields["mcp"], "unknown")
        self.assertEqual(fields["memory"], "unknown")

    def test_invalid_repository_slug_is_rejected_without_network(self):
        opener = _FakeOpener({})
        result = GitHubResearchClient(opener=opener).research_repositories(
            ["https://evil.example/readme", "../escape"],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_kind"], "research_invalid_repository")
        self.assertEqual(opener.calls, [])

    def test_rate_limit_error_is_classified_without_leaking_headers_or_token(self):
        from urllib.error import HTTPError

        opener = _FakeOpener({
            "/repos/langchain-ai/langgraph": HTTPError(
                "https://api.github.com/repos/langchain-ai/langgraph", 403,
                "rate limited", {"X-RateLimit-Remaining": "0"}, None,
            ),
        })
        result = GitHubResearchClient(opener=opener, token="secret-token").research_repositories(
            ["langchain-ai/langgraph"],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "research_rate_limited")
        self.assertNotIn("secret-token", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("RateLimit", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
