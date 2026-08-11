"""DeskOrb's HTTPS-only, allowlisted public read-only Fetch MCP adapter."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from typing import Any, Iterable

from mcp_security import URLPolicyError, validate_public_url


class PublicFetchError(RuntimeError):
    """A bounded public fetch policy or transport failure."""


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 120 * 1024
MAX_REDIRECTS = 5
_CHARSET = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.IGNORECASE)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "template"}:
            self._ignored += 1
        elif tag.casefold() in {"p", "div", "li", "br", "h1", "h2", "h3", "tr"} and not self._ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "template"} and self._ignored:
            self._ignored -= 1
        elif tag.casefold() in {"p", "div", "li", "br", "h1", "h2", "h3", "tr"} and not self._ignored:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n\s*\n+", "\n", " ".join(self.parts)).strip()


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, adapter: "PublicFetchAdapter") -> None:
        super().__init__()
        self.adapter = adapter
        self.redirects = 0

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        self.redirects += 1
        if self.redirects > MAX_REDIRECTS:
            raise PublicFetchError("redirect limit exceeded")
        self.adapter.validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicFetchAdapter:
    """Fetch only allowlisted public HTTPS pages and return bounded text."""

    def __init__(self, allowed_domains: Iterable[str], *, timeout_seconds: int = 20,
                 max_response_bytes: int = MAX_RESPONSE_BYTES) -> None:
        self.allowed_domains = tuple(str(item).strip().lower() for item in allowed_domains if str(item).strip())
        self.timeout_seconds = max(3, min(60, int(timeout_seconds)))
        self.max_response_bytes = max(1024, min(MAX_RESPONSE_BYTES, int(max_response_bytes)))

    def validate_url(self, url: str, *, resolve_dns: bool = True) -> Any:
        try:
            return validate_public_url(url, self.allowed_domains,
                                       resolve_dns=resolve_dns)
        except URLPolicyError as exc:
            raise PublicFetchError(str(exc)) from exc

    def fetch(self, url: str, *, max_chars: int = MAX_OUTPUT_CHARS) -> dict[str, Any]:
        target = self.validate_url(url)
        limit = max(1, min(MAX_OUTPUT_CHARS, int(max_chars)))
        request = urllib.request.Request(
            target.url,
            headers={"Accept": "text/html,text/plain,application/json,application/xml;q=0.9",
                     "User-Agent": "DeskOrb-readonly-fetch/0.1"},
            method="GET",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _SafeRedirectHandler(self))
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                final_url = str(response.geturl() or getattr(response, "url", "") or target.url)
                final = self.validate_url(final_url)
                headers = getattr(response, "headers", {})
                content_type = str(headers.get("Content-Type", "text/plain")).split(";", 1)[0].strip().lower()
                if content_type not in {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}:
                    raise PublicFetchError("response content type is not allowlisted")
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise PublicFetchError("response exceeds the configured size limit")
        except PublicFetchError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise PublicFetchError("public fetch failed") from exc
        charset_match = _CHARSET.search(str(headers.get("Content-Type", "")))
        encoding = charset_match.group(1) if charset_match else "utf-8"
        try:
            text = body.decode(encoding, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        if content_type == "text/html":
            parser = _HTMLText()
            parser.feed(text)
            text = parser.text()
        text = text.replace("\x00", "")
        return {
            "ok": True,
            "host": final.hostname,
            "status": int(getattr(response, "status", 200) or 200),
            "content_type": content_type,
            "characters": len(text),
            "truncated": len(text) > limit,
            "text": text[:limit],
        }


class PublicFetchMcpServer:
    """Minimal stdio MCP server for the read-only public Fetch capability."""

    def __init__(self, allowed_domains: Iterable[str] = ()) -> None:
        self.adapter = PublicFetchAdapter(allowed_domains)

    @classmethod
    def from_environment(cls) -> "PublicFetchMcpServer":
        raw = os.environ.get("DESKORB_AGENT_PUBLIC_ALLOWED_DOMAINS", "")
        return cls(item for item in raw.split(",") if item.strip())

    def run(self) -> None:
        for raw in sys.stdin:
            message: dict[str, Any] = {}
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                response = self._handle(message)
                if response is not None:
                    self._emit(response)
            except Exception as exc:
                if message.get("id") is not None:
                    self._emit(self._error(message["id"], -32603, str(exc)[:300]))

    def _handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, request_id = str(message.get("method") or ""), message.get("id")
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "deskorb-public-fetch", "version": "0.1.0"},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._result(request_id, {"tools": self._tools()})
        if method == "tools/call":
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise PublicFetchError("tools/call parameters must be an object")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise PublicFetchError("tool arguments must be an object")
            result = self._call(str(params.get("name") or ""), arguments)
            return self._result(request_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": not bool(result.get("ok")),
            })
        if request_id is None:
            return None
        return self._error(request_id, -32601, f"Unknown MCP method: {method}")

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "public_fetch_status":
                return {"ok": True, "configured": bool(self.adapter.allowed_domains)}
            if name == "public_fetch":
                if not self.adapter.allowed_domains:
                    raise PublicFetchError("public fetch allowlist is not configured")
                return self.adapter.fetch(str(arguments.get("url") or ""),
                                          max_chars=int(arguments.get("max_chars", MAX_OUTPUT_CHARS)))
            raise PublicFetchError(f"Unknown public fetch tool: {name}")
        except (PublicFetchError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:300]}

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        read_only = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
        return [
            {"name": "public_fetch_status", "description": "Report whether the public HTTPS allowlist is configured.",
             "annotations": read_only,
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "public_fetch", "description": "Fetch one allowlisted public HTTPS page as bounded untrusted text.",
             "annotations": read_only,
             "inputSchema": {"type": "object", "properties": {
                 "url": {"type": "string"},
                 "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_CHARS}},
                 "required": ["url"]}},
        ]

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _emit(message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--domain", action="append", default=[])
    args, _unknown = parser.parse_known_args()
    domains = args.domain or [item for item in os.environ.get("DESKORB_AGENT_PUBLIC_ALLOWED_DOMAINS", "").split(",")
                              if item.strip()]
    PublicFetchMcpServer(domains).run()
