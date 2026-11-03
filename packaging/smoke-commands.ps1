param([Parameter(Mandatory)][string] $Bin, [Parameter(Mandatory)][string] $Fixture, [Parameter(Mandatory)][string] $Version)
$ErrorActionPreference = 'Stop'
function Assert-Exit([string] $Label) {
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}
$resolved = (Get-Command powerpdf -CommandType Application).Source
if ($resolved -ne (Join-Path $Bin 'powerpdf.exe')) { throw "PATH resolved wrong powerpdf: $resolved" }
Write-Host "SMOKE PATH powerpdf=$resolved"
$actual = powerpdf --version
Assert-Exit 'powerpdf --version'
if ($actual -ne "powerpdf $Version") { throw "Wrong version: $actual" }
Write-Host "SMOKE VERSION $actual"
$tools = powerpdf --runtime-info | ConvertFrom-Json
Assert-Exit 'runtime resolution'
foreach ($tool in @('ghostscript', 'tesseract', 'fzf')) {
    if (-not $tools.$tool.StartsWith("$Bin\vendor\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "$tool did not resolve to the bundle: $($tools.$tool)"
    }
    Write-Host "SMOKE BUNDLED $tool=$($tools.$tool)"
}
& $tools.ghostscript --version
Assert-Exit 'bundled Ghostscript'
& $tools.tesseract --version
Assert-Exit 'bundled Tesseract'
$languages = & $tools.tesseract --tessdata-dir "$Bin/vendor/tesseract/tessdata" --list-langs
Assert-Exit 'Tesseract languages'
foreach ($language in @('eng', 'ara', 'osd')) {
    if ($languages -notcontains $language) { throw "Missing Tesseract language: $language" }
}
Write-Host 'SMOKE OCR app resolver and eng+ara+osd OK'
& $tools.fzf --version
Assert-Exit 'bundled fzf'

pdfcompress $Fixture
Assert-Exit 'pdfcompress'
$folder = Split-Path -Parent $Fixture
foreach ($command in @('image', 'split')) {
    & "pdf$command" $Fixture
    Assert-Exit "pdf$command"
}
pdfmerge "$folder/scan_pages/scan_page_001.pdf" "$folder/scan_pages/scan_page_002.pdf" -o "$folder/merged.pdf"
Assert-Exit 'pdfmerge'
pdfprint -h
Assert-Exit 'pdfprint help (no printer jobs)'
foreach ($path in @('scan_small.pdf', 'scan_images/scan_page_001.jpg', 'scan_pages/scan_page_002.pdf', 'merged.pdf')) {
    if (-not (Test-Path "$folder/$path")) { throw "Missing output: $path" }
}
Write-Host 'SMOKE CLIS pdfcompress=_small.pdf pdfimage=JPG pdfsplit=2-pages pdfmerge=merged.pdf pdfprint=help OK'
