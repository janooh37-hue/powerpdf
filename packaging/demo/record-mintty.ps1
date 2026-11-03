# Run from an interactive Windows GitHub-hosted runner after uv sync and ffmpeg installation.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$output = Join-Path $repo 'demo-output'
New-Item -ItemType Directory -Force -Path $output | Out-Null
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    'Missing .venv\Scripts\python.exe: run uv sync before the recording step.' | Set-Content (Join-Path $output 'bootstrap-error.txt')
    throw 'The project Windows venv is required.'
}
# Python owns the desktop, timing, native input, ffmpeg processes, and unconditional evidence capture.
& $python (Join-Path $PSScriptRoot 'desktop.py') record --repo $repo --output $output
if ($LASTEXITCODE -ne 0) { throw "Desktop recording failed (exit $LASTEXITCODE); inspect demo-output metadata, screenshots, and logs." }
