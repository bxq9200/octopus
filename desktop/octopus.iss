; Octopus 桌面版安装脚本 (Inno Setup 6)
; 编译: "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" desktop\octopus.iss
; 产物: dist_installer\OctopusSetup-<版本>.exe（单文件，双击安装）
; per-user 安装（PrivilegesRequired=lowest）→ 不弹 UAC、不需管理员

#define MyAppName "Octopus"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Octopus"
#define MyAppExeName "Octopus.exe"

[Setup]
AppId={{A7F3C2E1-9B4D-4E6A-8C1F-2D5E9A0B3C4D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/bxq9200/octopus
DefaultDirName={localappdata}\Programs\Octopus
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
OutputBaseFilename=OctopusSetup-{#MyAppVersion}
OutputDir={#SourcePath}..\dist_installer
SetupIconFile={#SourcePath}octopus.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "{#SourcePath}..\dist\Octopus\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Octopus"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Octopus"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 Octopus"; Flags: nowait postinstall skipifsilent
