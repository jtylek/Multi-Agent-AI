# Build a self-contained Windows installer, following ScreenCapture packaging.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
python -m pip install --upgrade pyinstaller .
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
python -m PyInstaller --noconfirm --clean --onedir --console --name Multi-Agent-AI --collect-submodules multi_agent_ai packaging\launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidate = "$([Environment]::GetFolderPath('ProgramFilesX86'))\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) { $iscc = $candidate }
}
if ($iscc) {
    $isccPath = if ($iscc -is [System.Management.Automation.CommandInfo]) { $iscc.Source } else { [string]$iscc }
    & $isccPath "/DAppVersion=$version" packaging\windows.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Installer: dist\Multi-Agent-AI-Setup.exe"
} else {
    Write-Warning "Inno Setup not found; portable dist\Multi-Agent-AI folder built."
}
