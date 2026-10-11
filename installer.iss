[Setup]
AppName=ClipStack
AppVersion=1.0
AppPublisher=Jack Fisher
AppPublisherURL=https://github.com/jackfisher-og/clipstack
AppSupportURL=https://github.com/jackfisher-og/clipstack/issues
DefaultDirName={autopf}\ClipStack
DefaultGroupName=ClipStack
OutputDir=Output
OutputBaseFilename=ClipStack-Installer
SetupIconFile=icon.ico
Compression=lzma
SolidCompression=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\ClipStack.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "Fonts\*"; DestDir: "{app}\Fonts"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Icons\*"; DestDir: "{app}\Icons"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ClipStack"; Filename: "{app}\ClipStack.exe"; IconFilename: "{app}\icon.ico"; WorkingDir: "{app}"; Tasks: startmenuicon
Name: "{autodesktop}\ClipStack"; Filename: "{app}\ClipStack.exe"; IconFilename: "{app}\icon.ico"; WorkingDir: "{app}"; Tasks: desktopicon