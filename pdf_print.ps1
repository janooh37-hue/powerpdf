[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $Path,
    [Alias('p')][switch] $PickPrinter,
    [Alias('h')][switch] $Help
)
$ErrorActionPreference = 'Stop'

if ($Help) {
    @'
usage: pdfPrint [paths ...] [-p] [-h]

Print one or more PDFs (via Ghostscript). PDF files given by name print straight away.
With no path, or for a folder, pick PDFs in a list:
  Tab = mark, Ctrl-A = mark all, Enter = print, Esc = cancel

positional arguments:
  paths       PDF files and/or folders (default: current folder)

options:
  -p          pick the printer from a list (default printer listed first);
              without -p, prints to the Windows default printer
  -h          show this help and exit

examples:
  pdfPrint                 pick PDFs from this folder, print to the default printer
  pdfPrint a.pdf b.pdf     print a.pdf and b.pdf, no picker
  pdfPrint .\scans         pick PDFs from .\scans
  pdfPrint -p              pick PDFs, then pick the printer
'@
    exit 0
}

# Match Python's resolution order: shipped runtime, PATH, conventional installs.
function Find-PowerPDFTool([string] $Name, [string] $Relative, [string[]] $Known) {
    foreach ($root in @($PSScriptRoot, (Split-Path -Parent $PSScriptRoot))) {
        $candidate = Join-Path $root "vendor\$Relative"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    foreach ($candidate in $Known) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw "$Name not found. Install the bundled PowerPDF Windows release."
}
$knownGS = @(Get-ChildItem "$env:ProgramFiles\gs\gs*\bin\gswin64c.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
$gs = Find-PowerPDFTool 'gswin64c' 'ghostscript\bin\gswin64c.exe' $knownGS
if ($gs -like '*\vendor\ghostscript\bin\gswin64c.exe') {
    $gsHome = Split-Path -Parent (Split-Path -Parent $gs)
    $env:GS_LIB = "$gsHome\lib;$gsHome\Resource\Init;$gsHome\Resource\Font"
}

# fzf talks UTF-8; the console default (e.g. ibm850) garbles non-ASCII names like Arabic
$prevEncoding = [Console]::OutputEncoding
$prevPipeEncoding = $OutputEncoding
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
try {
    $files = @()
    $pool = @()
    $inputs = if ($Path) { $Path } else { @('.') }
    foreach ($p in $inputs) {
        $item = Get-Item -LiteralPath $p
        if ($item.PSIsContainer) { $pool += Get-ChildItem -LiteralPath $item.FullName -File -Filter *.pdf | Sort-Object Name }
        elseif ($item.Extension -eq '.pdf') { $files += $item }
        else { throw "Not a PDF file or folder: $p" }
    }
    if ($pool) {
        $fzf = Find-PowerPDFTool 'fzf' 'fzf\fzf.exe' @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\fzf.exe")
        $picked = $pool | ForEach-Object { Resolve-Path -Relative -LiteralPath $_.FullName } |
            & $fzf --multi --bind ctrl-a:select-all --prompt 'PDFs> ' --header 'Tab: mark  Ctrl-A: all  Enter: print  Esc: cancel'
        $files += $picked | ForEach-Object { Get-Item -LiteralPath $_ }
    }
    if (-not $files) { Write-Host 'Nothing to print.'; exit 1 }

    $printer = (Get-CimInstance Win32_Printer -Filter 'Default=true').Name
    if ($PickPrinter) {
        $fzf = Find-PowerPDFTool 'fzf' 'fzf\fzf.exe' @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\fzf.exe")
        # default printer listed first
        $printer = @($printer) + @((Get-Printer).Name | Where-Object { $_ -ne $printer }) | & $fzf --prompt 'Printer> '
        if (-not $printer) { Write-Host 'No printer picked.'; exit 1 }
    }

    $failed = 0
    foreach ($f in $files) {
        Write-Host "Printing $($f.Name) -> $printer"
        & $gs -q -dSAFER -dBATCH -dNOPAUSE -dNoCancel -sDEVICE=mswinpr2 "-sOutputFile=%printer%$printer" $f.FullName
        if ($LASTEXITCODE) { Write-Host "Failed: $($f.FullName)" -ForegroundColor Red; $failed++ }
    }
    exit [int]($failed -gt 0)
} finally {
    [Console]::OutputEncoding = $prevEncoding
    $OutputEncoding = $prevPipeEncoding
}
