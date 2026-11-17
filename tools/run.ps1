# Task runner for the verification gates. Usage: powershell -File tools/run.ps1 <task>
param([Parameter(Mandatory = $true)][string]$Task, [string]$Ip = $env:TAPO_HW_IP)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
Set-Location $Root
$env:QT_QPA_PLATFORM = 'offscreen'

switch ($Task) {
  'verify-specs'  { & $Py tools/specs_tool.py verify; exit $LASTEXITCODE }
  'test-tdd'      { & $Py -m pytest tests -m "not soak and not hardware" -q -p no:cacheprovider --timeout 120; exit $LASTEXITCODE }
  'test-soak'     { if (-not $env:TAPO_SOAK_SECONDS) { $env:TAPO_SOAK_SECONDS = '1800' }
                    & $Py -m pytest tests/soak -m soak -q -s -p no:cacheprovider --timeout 2400; exit $LASTEXITCODE }
  'test-hardware' { if (-not $Ip) { Write-Error 'set TAPO_HW_IP (and TAPO_HW_CLOUD_PASSWORD, TAPO_HW_CAM_USER, TAPO_HW_CAM_PASSWORD) or pass -Ip' }
                    $env:TAPO_HW_IP = $Ip
                    & $Py -m pytest tests -m hardware -q -s -p no:cacheprovider; exit $LASTEXITCODE }
  'smoke'         { & $Py tools/smoke_e2e.py; exit $LASTEXITCODE }
  'build-windows' { & $Py tools/build_windows.py; exit $LASTEXITCODE }
  'audit-release' { & $Py tools/audit_release.py dist/TapoDesktop --json artifacts/release_audit.json; exit $LASTEXITCODE }
  default         { Write-Error "unknown task $Task" }
}
