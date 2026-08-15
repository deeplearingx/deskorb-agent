"""Fast, read-only research adapters for tasks that do not need a browser UI.

The first adapter is deliberately narrow: GitHub repository metadata, README
installation hints, capability signals, and open Issue titles.  It is a
research data source, not a second browser controller.  The caller receives a
bounded, provenance-preserving evidence ledger and never receives the raw
README, credentials, or upstream error payloads.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

from browser_evidence import BrowserEvidenceLedger, normalize_capability_status, safe_http_url


_GITHUB_API = "https://api.github.com"
_REPOSITORY_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_ALLOWED_FIELDS = frozenset({
    "title", "url", "repository", "stars", "language", "updated_at",
    "installation", "mcp", "memory", "multi_agent", "tool_calling",
    "issue_title",
})
_DEFAULT_FIELDS = (
    "title", "url", "repository", "stars", "language", "updated_at",
    "installation", "mcp", "memory", "multi_agent", "tool_calling", "issue_title",
)
_CAPABILITY_MARKERS: dict[str, tuple[str, ...]] = {
    "mcp": (r"\bmcp\b",),
    "memory": (r"\bmemory\b", r"long[- ]term memory", r"short[- ]term memory"),
    "multi_agent": (r"multi[- ]agent", r"multiagent", r"multi agent"),
    "tool_calling": (r"tool[- ]calling", r"tool calling", r"function calling", r"tool use"),
}
_INSTALL_COMMAND = re.compile(
    r"\b(?:pip|pipx)\s+install\b[^\n`]{0,240}"
    r"|\buv\s+(?:add|pip\s+install)\b[^\n`]{0,240}"
    r"|\bpoetry\s+add\b[^\n`]{0,240}"
    r"|\bnpm\s+install\b[^\n`]{0,240}",
    re.IGNORECASE,
)


class ResearchFetchError(RuntimeError):
    """Internal bounded error; its message is never returned to the model."""

    def __init__(self, failure_kind: str):
        self.failure_kind = str(failure_kind or "research_fetch_failed")[:80]
        super().__init__(self.failure_kind)


class GitHubResearchClient:
    """Collect bounded GitHub evidence through the read-only REST API."""

    def __init__(self, *, opener: Any | None = None, token: str | None = None,
                 proxy_url: str | None = None, timeout_seconds: float = 15.0,
                 max_response_bytes: int = 1_500_000):
        self.timeout_seconds = max(3.0, min(60.0, float(timeout_seconds)))
        self.max_response_bytes = max(64_000, min(4_000_000, int(max_response_bytes)))
        self.token = str(token or os.environ.get("DESKORB_AGENT_GITHUB_TOKEN")
                       or os.environ.get("GITHUB_TOKEN") or "").strip()
        if opener is not None:
            self._opener = opener
        else:
            proxy = str(proxy_url or os.environ.get("DESKORB_AGENT_API_PROXY")
                       or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                       or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or "").strip()
            proxy_handler = urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy} if proxy else None
            )
            self._opener = urllib.request.build_opener(proxy_handler)

    def research_repositories(
        self,
        repositories: Iterable[str],
        *,
        required_fields: Iterable[str] = (),
        include_issues: bool = True,
        issue_limit: int = 2,
    ) -> dict[str, Any]:
        """Return one composite evidence record per valid repository slug."""
        slugs = self._validate_repositories(repositories)
        if isinstance(slugs, dict):
            return slugs
        fields = self._validate_fields(required_fields)
        if isinstance(fields, dict):
            return fields
        issue_limit = max(1, min(5, int(issue_limit)))
        ledger = BrowserEvidenceLedger(max_records=60)
        successes: list[str] = []
        failures: list[dict[str, str]] = []

        for slug in slugs:
            try:
                metadata = self._get_json(f"/repos/{slug}")
                readme = self._get_json(f"/repos/{slug}/readme")
                readme_text = self._decode_readme(readme)
                page_url = safe_http_url(metadata.get("html_url")) or f"https://github.com/{slug}"
                requested = set(fields)
                requested.update({"title", "url"})
                if include_issues:
                    requested.add("issue_title")
                record_fields = self._repository_fields(slug, metadata, readme_text, requested)
                issue_titles: list[str] = []
                if include_issues:
                    issue_payload = self._get_json(
                        f"/repos/{slug}/issues",
                        {"state": "open", "per_page": issue_limit},
                    )
                    issue_titles = self._add_issues(
                        ledger, slug, issue_payload, issue_limit,
                    )
                    record_fields["issue_title"] = " | ".join(issue_titles) if issue_titles else "unknown"
                ledger.add(
                    kind="research_repository",
                    tab_id="research:github",
                    observation_id=f"github:{slug}",
                    source_url=page_url,
                    fields=record_fields,
                    supporting_text="GitHub API metadata and bounded README signals",
                )
                # Issue rows are intentionally supplemental.  Attach their
                # titles to the repository record after it exists so a final
                # verifier can require one complete framework record.
                if issue_titles:
                    ledger.merge_repository_fields(
                        source_url=f"https://github.com/{slug}/issues",
                        fields={"issue_title": " | ".join(issue_titles)},
                        supporting_text="Open Issue titles",
                    )
                successes.append(slug)
            except ResearchFetchError as exc:
                failures.append({"repository": slug, "failure_kind": exc.failure_kind})

        safe_ledger = ledger.safe_dict()
        if not successes:
            failure_kind = failures[0]["failure_kind"] if failures else "research_no_records"
            return self._result(
                ok=False,
                status="error",
                failure_kind=failure_kind,
                ledger=safe_ledger,
                successes=successes,
                failures=failures,
            )
        return self._result(
            ok=True,
            status="partial" if failures else "success",
            failure_kind="research_partial" if failures else None,
            ledger=safe_ledger,
            successes=successes,
            failures=failures,
        )

    @staticmethod
    def _validate_repositories(value: Iterable[str]) -> list[str] | dict[str, Any]:
        try:
            candidates = [str(item or "").strip() for item in value]
        except TypeError:
            return {"ok": False, "status": "error", "failure_kind": "research_invalid_repository"}
        if not candidates or len(candidates) > 5:
            return {"ok": False, "status": "error", "failure_kind": "research_repository_limit"}
        slugs: list[str] = []
        for candidate in candidates:
            if not _REPOSITORY_SLUG.fullmatch(candidate):
                return {"ok": False, "status": "error", "failure_kind": "research_invalid_repository"}
            normalized = candidate.casefold()
            if normalized not in {item.casefold() for item in slugs}:
                slugs.append(candidate)
        return slugs

    @staticmethod
    def _validate_fields(value: Iterable[str]) -> tuple[str, ...] | dict[str, Any]:
        try:
            requested = [str(item or "").strip() for item in value if str(item or "").strip()]
        except TypeError:
            return {"ok": False, "status": "error", "failure_kind": "research_invalid_fields"}
        if len(requested) > 12 or any(item not in _ALLOWED_FIELDS for item in requested):
            return {"ok": False, "status": "error", "failure_kind": "research_invalid_fields"}
        return tuple(dict.fromkeys(requested or _DEFAULT_FIELDS))

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = _GITHUB_API + str(path)
        if query:
            url += "?" + query
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DeskOrb-Agent-Research/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429}:
                raise ResearchFetchError("research_rate_limited") from None
            if exc.code == 404:
                raise ResearchFetchError("research_not_found") from None
            raise ResearchFetchError("research_http_failed") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ResearchFetchError("research_network_failed") from None
        if len(raw) > self.max_response_bytes:
            raise ResearchFetchError("research_response_too_large")
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            raise ResearchFetchError("research_invalid_response") from None

    @staticmethod
    def _decode_readme(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ResearchFetchError("research_readme_missing")
        encoded = str(payload.get("content") or "")
        if not encoded:
            raise ResearchFetchError("research_readme_missing")
        try:
            decoded = base64.b64decode(encoded.replace("\n", ""), validate=False)
        except (ValueError, TypeError):
            raise ResearchFetchError("research_readme_invalid") from None
        return decoded.decode("utf-8", "replace")[:400_000]

    @classmethod
    def _repository_fields(cls, slug: str, metadata: dict[str, Any], readme: str,
                           requested: set[str]) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            raise ResearchFetchError("research_invalid_response")
        html_url = safe_http_url(metadata.get("html_url")) or f"https://github.com/{slug}"
        fields: dict[str, Any] = {
            "title": str(metadata.get("name") or metadata.get("full_name") or slug).strip(),
            "url": html_url,
            "repository": str(metadata.get("full_name") or slug).strip(),
            "stars": metadata.get("stargazers_count"),
            "language": str(metadata.get("language") or "unknown").strip() or "unknown",
            "updated_at": metadata.get("pushed_at") or metadata.get("updated_at") or "unknown",
            "installation": cls._installation_excerpt(readme) or "unknown",
        }
        for field, markers in _CAPABILITY_MARKERS.items():
            matching_lines = [
                line.strip() for line in readme.splitlines()
                if any(re.search(marker, line, re.IGNORECASE) for marker in markers)
            ]
            fields[field] = normalize_capability_status(" ".join(matching_lines[:6]))
        return {key: fields.get(key, "unknown") for key in requested}

    @staticmethod
    def _installation_excerpt(readme: str) -> str:
        for line in readme.splitlines():
            cleaned = line.strip().strip("`* ")
            match = _INSTALL_COMMAND.search(cleaned)
            if match:
                return match.group(0).strip()[:500]
        return ""

    @staticmethod
    def _add_issues(ledger: BrowserEvidenceLedger, slug: str, payload: Any,
                    issue_limit: int) -> list[str]:
        if not isinstance(payload, list):
            raise ResearchFetchError("research_invalid_response")
        titles: list[str] = []
        source_url = f"https://github.com/{slug}/issues"
        for issue in payload:
            if not isinstance(issue, dict) or issue.get("pull_request"):
                continue
            title = str(issue.get("title") or "").strip()[:300]
            url = safe_http_url(issue.get("html_url"))
            if not title or not url or len(titles) >= issue_limit:
                continue
            titles.append(title)
            ledger.add(
                kind="list_item",
                tab_id="research:github",
                observation_id=f"github:{slug}:issues",
                source_url=source_url,
                fields={"issue_title": title, "url": url},
                supporting_text=title,
            )
        return titles

    @staticmethod
    def _result(*, ok: bool, status: str, failure_kind: str | None,
                ledger: dict[str, Any], successes: list[str],
                failures: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "ok": bool(ok),
            "status": status,
            "failure_kind": failure_kind,
            "summary": (f"Collected {len(successes)} GitHub repository record(s)."
                         if successes else "No GitHub repository record was collected."),
            "next_actions": (["Use the evidence ledger for comparison and final verification."]
                              if successes else ["Retry once after checking the GitHub API/network status."]),
            "artifacts": {"evidence_ledger": ledger},
            "evidence_ledger": ledger,
            "record_count": len(successes),
            "repositories": list(successes)[:5],
            "failed_repositories": list(failures)[:5],
        }


__all__ = ["GitHubResearchClient", "ResearchFetchError"]
