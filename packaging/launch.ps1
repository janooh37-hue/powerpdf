$ErrorActionPreference = 'Stop'
# Agent/session variables should not disable normal terminal rendering.
Remove-Item Env:NO_COLOR, Env:HERDR_ENV, Env:HERDR_PANE_ID, Env:HERDR_SOCKET_PATH -ErrorAction SilentlyContinue
if ($env:TERM -eq 'dumb') { Remove-Item Env:TERM }
$exe = Join-Path $PSScriptRoot 'powerpdf.exe'
$terminal = Get-Command wt.exe -ErrorAction SilentlyContinue
if ($terminal) {
    & $terminal.Source -w new --title PowerPDF $exe
} else {
    & $exe
}
