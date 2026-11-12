; Inno Setup script for the NovelDownloader desktop bundle.
;
; Build (from the repo root, after scripts/build_desktop.py succeeded):
;   iscc /DAppVersion=1.2.0 packaging\noveldl_installer.iss
; -> dist\NovelDownloader-setup-<version>-x64.exe
;
; Design: per-user install (no UAC), desktop + Start-menu icons, and user
; data (settings.toml, downloads/, caches) is intentionally preserved on
; uninstall.  The app home (%LOCALAPPDATA%\novel_downloader) is a sibling of
; the install directory, never inside it, so no payload path overlaps it.

#define MyAppName "书音 BookTone"
#define MyAppExe "NovelDownloader.exe"
#define MyAppPublisher "lingyi-ling"

#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7A1C0B4D-52E8-4F6A-9B3D-10C2E5F7A821}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/lingyi-ling/novel-downloader
DefaultDirName={localappdata}\Programs\NovelDownloader
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; The frozen payload is x64-only.  Without this, Setup happily installs it on
; 32-bit Windows and the launcher then cannot start at all.  ``x64compatible``
; also accepts Windows 11 on ARM, which runs the bundle under x64 emulation.
; ``VER`` is the ISPP compiler version, so Inno Setup 6.0-6.2 (which predates
; the x64compatible token) still compiles the script.
; Do NOT set ArchitecturesInstallIn64BitMode here: the product has always
; installed in 32-bit mode, and switching now would move the uninstall key to
; the 64-bit registry view and leave the old entry behind on upgrade.
#if VER >= EncodeVer(6,3,0)
ArchitecturesAllowed=x64compatible
#else
ArchitecturesAllowed=x64
#endif
OutputDir=..\dist
OutputBaseFilename=NovelDownloader-setup-{#AppVersion}-x64
SetupIconFile=..\src\novel_downloader\resources\icons\app.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExe}
; Keep the setup executable itself on the same version as the frozen bundle
; and latest.json: Explorer's Details tab, Add/Remove Programs and the update
; manifest then all agree.  The numeric fields accept the three-part version
; that ``novel_downloader.__version__`` uses (Windows pads the fourth field).
VersionInfoVersion={#AppVersion}
VersionInfoTextVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoCopyright=MIT License
VersionInfoOriginalFileName=NovelDownloader-setup-{#AppVersion}-x64.exe
CloseApplications=yes
; The frozen payload is .exe/.dll/.pyd; the default filter omits .pyd, so an
; instance that only keeps an extension module mapped could go undetected.
CloseApplicationsFilter=*.exe,*.dll,*.pyd,*.chm
; The app creates this mutex at start-up. With it, Setup uses the Restart
; Manager to close a running instance deterministically before replacing the
; binaries -- which is what the in-app updater relies on after launching us
; with /CLOSEAPPLICATIONS.
AppMutex=NovelDownloader-UpdateMutex
; A manual upgrade restarts the app through the [Run] entry below.  Setup must
; not restart it on top of that: the updater passes /RESTARTAPPLICATIONS, but
; Inno can only honour that for apps that registered with
; RegisterApplicationRestart (this one does not), so the [Run] entry is the
; mechanism that actually brings the new build back.
RestartApplications=no
; Upgrades keep the previous directory and desktop-icon choice.
UsePreviousAppDir=yes
UsePreviousTasks=yes

[InstallDelete]
; PyInstaller onedir keeps every module/resource below _internal\.  Inno only
; overwrites files that exist in the new build, so an upgrade would otherwise
; keep files that this version no longer ships -- including old
; novel_downloader\plugins\sites\* directories, which the plugin registry
; enumerates from disk at start-up (removed plugins would reappear).  Remove
; the payload directory before [Files] copies the new one.  User data never
; lives here: settings.toml, downloads/ and every cache are under
; %LOCALAPPDATA%\novel_downloader, and uninstall removes nothing outside
; {app}.
; Residual risk: an install interrupted between this delete and the copy
; leaves the app unusable until Setup is run again (no user data is lost).
; The directory name must stay equal to PyInstaller's contents_directory
; (pinned in packaging/novel_desktop.spec and checked in tests).
Type: filesandordirs; Name: "{app}\_internal"

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
; No skipifsilent: the in-app updater launches Setup with /SILENT and expects
; the new build to come back ("Install and restart" in the UI).  Restart
; Manager cannot do that here (no RegisterApplicationRestart), so this entry
; is the only relaunch mechanism.  A silent *fresh* install also starts the
; app once, which is the conventional postinstall behaviour.
Filename: "{app}\{#MyAppExe}"; \
    Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall

[UninstallRun]
; nothing: user data under the app home is preserved by design
