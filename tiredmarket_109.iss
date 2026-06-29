; ============================================================================
; Tired Market - Inno Setup installer  v4.14.6.109
; ----------------------------------------------------------------------------
; Wraps the PyInstaller ONEDIR build (dist\TiredMarket-portable-v4.14.6.109\)
; into a single Setup .exe.
;
; DATA SAFETY (critical):
;   This installer lays down PROGRAM FILES ONLY. It NEVER creates, seeds, or
;   (on uninstall) deletes the data dir. Data location + ship-asset seeding are
;   owned entirely by the app at runtime (the frozen first-run data-location
;   chooser + the v4.14.6.109 _seed_ship_assets() copy-out). There is no
;   [UninstallDelete] section and no "also remove data?" page, so removal can
;   never target a user's data — wherever it lives, it is left untouched.
; ============================================================================

#define MyAppName "Tired Market"
#define MyAppVersion "4.14.6.112"
#define MyAppPublisher "mseals"
#define MyAppExeName "TiredMarket.exe"
#define SourceDir "D:\TiredMarket\dist\TiredMarket-portable-v4.14.6.112"

[Setup]
; Stable AppId so future versions upgrade-in-place instead of installing twice.
AppId={{8F3A1C5E-2B94-4D7A-9E61-5C0AD2F47B33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\TiredMarket
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=D:\TiredMarket\dist
OutputBaseFilename=TiredMarket-Setup-v{#MyAppVersion}
SetupIconFile=D:\TiredMarket\tired_market.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Installing into Program Files requires elevation.
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Everything in the onedir folder (TiredMarket.exe + the whole _internal\ tree),
; recursive, structure preserved.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\tired_market.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\tired_market.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; NOTE: deliberately NO [UninstallDelete] section. Uninstall removes only the
; files this installer wrote under {app}, the shortcuts, and the registry
; uninstall entry. The data dir is never named here, so it always survives.
