param([string] $RuntimeDestination = 'build/desktop-vendor')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Download([string] $Url, [string] $Destination, [string] $Sha256) {
    Write-Host "Downloading $Url"
    Invoke-WebRequest $Url -OutFile $Destination
    if ($Sha256 -and (Get-FileHash $Destination -Algorithm SHA256).Hash -ne $Sha256) {
        throw "Checksum mismatch: $Url"
    }
}
function Expand-Installer([string] $File, [string] $Destination) {
    # NSIS payload extraction avoids upstream installers' UI/redistributable downloads.
    Write-Host "Extracting $File to $Destination"
    & '7z.exe' x "-o$Destination" -y -bso0 -bsp0 $File
    if ($LASTEXITCODE) { throw "Dependency extraction failed: $File" }
}


$root = (Get-Location).Path
$bin = Join-Path $root $RuntimeDestination
$vendor = Join-Path $bin 'vendor'
$licenses = Join-Path $bin 'licenses'
New-Item -ItemType Directory -Force $vendor, $licenses, 'build/downloads' | Out-Null
$downloads = Join-Path $root 'build/downloads'

# Official Artifex binary and corresponding source (also published alongside the installer).
$gsRelease = 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10080'
Download "$gsRelease/gs10080w64.exe" "$downloads/ghostscript.exe" '52a91b8bf09298788d7a57b9206127026c23eacd75405f0a131e26dc381dce50'
Download "$gsRelease/ghostscript-10.08.0.tar.xz" "$root/dist/ghostscript-10.08.0-source.tar.xz" 'c20492bc8ebb96c87fa2e52a0926e1cda8cde95d66145e018ac713fed5da38cf'
$gsHome = Join-Path $root 'build/ghostscript'
Expand-Installer "$downloads/ghostscript.exe" $gsHome
New-Item -ItemType Directory -Force "$vendor/ghostscript" | Out-Null
foreach ($part in @('bin', 'lib', 'Resource', 'iccprofiles', 'doc')) {
    if (Test-Path "$gsHome/$part") { Copy-Item "$gsHome/$part" "$vendor/ghostscript/$part" -Recurse }
}
Copy-Item "$gsHome/doc/COPYING" "$licenses/Ghostscript-AGPL-3.0.txt"
# Ghostscript's MSVC runtime must be app-local too; installing the VC redist needs admin.
foreach ($dll in @('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'concrt140.dll')) {
    Copy-Item "$env:WINDIR/System32/$dll" "$vendor/ghostscript/bin/$dll"
}

# Chocolatey's pinned, embedded UB Mannheim build, not an unpinned web installer.
Download 'https://community.chocolatey.org/api/v2/package/tesseract/5.5.0.20241111' "$downloads/tesseract.zip" '56659a4c01e6ea75a0b710ba7e8bb16e9cc6675978d2861323751812aeea6183'
Expand-Archive "$downloads/tesseract.zip" "$downloads/tesseract-package" -Force
$tessHome = Join-Path $root 'build/tesseract'
Expand-Installer "$downloads/tesseract-package/tools/tesseract-ocr-w64-setup-5.5.0.20241111.exe" $tessHome
New-Item -ItemType Directory -Force "$vendor/tesseract/tessdata" | Out-Null
Copy-Item "$tessHome/tesseract.exe", "$tessHome/*.dll" "$vendor/tesseract"
Copy-Item "$tessHome/doc" "$vendor/tesseract/doc" -Recurse
Copy-Item "$tessHome/tessdata/configs", "$tessHome/tessdata/tessconfigs", "$tessHome/tessdata/pdf.ttf" "$vendor/tesseract/tessdata" -Recurse
Copy-Item "$tessHome/doc/LICENSE" "$licenses/Tesseract-Apache-2.0.txt"
$models = @{
    eng = '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
    ara = 'e3206d3dc87fd50c24a0fb9f01838615911d25168f4e64415244b67d2bb3e729'
    osd = '9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff'
}
foreach ($language in $models.Keys) {
    Download "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/$language.traineddata" "$vendor/tesseract/tessdata/$language.traineddata" $models[$language]
}
Download 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/LICENSE' "$licenses/Tessdata-Apache-2.0.txt" ''

Download 'https://github.com/junegunn/fzf/releases/download/v0.74.4/fzf-0.74.4-windows_amd64.zip' "$downloads/fzf.zip" '5e63c0e798406fcb9c51a9fed4988398e25fdabf8c670e32c16caf7b4a7ed02d'
Expand-Archive "$downloads/fzf.zip" "$vendor/fzf" -Force
Download 'https://raw.githubusercontent.com/junegunn/fzf/v0.74.4/LICENSE' "$licenses/fzf-MIT.txt" ''
Copy-Item 'packaging/THIRD-PARTY-NOTICES.txt' $bin
