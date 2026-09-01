; Inno Setup script for SAP BASIS Monitor.
; Download Inno Setup (free) from jrsoftware.org/isinfo.php, then open this
; file in the Inno Setup Compiler and click Build -- or run from command line:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
;
; PrivilegesRequired=lowest means this installs into the CURRENT USER's
; profile (%LOCALAPPDATA%) and needs NO admin rights -- works on
; restricted machines. The optional "launch at startup" checkbox uses the
; per-user Startup folder (not Task Scheduler), which also needs no admin.

#define MyAppName "SAP BASIS Monitor"
#define MyAppVersion "1.0.0"
#define MyAppExeName "SAP_BASIS_MONITOR.exe"

[Setup]
AppId={{8F2C9B1A-6E4D-4A3B-9C7F-1D5E8A2B4C6F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\SAP_BASIS_MONITOR
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\dist_installer
OutputBaseFilename=SAP_BASIS_MONITOR_Setup
Compression=lzma
SolidCompression=yes
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startupicon"; Description: "Launch {#MyAppName} automatically when Windows starts"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
; Everything staged by packaging\build.bat into dist\ -- the exe plus its
; loose config templates and dashboard static assets.
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\dashboard\static\*"; DestDir: "{app}\dashboard\static"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; Per-user Startup folder shortcut -- no admin/Task Scheduler needed.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up runtime-generated folders on uninstall. Config/credentials are
; included here too -- if you want to preserve them across reinstalls,
; remove the config.yaml/.env lines below.
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\reports"
Type: filesandordirs; Name: "{app}\dashboard\snapshots"
