; CI-only installer. Deliberately distinct AppId/name: never upgrades PowerPDF.
[Setup]
AppId={{6A7CC15C-BF5C-4E86-A22B-5A9E2E750FC7}
AppName=PowerPDF Packaging Smoke (CI only)
AppVersion=0.7.0
AppPublisher=PowerPDF
DefaultDirName={localappdata}\Programs\PowerPDF-Packaging-Smoke
DefaultGroupName=PowerPDF Packaging Smoke (CI only)
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=powerpdf-packaging-smoke-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\PowerPDF-Packaging-Smoke.exe

[Files]
Source: "..\dist\PowerPDF-Packaging-Smoke\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PowerPDF Packaging Smoke (CI only)"; Filename: "{app}\PowerPDF-Packaging-Smoke.exe"
