; Inno Setup script for Tepo Desktop (built by tools/build_windows.py)
; Name change: the old name was "Tapo Desktop". AppId stays the same on purpose, so setup upgrades the
; old install in place (same folder, same group, same saved data in %APPDATA%\TapoDesktop, the old name is kept there).
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6C1E2F0A-4B7D-4E59-9C51-7A3D2B8E5F10}
AppName=Tepo Desktop
AppVersion={#AppVersion}
AppPublisher=Tepo Desktop (unofficial)
DefaultDirName={localappdata}\Programs\Tepo Desktop
DefaultGroupName=Tepo Desktop
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=TepoDesktop-{#AppVersion}-setup
SetupIconFile={#IconFile}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\TepoDesktop.exe
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start with Windows (minimized to tray)"; Flags: unchecked

[InstallDelete]
; Upgrade from the old name: remove the old exe and the old shortcuts (they would start the old exe).
Type: files; Name: "{app}\TapoDesktop.exe"
Type: files; Name: "{group}\Tapo Desktop.lnk"
Type: files; Name: "{group}\Uninstall Tapo Desktop.lnk"
Type: files; Name: "{userdesktop}\Tapo Desktop.lnk"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Tepo Desktop"; Filename: "{app}\TepoDesktop.exe"
Name: "{group}\Uninstall Tepo Desktop"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Tepo Desktop"; Filename: "{app}\TepoDesktop.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TepoDesktop"; ValueData: """{app}\TepoDesktop.exe"""; Tasks: autostart; Flags: uninsdeletevalue
; The autostart value of the old name points at the old exe: delete it (the line above writes the new one).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "TapoDesktop"; Flags: deletevalue

[Run]
Filename: "{app}\TepoDesktop.exe"; Description: "Launch Tepo Desktop"; Flags: nowait postinstall skipifsilent
