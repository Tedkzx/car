; Inno Setup script: one per-user installer, no admin rights (spec section 11).
; Build:  iscc installer\setup.iss /DMyAppVersion=0.1.0
#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif
#define MyAppName "SX200 Tracker"
#define MyAppExe "SX200Tracker.exe"

[Setup]
AppId={{42ED17D7-97C3-40F8-882F-61453DCF447D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=SX200 Tracker
DefaultDirName={localappdata}\Programs\SX200 Tracker
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=SX200-Tracker-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayIcon={app}\{#MyAppExe}
CloseApplications=force

[Files]
Source: "..\dist\SX200Tracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{userprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; Start at Windows login: per-user Run key, no admin rights needed.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SX200Tracker"; ValueData: """{app}\{#MyAppExe}"" --background"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Start SX200 Tracker"; Flags: nowait postinstall skipifsilent
; An automatic update runs silently with /update=1 and restarts the tracker afterwards.
Filename: "{app}\{#MyAppExe}"; Parameters: "--background"; Flags: nowait; Check: IsAutoUpdate

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExe}"; Flags: runhidden; RunOnceId: "StopTracker"

[Code]
function IsAutoUpdate: Boolean;
begin
  Result := ExpandConstant('{param:update|0}') = '1';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\SX200Tracker');
    if DirExists(DataDir) then
      if MsgBox('Keep your saved cars, notes and settings?' + #13#10 + #13#10 +
                'Choose Yes if you might install SX200 Tracker again.', mbConfirmation, MB_YESNO) = IDNO then
        DelTree(DataDir, True, True, True);
  end;
end;
