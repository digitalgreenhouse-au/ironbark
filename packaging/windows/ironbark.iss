; Inno Setup script for the Windows installer (Inno Setup 6). Built by build.ps1, which passes the
; version and the folders:  ISCC /DAppVersion=X.Y.Z /DSourceDir=<PyInstaller output> /DOutputDir=<out>
;
; Installs for the current user only, with no admin prompt, into %LOCALAPPDATA%\Programs\Ironbark.
; Installing a newer version over an older one upgrades it in place. Uninstalling leaves the user's
; settings and playlists (%APPDATA%\Ironbark) unless they choose to remove them.

#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=X.Y.Z
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\windows\Ironbark"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\windows"
#endif

[Setup]
; The AppId identifies Ironbark to Windows across versions: never change it, or upgrades break.
AppId={{02A5E50B-106F-4CB2-9E16-AB086C9E6871}
AppName=Ironbark
AppVersion={#AppVersion}
AppVerName=Ironbark {#AppVersion}
AppPublisher=Digital Greenhouse
AppPublisherURL=https://digitalgreenhouse.com.au/
AppCopyright=Copyright Digital Greenhouse. MIT licence.
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Ironbark music player installer
PrivilegesRequired=lowest
DefaultDirName={autopf}\Ironbark
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=..\..\ironbark\assets\ironbark.ico
UninstallDisplayIcon={app}\Ironbark.exe
UninstallDisplayName=Ironbark
OutputDir={#OutputDir}
OutputBaseFilename=Ironbark-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
; If Ironbark is open, setup offers to close it (and reopen it afterwards).
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the bundled libraries whole, so files a newer version no longer uses don't linger.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\licenses"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The shortcut carries the app's AppUserModelID, the one the program sets for itself, so Windows
; treats the shortcut, the taskbar button and the media controls as the same app.
Name: "{autoprograms}\Ironbark"; Filename: "{app}\Ironbark.exe"; AppUserModelID: "DigitalGreenhouse.Ironbark"
Name: "{autodesktop}\Ironbark"; Filename: "{app}\Ironbark.exe"; AppUserModelID: "DigitalGreenhouse.Ironbark"; Tasks: desktopicon

[Run]
Filename: "{app}\Ironbark.exe"; Description: "{cm:LaunchProgram,Ironbark}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Settings: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Settings := ExpandConstant('{userappdata}\Ironbark');
  if not DirExists(Settings) then
    Exit;
  { Silent uninstalls (upgrades, scripts) always keep them. The default answer is No. }
  if UninstallSilent then
    Exit;
  if SuppressibleMsgBox('Also remove your Ironbark settings, playlists and library index?' + #13#10#13#10 +
       'They are in ' + Settings + #13#10 + 'Choose No to keep them for next time.',
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    DelTree(Settings, True, True, True);
end;
