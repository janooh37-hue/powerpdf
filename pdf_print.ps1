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

# fzf talks UTF-8; the console default (e.g. ibm850) garbles non-ASCII names like Arabic
$prevEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try {
    $files = @()
    $pool = @()
    foreach ($p in ($Path ?? @('.'))) {
        $item = Get-Item -LiteralPath $p
        if ($item.PSIsContainer) { $pool += Get-ChildItem -LiteralPath $item.FullName -File -Filter *.pdf | Sort-Object Name }
        elseif ($item.Extension -eq '.pdf') { $files += $item }
        else { throw "Not a PDF file or folder: $p" }
    }
    if ($pool) {
        $picked = $pool | ForEach-Object { Resolve-Path -Relative -LiteralPath $_.FullName } |
            fzf --multi --bind ctrl-a:select-all --prompt 'PDFs> ' --header 'Tab: mark  Ctrl-A: all  Enter: print  Esc: cancel'
        $files += $picked | ForEach-Object { Get-Item -LiteralPath $_ }
    }
    if (-not $files) { Write-Host 'Nothing to print.'; exit 1 }

    $printer = (Get-CimInstance Win32_Printer -Filter 'Default=true').Name
    if ($PickPrinter) {
        # default printer listed first
        $printer = @($printer) + @((Get-Printer).Name | Where-Object { $_ -ne $printer }) | fzf --prompt 'Printer> '
        if (-not $printer) { Write-Host 'No printer picked.'; exit 1 }
    }

    $failed = 0
    foreach ($f in $files) {
        Write-Host "Printing $($f.Name) -> $printer"
        & gswin64c -q -dSAFER -dBATCH -dNOPAUSE -dNoCancel -sDEVICE=mswinpr2 "-sOutputFile=%printer%$printer" $f.FullName
        if ($LASTEXITCODE) { Write-Host "Failed: $($f.FullName)" -ForegroundColor Red; $failed++ }
    }
    exit [int]($failed -gt 0)
} finally {
    [Console]::OutputEncoding = $prevEncoding
}
