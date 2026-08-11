import socket
import unittest
from unittest.mock import patch

from mcp_security import URLPolicyError, validate_local_path, validate_public_url


class MCPSecurityTests(unittest.TestCase):
    def test_public_url_requires_https_and_exact_allowed_host(self):
        with self.assertRaisesRegex(URLPolicyError, "https"):
            validate_public_url("http://docs.example.test/page", ("example.test",), resolve_dns=False)
        with self.assertRaises(URLPolicyError):
            validate_public_url("https://example.test.evil/page", ("example.test",), resolve_dns=False)
        self.assertEqual(
            validate_public_url("https://docs.example.test/page", ("example.test",), resolve_dns=False).hostname,
            "docs.example.test",
        )

    def test_public_url_rejects_credentials_nonstandard_port_and_private_dns(self):
        for value in (
            "https://user:pass@example.test/page",
            "https://example.test:8443/page",
        ):
            with self.subTest(value=value), self.assertRaises(URLPolicyError):
                validate_public_url(value, ("example.test",), resolve_dns=False)
        with patch("mcp_security.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]):
            with self.assertRaisesRegex(URLPolicyError, "private"):
                validate_public_url("https://example.test/page", ("example.test",), resolve_dns=True)

    def test_local_path_is_confined_to_real_allowed_root(self):
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            inside = allowed / "document.pdf"
            inside.write_bytes(b"pdf")
            self.assertEqual(validate_local_path(inside, (allowed,)), inside.resolve())
            with self.assertRaisesRegex(ValueError, "outside"):
                validate_local_path(root / "outside.pdf", (allowed,))


if __name__ == "__main__":
    unittest.main()
