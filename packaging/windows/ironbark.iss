; Inno Setup script for the Windows installer (Inno Setup 6). Built by build.ps1, which passes the
; version and the folders:  ISCC /DAppVersion=X.Y.Z /DSourceDir=<PyInstaller output> /DOutputDir=<out>
;
; Installs for the current user only, with no admin prompt, into %LOCALAPPDATA%\Programs\Ironbark.
; Installing a newer version over an older one upgrades it in place. Uninstalling removes the network
; playback cache's copies of songs (%LOCALAPPDATA%\Ironbark\NetCache) and leaves the user's settings
; and playlists (%APPDATA%\Ironbark) and the library database (%LOCALAPPDATA%\Ironbark) unless they
; choose to remove them. A silent uninstall (an upgrade, a script) keeps everything.
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
; If Ironbark is open, setup offers to close it: Windows asks it to close as its window's close
; button would, so it saves everything first. Setup can only reopen programs that register with
; Windows for that (RegisterApplicationRestart), which Ironbark doesn't; the last page's "Launch
; Ironbark" box starts it again.
CloseApplications=yes
RestartApplications=no
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
{ The Tasks page's tick boxes sit flush with the list's left edge, and with the modern wizard style
  their left side is cut off (seen at 150% display scaling, Inno Setup 6.7.3). A little more space
  before each box shows it whole. }
procedure InitializeWizard();
begin
  WizardForm.TasksList.Offset := WizardForm.TasksList.Offset + ScaleX(4);
end;

{ The running player holds this mutex (ironbark/instance.py, INSTALLER_MUTEX). Uninstalling while it
  runs would leave its files behind, so ask for it to be closed first. Setup itself doesn't need
  this: CloseApplications offers to close it. }
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

{ The player keeps its data in two folders: settings, playlists and logs in the roaming one
  (settings.settings_path), the library database (every song's name and place, play counts,
  favourites) and the network playback cache (copies of songs from network drives) in the local
  one (settings.cache_folder). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Settings, LocalData, What, Where: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  { Silent uninstalls (upgrades, scripts) always keep everything. The default answer is No. }
  if UninstallSilent then
    Exit;
  Settings := ExpandConstant('{userappdata}\Ironbark');
  LocalData := ExpandConstant('{localappdata}\Ironbark');
  { The network playback cache's copies of songs (as much as the size chosen in Settings, 10 GB at
    first) can always be downloaded again: they go without asking. }
  DelTree(LocalData + '\NetCache', True, True, True);
  Where := '';
  if DirExists(Settings) then
    Where := Where + #13#10 + Settings;
  if DirExists(LocalData) then
    Where := Where + #13#10 + LocalData;
  if Where = '' then
    Exit;
  What := 'settings, playlists and library database (play counts and favourites)';
  if SuppressibleMsgBox('Also remove your Ironbark ' + What + '?' + #13#10#13#10 +
       'Your music files are never touched. These are in:' + Where + #13#10#13#10 +
       'Choose No to keep them for next time.',
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
  begin
    DelTree(Settings, True, True, True);
    DelTree(LocalData, True, True, True);
  end;
end;
