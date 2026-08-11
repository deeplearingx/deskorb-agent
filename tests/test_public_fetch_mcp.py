import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from public_fetch_mcp import PublicFetchAdapter, PublicFetchError, PublicFetchMcpServer


class _Response:
    status = 200
    url = "https://docs.example.test/page"
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, _size=-1):
        return b"<html><body><h1>DeskOrb</h1><p>Read only page.</p></body></html>"


class PublicFetchMcpTests(unittest.TestCase):
    def test_fetch_returns_bounded_text_without_raw_url(self):
        class _Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return _Response()

        with patch("public_fetch_mcp.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]), patch("public_fetch_mcp.urllib.request.build_opener", return_value=_Opener()):
            result = PublicFetchAdapter(("example.test",)).fetch("https://docs.example.test/page")

        self.assertTrue(result["ok"])
        self.assertEqual(result["host"], "docs.example.test")
        self.assertIn("DeskOrb", result["text"])
        self.assertNotIn("url", result)

    def test_fetch_rejects_non_https_and_private_dns_before_network(self):
        opener = Mock()
        adapter = PublicFetchAdapter(("example.test",))
        with self.assertRaisesRegex(PublicFetchError, "https"):
            adapter.fetch("http://example.test/page")
        with patch("public_fetch_mcp.urllib.request.urlopen", opener), \
             patch("public_fetch_mcp.socket.getaddrinfo", return_value=[
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
             ]):
            with self.assertRaisesRegex(PublicFetchError, "private"):
                adapter.fetch("https://example.test/page")
        opener.assert_not_called()

    def test_redirect_target_is_revalidated(self):
        adapter = PublicFetchAdapter(("example.test",))
        with self.assertRaises(PublicFetchError):
            adapter.validate_url("https://other.example/page", resolve_dns=False)

    def test_mcp_server_exposes_only_read_only_fetch_tools(self):
        tools = PublicFetchMcpServer._tools()
        self.assertEqual([item["name"] for item in tools], ["public_fetch_status", "public_fetch"])
        self.assertTrue(all(item["annotations"]["readOnlyHint"] for item in tools))


if __name__ == "__main__":
    unittest.main()
