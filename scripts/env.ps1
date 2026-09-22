# scripts/env.ps1 - Environment activator for iMedNet Toolkit
# Sets UTF-8 encoding, ensures uv is in PATH, and activates virtualenv if present.

$ErrorActionPreference = "Stop"

# 1. Enforce UTF-8 encoding across Python and PowerShell session
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8

Write-Host "✓ Configured UTF-8 session encoding (PYTHONUTF8=1)" -ForegroundColor Green

# 2. Check and locate uv in PATH or common directories
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $searchPaths = @(
        "$env:APPDATA\Python\Python*\Scripts",
        "$env:USERPROFILE\.cargo\bin",
        "$env:USERPROFILE\.local\bin",
        "C:\Python*\Scripts"
    )
    $found = $false
    foreach ($pattern in $searchPaths) {
        $matches = Get-ChildItem -Path $pattern -Filter "uv.exe" -ErrorAction SilentlyContinue
        if ($matches) {
            $uvDir = $matches[0].DirectoryName
            $env:PATH = "$uvDir;$env:PATH"
            Write-Host "✓ Located uv and prepended to PATH: $uvDir" -ForegroundColor Green
            $found = $true
            break
        }
    }
    if (-not $found) {
        Write-Warning "uv not found on PATH. Run: pip install --user uv"
    }
} else {
    Write-Host "✓ uv is ready on PATH" -ForegroundColor Green
}

# 3. Activate virtual environment if present
$venvActivate = Join-Path $PSScriptRoot "..\.venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
    Write-Host "✓ Activated .venv" -ForegroundColor Green
} else {
    Write-Host "i Monorepo venv not yet created. Run: uv sync --extra dev --extra docs" -ForegroundColor Cyan
}
