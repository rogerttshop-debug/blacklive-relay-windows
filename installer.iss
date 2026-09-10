; Instalador do Black Live (Inno Setup) — esconde a _internal do aluno:
; instala em %LocalAppData%\Black Live (SEM pedir admin), cria icones e abre no fim.
#define MyAppName "Black Live"
#define MyAppVersion "1.9.0"
#define MyAppExeName "Black Live.exe"

[Setup]
AppId={{7B1ACC13-B14C-4C11-9E00-000000000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Black Live
DefaultDirName={localappdata}\{#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=installer_out
OutputBaseFilename=BlackLiveSetup
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
WizardStyle=modern

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Files]
Source: "dist\Black Live\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
; fecha qualquer versao antiga aberta antes de lancar a nova
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM ""Black Live.exe"" /T & exit 0"; Flags: runhidden
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir o {#MyAppName} agora"; Flags: nowait postinstall skipifsilent
