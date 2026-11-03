#ifndef AppVersion
  #error AppVersion must be supplied by the release build
#endif

[Setup]
AppId={{2F11D48C-423D-49BC-8251-EC4A987A3454}
AppName=PowerPDF
AppVersion={#AppVersion}
AppPublisher=PowerPDF
AppPublisherURL=https://github.com/janooh37-hue/powerpdf
DefaultDirName={autopf}\PowerPDF
DefaultGroupName=PowerPDF
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=powerpdf-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
UninstallDisplayIcon={app}\bin\powerpdf.exe
CloseApplications=yes

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\powerpdf\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PowerPDF"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\launch.ps1"""; WorkingDir: "{userdocs}"; IconFilename: "{app}\bin\powerpdf.exe"
Name: "{autodesktop}\PowerPDF"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\launch.ps1"""; WorkingDir: "{userdocs}"; IconFilename: "{app}\bin\powerpdf.exe"; Tasks: desktopicon

[Code]
function EnvironmentRoot: Integer;
begin
  if IsAdminInstallMode then Result := HKLM else Result := HKCU;
end;

function EnvironmentKey: String;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

function WithoutBin(Value: String): String;
var
  Item: String;
  Separator: Integer;
begin
  Result := '';
  while Value <> '' do begin
    Separator := Pos(';', Value);
    if Separator = 0 then Separator := Length(Value) + 1;
    Item := Copy(Value, 1, Separator - 1);
    Delete(Value, 1, Separator);
    if (Item <> '') and (CompareText(Item, ExpandConstant('{app}\bin')) <> 0) then begin
      if Result <> '' then Result := Result + ';';
      Result := Result + Item;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Value: String;
begin
  if CurStep = ssPostInstall then begin
    RegQueryStringValue(EnvironmentRoot, EnvironmentKey, 'Path', Value);
    Value := WithoutBin(Value);
    if Value <> '' then Value := Value + ';';
    if not RegWriteExpandStringValue(EnvironmentRoot, EnvironmentKey, 'Path',
      Value + ExpandConstant('{app}\bin')) then
      RaiseException('Unable to add PowerPDF to PATH.');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Value: String;
begin
  if CurUninstallStep = usPostUninstall then begin
    if RegQueryStringValue(EnvironmentRoot, EnvironmentKey, 'Path', Value) then
      RegWriteExpandStringValue(EnvironmentRoot, EnvironmentKey, 'Path', WithoutBin(Value));
  end;
end;
