; Inno Setup script for the Windows installer (Inno Setup 6). Built by build.ps1, which passes the
; version and the folders:  ISCC /DAppVersion=X.Y.Z /DSourceDir=<PyInstaller output> /DOutputDir=<out>
;
; Installs for the current user only, with no admin prompt, into %LOCALAPPDATA%\Programs\Ironbark.
; Installing a newer version over an older one upgrades it in place. Uninstalling leaves the user's
; settings and playlists (%APPDATA%\Ironbark) unless they choose to remove them.
;
; Optional (unticked): Ironbark in Explorer's "Open with" list for music files, playlists and CUE
; sheets. It never makes itself the default program for anything.

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
AppSupportURL=https://www.digitalgreenhouse.com.au
AppUpdatesURL=https://www.digitalgreenhouse.com.au
AppContact=https://www.digitalgreenhouse.com.au
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
; Explorer re-reads its "Open with" lists after installing and uninstalling.
ChangesAssociations=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "openwith"; Description: "Add Ironbark to ""Open with"" for music files (it won't become the default)"; GroupDescription: "Explorer:"; Flags: unchecked

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

[Registry]
; "Open with": Ironbark offered for these file types, under the current user only. Nothing here sets a
; default program. All of it goes on uninstall (uninsdeletekey / uninsdeletevalue).
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "Ironbark"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\Ironbark.exe,0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Ironbark.exe"" ""%1"""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".flac"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".wav"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".aif"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".aiff"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".m4a"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".mp3"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".ogg"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".opus"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".cue"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".m3u"; ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\Ironbark.exe\SupportedTypes"; ValueType: string; ValueName: ".m3u8"; ValueData: ""; Tasks: openwith
; Ironbark's own file kinds (ProgIDs), which the extensions' OpenWithProgids lists point at.
Root: HKCU; Subkey: "Software\Classes\Ironbark.Music"; ValueType: string; ValueName: ""; ValueData: "Music file"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.Music\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\Ironbark.exe,0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.Music\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Ironbark.exe"" ""%1"""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.Playlist"; ValueType: string; ValueName: ""; ValueData: "Playlist"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.Playlist\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\Ironbark.exe,0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.Playlist\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Ironbark.exe"" ""%1"""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.CueSheet"; ValueType: string; ValueName: ""; ValueData: "CUE sheet"; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.CueSheet\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\Ironbark.exe,0"; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Ironbark.CueSheet\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Ironbark.exe"" ""%1"""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.flac\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.wav\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.aif\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.aiff\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.m4a\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.mp3\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.ogg\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.opus\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Music"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.cue\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.CueSheet"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.m3u\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Playlist"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\.m3u8\OpenWithProgids"; ValueType: string; ValueName: "Ironbark.Playlist"; ValueData: ""; Flags: uninsdeletevalue; Tasks: openwith

[Run]
Filename: "{app}\Ironbark.exe"; Description: "{cm:LaunchProgram,Ironbark}"; Flags: nowait postinstall skipifsilent

[Code]
{ The running player holds this mutex (ironbark/instance.py, INSTALLER_MUTEX). Uninstalling while it
  runs would leave its files behind, so ask for it to be closed first. Setup itself doesn't need
  this: CloseApplications offers to close it and reopen it afterwards. }
function InitializeUninstall(): Boolean;
begin
  Result := True;
  while CheckForMutexes('DigitalGreenhouse.Ironbark.Running') do
  begin
    if UninstallSilent then
      Exit;
    if MsgBox('Ironbark is open. Close it, then click OK to carry on uninstalling.',
         mbError, MB_OKCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Settings, Local: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  { Silent uninstalls (upgrades, scripts) always keep everything. The default answer is No. }
  if UninstallSilent then
    Exit;
  Settings := ExpandConstant('{userappdata}\Ironbark');
  Local := ExpandConstant('{localappdata}\Ironbark');
  { The network playback cache's copies of songs (as much as the size chosen in Settings, 10 GB at
    first) can always be downloaded again: they go without asking. }
  DelTree(Local + '\NetCache', True, True, True);
  if not DirExists(Settings) and not DirExists(Local) then
    Exit;
  if SuppressibleMsgBox('Also remove your Ironbark settings, playlists and library index?' + #13#10#13#10 +
       'They are in ' + Settings + #13#10 + 'and ' + Local + #13#10 + 'Choose No to keep them for next time.',
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
  begin
    DelTree(Settings, True, True, True);
    DelTree(Local, True, True, True);
  end;
end;
