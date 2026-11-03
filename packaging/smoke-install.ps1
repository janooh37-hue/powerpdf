param([Parameter(Mandatory)][string] $Version)
$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
$installer = Join-Path $root "dist/powerpdf-$Version-windows-x64-setup.exe"
$install = Join-Path $env:LOCALAPPDATA 'Programs/PowerPDF'
$bin = Join-Path $install 'bin'
$fixture = Join-Path $root 'build/smoke/scan.pdf'
New-Item -ItemType Directory -Force (Split-Path -Parent $fixture), 'build/poison-path' | Out-Null
uv run --frozen python -c "from PIL import Image, ImageDraw; from pathlib import Path; p=Path(r'$fixture'); im=Image.new('RGB',(600,800),'white'); ImageDraw.Draw(im).text((40,40),'PowerPDF release smoke',fill='black'); im.save(p,'PDF',save_all=True,append_images=[im],resolution=100)"
if ($LASTEXITCODE) { throw 'Smoke fixture generation failed' }
foreach ($name in @('gswin64c', 'gs', 'tesseract', 'fzf')) {
    '@exit /b 99' | Set-Content "build/poison-path/$name.cmd" -Encoding ascii
}
function Install([string] $Setup) {
    $process = Start-Process $Setup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER') -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Installer exit code $($process.ExitCode)" }
}
try {
    # Exercise a real lower-version Add/Remove Programs registration, then upgrade it.
    $compiler = "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe"
    & $compiler '/DAppVersion=0.5.99' 'packaging/powerpdf.iss'
    if ($LASTEXITCODE) { throw 'Upgrade rehearsal installer build failed' }
    Install (Join-Path $root 'dist/powerpdf-0.5.99-windows-x64-setup.exe')
    Install $installer
    $entry = Get-ItemProperty 'HKCU:/Software/Microsoft/Windows/CurrentVersion/Uninstall/{2F11D48C-423D-49BC-8251-EC4A987A3454}_is1'
    if ($entry.DisplayVersion -ne $Version) { throw 'Upgrade did not replace Add/Remove Programs version' }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (@($userPath.Split(';') | Where-Object { $_ -eq $bin }).Count -ne 1) { throw 'Installer PATH entry absent or duplicated' }
    Write-Host "SMOKE UPGRADE 0.5.99 -> $($entry.DisplayVersion), one user PATH entry"
    $env:PATH = (Join-Path $root 'build/poison-path') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + $userPath
    # A separate process must find every command using only the refreshed environment.
    & pwsh -NoProfile -File 'packaging/smoke-commands.ps1' -Bin $bin -Fixture $fixture -Version $Version
    if ($LASTEXITCODE) { throw 'Fresh-shell smoke checks failed' }
} finally {
    if (Test-Path "$install/unins000.exe") {
        $process = Start-Process "$install/unins000.exe" -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Uninstaller exit code $($process.ExitCode)" }
        if ([Environment]::GetEnvironmentVariable('Path', 'User').Split(';') -contains $bin) { throw 'Uninstaller left PATH entry behind' }
        if (Test-Path "$bin/powerpdf.exe") { throw 'Uninstaller left executable behind' }
        Write-Host 'SMOKE UNINSTALL user PATH entry and installed executable removed OK'
    }
}
