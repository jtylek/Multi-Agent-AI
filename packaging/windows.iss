#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppName "Multi-Agent-AI"
#define AppExe "Multi-Agent-AI.exe"

[Setup]
AppId={{E89F6C14-17A9-46F0-8E64-31DA1A7B7920}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Multi-Agent-AI contributors
AppPublisherURL=https://github.com/jtylek/Multi-Agent-AI
DefaultDirName={localappdata}\Programs\Multi-Agent-AI
DefaultGroupName=Multi-Agent-AI
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Multi-Agent-AI-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "..\dist\Multi-Agent-AI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\Multi-Agent-AI-GUI\*"; DestDir: "{app}\GUI"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Multi-Agent-AI Control Panel"; Filename: "{app}\GUI\Multi-Agent-AI-GUI.exe"; WorkingDir: "{userdocs}"
Name: "{group}\Multi-Agent-AI Help"; Filename: "{cmd}"; Parameters: "/K ""{app}\{#AppExe}"" --help"; WorkingDir: "{userdocs}"
Name: "{group}\Check Multi-Agent-AI"; Filename: "{cmd}"; Parameters: "/K ""{app}\{#AppExe}"" doctor"; WorkingDir: "{userdocs}"
Name: "{group}\Uninstall Multi-Agent-AI"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

Name: "{autodesktop}\Multi-Agent-AI"; Filename: "{app}\GUI\Multi-Agent-AI-GUI.exe"; Tasks: desktopicon

