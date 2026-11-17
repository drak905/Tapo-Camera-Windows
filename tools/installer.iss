; Inno Setup script for Tapo Desktop (built by tools/build_windows.py)
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6C1E2F0A-4B7D-4E59-9C51-7A3D2B8E5F10}
AppName=Tapo Desktop
AppVersion={#AppVersion}
AppPublisher=Tapo Desktop (unofficial)
DefaultDirName={localappdata}\Programs\Tapo Desktop
DefaultGroupName=Tapo Desktop
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=TapoDesktop-{#AppVersion}-setup
SetupIconFile={#IconFile}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\TapoDesktop.exe
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start with Windows (minimized to tray)"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Tapo Desktop"; Filename: "{app}\TapoDesktop.exe"
Name: "{group}\Uninstall Tapo Desktop"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Tapo Desktop"; Filename: "{app}\TapoDesktop.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TapoDesktop"; ValueData: """{app}\TapoDesktop.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\TapoDesktop.exe"; Description: "Launch Tapo Desktop"; Flags: nowait postinstall skipifsilent
