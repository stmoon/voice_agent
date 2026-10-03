# voice-bridge 한 번에 설치 (Windows): 설치 → 토큰 → 자동 실행 → 커넥터 주소 출력
# 실행: powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

if (-not (Has "py") -and -not (Has "python")) {
    Write-Host "Python 3.11 이상이 필요합니다:  winget install Python.Python.3.12  (설치 뒤 새 PowerShell 창)"; exit 1
}
if (-not (Has "claude")) {
    Write-Host "claude CLI 가 필요합니다: https://claude.com/claude-code  (설치 뒤 claude /login, Git for Windows 필요)"; exit 1
}
if (-not (Has "cloudflared")) {
    Write-Host "cloudflared 설치 중 (winget)..."
    winget install --id Cloudflare.cloudflared -e
    Write-Host "설치했습니다. 새 PowerShell 창에서 이 스크립트를 다시 실행하세요 (PATH 반영)."; exit 0
}

if (Has "py") { py -3 tools\install.py } else { python tools\install.py }
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$vb = Join-Path $env:LOCALAPPDATA "voice-bridge\venv\Scripts\voice-bridge.exe"
& $vb setup
Write-Host ""
Write-Host "다음: 휴대폰에서 부를 세션을 만드세요"
Write-Host '  voice-bridge session new "이름" C:\작업폴더'
