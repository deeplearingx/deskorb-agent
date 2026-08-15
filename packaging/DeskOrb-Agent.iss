#define AppName "DeskOrb Agent"
#define AppVersion "0.2.0"
#define AppPublisher "DeskOrb Agent"

#ifndef ReleaseRoot
  #define ReleaseRoot "..\artifacts\installer\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\artifacts\installer"
#endif

[Setup]
AppId={{9B7C4F55-8A3D-4B23-9B52-7D8B5CBF7A20}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\DeskOrb Agent
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=DeskOrb-Agent-0.2.0-Setup-x64
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayIcon={app}\runtime\python\pythonw.exe
VersionInfoVersion=0.2.0.0
VersionInfoDescription=DeskOrb Agent desktop assistant
VersionInfoProductName=DeskOrb Agent
VersionInfoProductVersion=0.2.0

[Files]
Source: "{#ReleaseRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\DeskOrb Agent"; Filename: "{app}\Start DeskOrb Agent Portable.cmd"; WorkingDir: "{app}"
Name: "{autodesktop}\DeskOrb Agent"; Filename: "{app}\Start DeskOrb Agent Portable.cmd"; WorkingDir: "{app}"
Name: "{autoprograms}\DeskOrb Agent README"; Filename: "{app}\README.md"; WorkingDir: "{app}"

[Run]
Filename: "{app}\runtime\python\Scripts\conda-unpack.exe"; WorkingDir: "{app}\runtime\python"; StatusMsg: "Finalizing the bundled Python runtime..."; Flags: runhidden waituntilterminated
Filename: "{sys}\cmd.exe"; Parameters: "/c ""{app}\Start DeskOrb Agent Portable.cmd"""; Description: "Launch DeskOrb Agent"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked
