; Inno Setup script for the packaged Windows build of Slalom Timing.
;
; Not run by hand — build/build.ps1 stages the payload and passes the defines
; below. See build/README.md for the whole pipeline.
;
; Two decisions worth knowing about:
;
;   * PrivilegesRequired=lowest. A club's timekeeping laptop is often used by
;     someone who is not a local administrator, and this installs into
;     %LOCALAPPDATA%\Programs by default. An admin can still choose a machine-wide
;     install from the dialog.
;   * Nothing here writes to the data directory (%LOCALAPPDATA%\SlalomTiming\data)
;     and nothing here deletes it. The database is the event: an upgrade replaces
;     the code, and an uninstall leaves every recorded time where it is.

#define AppName "Slalom Timing"
#define AppPublisher "Slalom Timing"

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "_stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{6A2F1D3C-9B47-4C5E-A1D8-3F0B7E5C21A4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Slalom Timing
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=SlalomTiming-Setup-{#AppVersion}
SetupIconFile={#StageDir}\SlalomTiming.ico
UninstallDisplayIcon={app}\SlalomTiming.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The launcher holds this mutex while the server runs, so Setup asks the operator
; to close a running event instead of overwriting files that are in use.
AppMutex=SlalomTimingRunning

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
de.DataKept=Ihre Daten (Datenbank, Logos, Einstellungen) wurden nicht gelöscht und liegen weiterhin in:%n%n%1%n%nDiesen Ordner können Sie von Hand löschen, wenn Sie die Ergebnisse nicht mehr benötigen.
en.DataKept=Your data (database, logos, settings) has not been deleted. It is still in:%n%n%1%n%nDelete that folder by hand if you no longer need the results.
de.DataFolder=Datenordner (Datenbank & Einstellungen)
en.DataFolder=Data folder (database & settings)
de.LaunchApp=Slalom Timing starten
en.LaunchApp=Start Slalom Timing

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; The shortcut runs the bundled interpreter (renamed, so the taskbar and Task
; Manager say Slalom Timing) against the launcher. No wrapper .exe to build.
Name: "{group}\{#AppName}"; Filename: "{app}\python\SlalomTiming.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\SlalomTiming.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\SlalomTiming.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\SlalomTiming.ico"; Tasks: desktopicon
Name: "{group}\{cm:DataFolder}"; Filename: "{localappdata}\SlalomTiming\data"

[Run]
Filename: "{app}\python\SlalomTiming.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Description: "{cm:LaunchApp}"; Flags: postinstall nowait skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    { SuppressibleMsgBox, not MsgBox: a silent uninstall (/VERYSILENT, or a
      deployment tool) would otherwise hang forever on a dialog nobody can see. }
    SuppressibleMsgBox(FmtMessage(CustomMessage('DataKept'), [ExpandConstant('{localappdata}\SlalomTiming\data')]),
                       mbInformation, MB_OK, IDOK);
end;
