"""Shared, fail-closed security checks for local MCP capabilities.

The agent receives URLs and file paths from model output.  These helpers keep
those values at the adapter boundary instead of relying on prompt wording or
on a third-party MCP server's own policy.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable, Iterable


class URLPolicyError(ValueError):
    """A URL does not satisfy the local public-read policy."""


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    hostname: str
    port: int


def _canonical_host(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if not host or "*" in host or any(character.isspace() for character in host):
        raise URLPolicyError("invalid host")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLPolicyError("invalid host") from exc


def _canonical_domains(domains: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in domains:
        try:
            domain = _canonical_host(value)
        except URLPolicyError:
            continue
        if domain not in result:
            result.append(domain)
    return tuple(result)


def _domain_allowed(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _resolve_addresses(host: str, resolver: Callable[..., list[tuple]] | None = None) -> set[str]:
    lookup = resolver or socket.getaddrinfo
    try:
        records = lookup(host, 443, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise URLPolicyError("DNS resolution failed") from exc
    addresses: set[str] = set()
    for record in records or ():
        try:
            sockaddr = record[4]
            addresses.add(str(sockaddr[0]))
        except (IndexError, TypeError, KeyError):
            continue
    if not addresses:
        raise URLPolicyError("DNS resolution returned no address")
    return addresses


def _ensure_public_addresses(addresses: Iterable[str]) -> None:
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
            mapped = getattr(address, "ipv4_mapped", None)
            if mapped is not None:
                address = mapped
        except ValueError as exc:
            raise URLPolicyError("DNS returned an invalid address") from exc
        # ``is_global`` is deliberately stricter than merely checking RFC1918:
        # loopback, link-local, multicast, reserved and unspecified addresses
        # must all fail closed for a public-only fetcher.
        if not address.is_global:
            raise URLPolicyError("DNS resolved to a private or non-public address")


def validate_public_url(
    value: str,
    allowed_domains: Iterable[str],
    *,
    resolve_dns: bool = True,
    resolver: Callable[..., list[tuple]] | None = None,
) -> ValidatedURL:
    """Validate one HTTPS URL against an exact host allowlist.

    Subdomains of an explicitly allowlisted domain are allowed.  IP literals,
    credentials, fragments with control characters, and non-default ports are
    rejected.  When ``resolve_dns`` is true every resolved address must be
    globally routable; callers should repeat this check after redirects.
    """
    raw = str(value or "").strip()
    if not raw or len(raw) > 4096 or any(ord(character) < 0x20 for character in raw):
        raise URLPolicyError("invalid URL")
    try:
        parsed = urlsplit(raw)
        host = _canonical_host(parsed.hostname or "")
        port = parsed.port
    except (ValueError, UnicodeError) as exc:
        raise URLPolicyError("invalid URL") from exc
    if parsed.scheme.casefold() != "https":
        raise URLPolicyError("https is required")
    if parsed.username is not None or parsed.password is not None:
        raise URLPolicyError("URL credentials are not allowed")
    if port not in (None, 443):
        raise URLPolicyError("non-default port is not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise URLPolicyError("IP literals are not allowed")
    domains = _canonical_domains(allowed_domains)
    if not domains or not _domain_allowed(host, domains):
        raise URLPolicyError("host is outside the HTTPS allowlist")
    if resolve_dns:
        _ensure_public_addresses(_resolve_addresses(host, resolver))
    return ValidatedURL(raw, host, 443)


def _within(candidate: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(candidate), str(root))).casefold() == str(root).casefold()
    except (OSError, ValueError):
        return False


def validate_local_path(
    value: str | Path,
    allowed_roots: Iterable[str | Path],
    *,
    must_exist: bool = True,
    must_be_file: bool = True,
) -> Path:
    """Resolve a local path and require it to stay within an approved root."""
    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        raise ValueError("invalid local path")
    roots = [Path(root).expanduser().resolve(strict=False) for root in allowed_roots]
    if not roots:
        raise ValueError("no local path roots are configured")
    supplied = Path(raw).expanduser()
    candidates = [supplied] if supplied.is_absolute() else [root / supplied for root in roots]
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if not any(_within(resolved, root) for root in roots):
            continue
        if must_exist and not resolved.exists():
            raise ValueError("local path does not exist")
        if must_be_file and resolved.exists() and not resolved.is_file():
            raise ValueError("local path is not a file")
        return resolved
    raise ValueError("local path is outside the approved roots")
