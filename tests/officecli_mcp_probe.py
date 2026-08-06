"""End-to-end smoke probe for the bundled OfficeCLI MCP server.

Run from the repository root after publishing OfficeCLI::

    .venv\\Scripts\\python.exe tests\\officecli_mcp_probe.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_client import resolve_officecli_binary  # noqa: E402


class ProbeError(RuntimeError):
    pass


class OfficeCliMcpProbe:
    def __init__(self, binary: str):
        self.binary = binary
        self.process: subprocess.Popen[str] | None = None
        self.stdout_lines: list[str] = []
        self.next_id = 1

    def run(self) -> None:
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            [self.binary, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        try:
            self.request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "deskorb-officecli-probe", "version": "1"},
            })
            self.notify("notifications/initialized", {})
            tools = self.request("tools/list", {}).get("tools", [])
            if len(tools) != 1 or tools[0].get("name") != "officecli":
                raise ProbeError(f"Expected exactly one officecli tool, got {tools!r}")

            with tempfile.TemporaryDirectory(prefix="deskorb officecli probe ") as directory:
                temp_dir = Path(directory)
                self._probe_docx(temp_dir / "probe.docx")
                self._probe_xlsx(temp_dir / "probe.xlsx")
                self._probe_pptx(temp_dir / "probe.pptx")
        finally:
            self.close()

    def _probe_docx(self, path: Path) -> None:
        self.call(["create", str(path)])
        self.call(["add", str(path), "/body", "--type", "paragraph",
                   "--prop", "text=DeskOrb OfficeCLI probe"])
        self.call(["validate", str(path)])
        result = self.call(["view", str(path), "text"])
        if "DeskOrb OfficeCLI probe" not in self.text(result):
            raise ProbeError("DOCX text read did not contain the probe paragraph")

    def _probe_xlsx(self, path: Path) -> None:
        self.call(["create", str(path)])
        self.call(["set", str(path), "/Sheet1/A1", "--prop", "value=DeskOrb"])
        self.call(["validate", str(path)])
        result = self.call(["get", str(path), "/Sheet1/A1", "--json"])
        if "DeskOrb" not in self.text(result):
            raise ProbeError("XLSX cell read did not contain the probe value")

    def _probe_pptx(self, path: Path) -> None:
        self.call(["create", str(path)])
        self.call(["add", str(path), "/", "--type", "slide", "--prop", "title=DeskOrb"])
        self.call(["validate", str(path)])
        outline = self.call(["view", str(path), "outline"])
        if "DeskOrb" not in self.text(outline):
            raise ProbeError("PPTX outline read did not contain the slide title")
        screenshot = self.call(["view", str(path), "screenshot", "--page", "1"])
        images = [item for item in screenshot.get("content", [])
                  if isinstance(item, dict) and item.get("type") == "image"]
        if not any(item.get("mimeType") == "image/png" for item in images):
            raise ProbeError("PPTX screenshot did not return an image/png content block")

    def call(self, command: list[str]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": "officecli", "arguments": {"command": command}})
        if result.get("isError"):
            raise ProbeError(f"OfficeCLI command failed: {command!r}: {self.text(result)}")
        return result

    @staticmethod
    def text(result: dict[str, Any]) -> str:
        parts = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise ProbeError("OfficeCLI MCP process is not running")
        request_id = self.next_id
        self.next_id += 1
        self.process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if line == "":
                raise ProbeError(f"OfficeCLI MCP stopped while handling {method}")
            self.stdout_lines.append(line)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeError(f"OfficeCLI wrote non-JSON stdout: {line!r}") from exc
            if not isinstance(message, dict):
                raise ProbeError(f"OfficeCLI wrote a non-object JSON message: {message!r}")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise ProbeError(f"MCP {method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProbeError(f"MCP {method} returned a non-object result: {result!r}")
            return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise ProbeError("OfficeCLI MCP process is not running")
        self.process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params,
        }, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def close(self) -> None:
        process, self.process = self.process, None
        if not process:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout:
            remainder = process.stdout.read()
            if remainder:
                self.stdout_lines.extend(remainder.splitlines(keepends=True))
        for line in self.stdout_lines:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProbeError(f"OfficeCLI stdout contained protocol garbage: {line!r}") from exc
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ProbeError(f"OfficeCLI stdout contained an invalid protocol message: {message!r}")


def main() -> int:
    binary = resolve_officecli_binary()
    if not binary:
        raise SystemExit("OfficeCLI binary not found. Run .\\build_officecli.ps1 first.")
    OfficeCliMcpProbe(binary).run()
    print("OfficeCLI MCP probe passed: DOCX, XLSX, PPTX, validation, reads, and PPTX screenshot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
