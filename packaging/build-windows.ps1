param([Parameter(Mandatory)][string] $Version)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Download([string] $Url, [string] $Destination, [string] $Sha256) {
    Invoke-WebRequest $Url -OutFile $Destination
    if ($Sha256 -and (Get-FileHash $Destination -Algorithm SHA256).Hash -ne $Sha256) {
        throw "Checksum mismatch: $Url"
    }
}
function Run-Installer([string] $File, [string] $Destination) {
    $process = Start-Process -FilePath $File -ArgumentList @('/S', "/D=$Destination") -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Dependency installation failed: $File ($($process.ExitCode))" }
}

$printScript = (Resolve-Path 'pdf_print.ps1').Path
uv run --frozen --with pyinstaller==6.17.0 pyinstaller powerpdf_cli.py --onedir --name powerpdf --console --noconfirm --clean `
    --specpath build --workpath build --distpath dist --collect-submodules textual --collect-data textual `
    --hidden-import powerpdf --hidden-import pdf_merge --hidden-import pdf_split --hidden-import pdf_card `
    --hidden-import pdf_compress --hidden-import pdf_image --hidden-import pdf_runtime --copy-metadata powerpdf `
    --add-data "${printScript};."
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

$root = (Get-Location).Path
$bin = Join-Path $root 'dist/powerpdf'
$vendor = Join-Path $bin 'vendor'
$licenses = Join-Path $bin 'licenses'
New-Item -ItemType Directory -Force $vendor, $licenses, 'build/downloads' | Out-Null
$downloads = Join-Path $root 'build/downloads'

# Official Artifex binary and corresponding source (also published alongside the installer).
$gsRelease = 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10060'
Download "$gsRelease/gs10060w64.exe" "$downloads/ghostscript.exe" '8d552205c0fe87a16bac2f377c8a1b090cfcbc610db7c281bd6a646b39c9c468'
Download "$gsRelease/ghostscript-10.06.0.tar.xz" "$root/dist/ghostscript-10.06.0-source.tar.xz" '64352648c2c081c8a9fb1a12dc1965e01ead7c57f58b72d1b54f6ef1cef3c561'
$gsHome = Join-Path $root 'build/ghostscript'
Run-Installer "$downloads/ghostscript.exe" $gsHome
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
Run-Installer "$downloads/tesseract-package/tools/tesseract-ocr-w64-setup-5.5.0.20241111.exe" $tessHome
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

Download 'https://github.com/junegunn/fzf/releases/download/v0.67.0/fzf-0.67.0-windows_amd64.zip' "$downloads/fzf.zip" 'cad95326ed416d14226a03425513b049f5bc9a41f30a355e5fe204a29e666867'
Expand-Archive "$downloads/fzf.zip" "$vendor/fzf" -Force
Download 'https://raw.githubusercontent.com/junegunn/fzf/v0.67.0/LICENSE' "$licenses/fzf-MIT.txt" ''
Copy-Item 'packaging/THIRD-PARTY-NOTICES.txt' $bin
Copy-Item 'packaging/launch.ps1' $bin
foreach ($command in @('split', 'merge', 'compress', 'image', 'print')) {
    "@echo off`r`n`"%~dp0powerpdf.exe`" --cli $command %*`r`nexit /b %errorlevel%`r`n" |
        Set-Content "$bin/pdf$command.cmd" -Encoding ascii
}

# Source URLs and exact package versions stay available even when installed offline.
$compiler = "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe"
if (-not (Test-Path $compiler)) {
    choco install innosetup --yes --no-progress
    if ($LASTEXITCODE) { throw 'Inno Setup installation failed' }
}
& $compiler "/DAppVersion=$Version" 'packaging/powerpdf.iss'
if ($LASTEXITCODE) { throw 'Inno Setup failed' }
Compress-Archive -Path $bin -DestinationPath "dist/powerpdf-$Version-windows-x64.zip" -Force
