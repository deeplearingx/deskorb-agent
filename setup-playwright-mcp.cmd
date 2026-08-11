@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "MCP_DIR=%~dp0.playwright-mcp"
set "MCP_REPOSITORY=https://github.com/microsoft/playwright-mcp.git"
set "MCP_RELEASE=v0.0.79"
set "MCP_COMMIT=4c5077651542f68525a0b51e97bab2a32abc9290"
set "BROWSER_CACHE=%MCP_DIR%\ms-playwright"
set "NPM_CACHE=%MCP_DIR%\.npm-cache"

where git >nul 2>nul
if errorlevel 1 (
  echo Git is required to install the pinned Playwright MCP release.
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js 18 or newer is required to install Playwright MCP.
  exit /b 1
)
where npm >nul 2>nul
if errorlevel 1 (
  echo npm is required to install Playwright MCP dependencies.
  exit /b 1
)

set "NODE_MAJOR="
for /f "tokens=1 delims=." %%V in ('node --version 2^>nul') do set "NODE_MAJOR=%%V"
set "NODE_MAJOR=%NODE_MAJOR:v=%"
if not defined NODE_MAJOR (
  echo Could not determine the installed Node.js version.
  exit /b 1
)
set /a NODE_MAJOR_NUM=%NODE_MAJOR% >nul 2>nul
if %NODE_MAJOR_NUM% LSS 18 (
  echo Node.js 18 or newer is required. Found: %NODE_MAJOR%
  exit /b 1
)

if exist "%MCP_DIR%\" (
  if not exist "%MCP_DIR%\.git\" (
    echo Refusing to overwrite an existing non-git .playwright-mcp directory.
    exit /b 1
  )
  for /f "delims=" %%S in ('git -C "%MCP_DIR%" status --porcelain 2^>nul') do (
    echo Refusing to update a dirty .playwright-mcp checkout.
    exit /b 1
  )
  git -C "%MCP_DIR%" fetch --tags --force origin
  if errorlevel 1 exit /b 1
) else (
  git clone "%MCP_REPOSITORY%" "%MCP_DIR%"
  if errorlevel 1 exit /b 1
)

git -C "%MCP_DIR%" checkout --detach "!MCP_COMMIT!"
if errorlevel 1 exit /b 1

for /f "delims=" %%H in ('git -C "%MCP_DIR%" rev-parse HEAD 2^>nul') do set "MCP_HEAD=%%H"
if /i not "!MCP_HEAD!"=="!MCP_COMMIT!" (
  echo Pinned Playwright MCP commit verification failed.
  exit /b 1
)

set "PLAYWRIGHT_BROWSERS_PATH=%BROWSER_CACHE%"
call npm ci --omit=dev --cache "%NPM_CACHE%" --prefix "%MCP_DIR%"
if errorlevel 1 exit /b 1
node "%MCP_DIR%\cli.js" install-browser chromium
if errorlevel 1 exit /b 1

if not exist "%MCP_DIR%\cli.js" (
  echo Playwright MCP CLI was not installed.
  exit /b 1
)
if not exist "%MCP_DIR%\node_modules\playwright-core\package.json" (
  echo Playwright MCP dependencies were not installed.
  exit /b 1
)
if not exist "%BROWSER_CACHE%\" (
  echo Playwright browser cache was not installed.
  exit /b 1
)

echo Installed Playwright MCP !MCP_RELEASE! at pinned commit !MCP_COMMIT!.
endlocal
exit /b 0
