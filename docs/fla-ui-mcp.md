# FlaUI-MCP desktop backend

DeskOrb can use FlaUI-MCP as an internal Windows UI Automation backend. The
model still receives only `desktop_uia_observe`, `desktop_uia_invoke`, and
`desktop_uia_set_value`; raw `windows_*` tools, screenshots, batches, close
operations, and coordinates are not exposed as model tools.

The integration is pinned to the upstream `v0.2.0` release at commit
`08301c31b301994c612ef5c6874c61f5d9578ea0`. Download the matching
self-contained Windows archive from the [FlaUI-MCP v0.2.0 release](https://github.com/shanselman/FlaUI-MCP/releases/tag/v0.2.0), verify the downloaded archive in your deployment process, and extract it to a machine-local directory. The self-contained x64 archive avoids an additional .NET runtime dependency.

Copy `mcp.servers.fla-ui.example.json`, change the executable path, set
`"enabled": true`, and set:

```powershell
$env:DESKORB_AGENT_MCP_CONFIG = 'D:\path\to\mcp.servers.fla-ui.json'
$env:DESKORB_AGENT_FLAUI_MCP_SERVER = 'windows'
```

For the current local deployment, the checked-in example is enabled and points
to the installed self-contained x64 executable at
`D:\tools\deskorb\fla-ui-mcp\v0.2.0\FlaUI.Mcp.exe`. On another machine, change
the command path or set `enabled` to `false`. With an explicit but unavailable
server DeskOrb fails closed as `desktop_backend_unavailable` rather than
switching to coordinates.

FlaUI-MCP needs the current interactive Windows desktop. UAC, the secure
desktop, integrity-level mismatches, modal dialogs, and inaccessible UWP
controls remain safety boundaries. They are reported as blocked or unavailable
and are not bypassed.
