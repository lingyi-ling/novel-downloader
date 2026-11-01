; Inno Setup script for the NovelDownloader desktop bundle.
;
; Build (from the repo root, after scripts/build_desktop.py succeeded):
;   iscc /DAppVersion=1.2.0 packaging\noveldl_installer.iss
; -> dist\NovelDownloader-setup-<version>-x64.exe
;
; Design: per-user install (no UAC), desktop + Start-menu icons, and user
; data (settings.toml, downloads/) is intentionally preserved on uninstall.

#define MyAppName "Novel Downloader"
#define MyAppExe "NovelDownloader.exe"

#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7A1C0B4D-52E8-4F6A-9B3D-10C2E5F7A821}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher=lingyi-ling
AppPublisherURL=https://github.com/lingyi-ling/novel-downloader
DefaultDirName={localappdata}\Programs\NovelDownloader
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=NovelDownloader-setup-{#AppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExe}
CloseApplications=yes
; The app creates this mutex at start-up. With it, Setup uses the Restart
; Manager to close a running instance deterministically before replacing the
; binaries -- which is what the in-app updater relies on after launching us
; with /CLOSEAPPLICATIONS.
AppMutex=NovelDownloader-UpdateMutex
RestartApplications=no

[Tasks]
Name: "desktopicon"; \
    Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\NovelDownloader\*"; \
    DestDir: "{app}"; \
    Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Novel Downloader"; \
    Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\Novel Downloader"; \
    Filename: "{app}\{#MyAppExe}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; \
    Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; nothing: user data under the app home is preserved by design
