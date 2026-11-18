# One-shot developer setup on a fresh Windows machine (see AGENTS.md section 2).
# Usage: powershell -ExecutionPolicy Bypass -File tools/setup_dev.ps1 [-Python py]
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$ver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.12") { throw "Python >= 3.12 required (found $ver)" }
if (-not (Test-Path .venv)) { & $Python -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip -q
& .venv\Scripts\python.exe -m pip install -e ".[dev]" -q
git config core.autocrlf false

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpeg) { "ffmpeg: $($ffmpeg.Source)" } else { Write-Warning "ffmpeg not on PATH: winget install Gyan.FFmpeg" }
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ }
if ($iscc) { "Inno Setup: $($iscc[0])" } else { "Inno Setup not found (only needed for setup.exe): winget install JRSoftware.InnoSetup --scope user" }

$env:QT_QPA_PLATFORM = "offscreen"
& .venv\Scripts\python.exe tools\specs_tool.py verify
& .venv\Scripts\python.exe -m tapo_desktop --selftest
if ($LASTEXITCODE -eq 0) { "Setup OK. Run the tests: .venv\Scripts\python -m pytest tests -q -m `"not soak and not hardware`"" }
