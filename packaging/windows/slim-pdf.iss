; Inno Setup script for Slim PDF.
;
;   iscc /DAppVersion=0.1.0 /DSourceDir="..\..\dist\Slim PDF" packaging\windows\slim-pdf.iss
;
; Installs per user by default (no admin rights needed); "Install for all
; users" is offered in the dialog. Output: dist\Slim-PDF-<version>-setup.exe

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\Slim PDF"
#endif
#define AppName "Slim PDF"
#define AppExe "Slim PDF.exe"
; /DSign (with /Ssigntool=... from the release workflow) signs the two
; executables, the uninstaller and the setup itself.
#ifdef Sign
  #define SignFlag " sign"
#else
  #define SignFlag ""
#endif

[Setup]
AppId={{6E0B2B5C-4C1E-4E55-9C1A-8F3D5B0D7A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Marcel Ritter
AppPublisherURL=https://github.com/linuxrrze/slim-pdf
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=Slim-PDF-{#AppVersion}-setup
SetupIconFile=..\build-icons\slim-pdf.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
#ifdef Sign
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "Add the slim-pdf command to PATH"; GroupDescription: "Command line:"
Name: "openwith"; Description: "Offer Slim PDF in ""Open with"" for PDF files"; GroupDescription: "Integration:"

[Files]
Source: "{#SourceDir}\*"; Excludes: "\Slim PDF.exe,\slim-pdf.exe"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\Slim PDF.exe"; DestDir: "{app}"; Flags: ignoreversion{#SignFlag}
Source: "{#SourceDir}\slim-pdf.exe"; DestDir: "{app}"; Flags: ignoreversion{#SignFlag}

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; "Open with" entry for .pdf (does not take over the default PDF viewer)
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// Add/remove {app} to/from the PATH of the current user (or all users).

function PathKeyRoot(): Integer;
begin
  if IsAdminInstallMode then Result := HKEY_LOCAL_MACHINE else Result := HKEY_CURRENT_USER;
end;

function PathKey(): String;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

procedure AddToPath();
var
  Paths: String;
begin
  if not RegQueryStringValue(PathKeyRoot(), PathKey(), 'Path', Paths) then Paths := '';
  if Pos(';' + Uppercase(ExpandConstant('{app}')) + ';', ';' + Uppercase(Paths) + ';') > 0 then exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then Paths := Paths + ';';
  RegWriteExpandStringValue(PathKeyRoot(), PathKey(), 'Path', Paths + ExpandConstant('{app}'));
end;

procedure RemoveFromPath();
var
  Paths, Padded, App: String;
  P: Integer;
begin
  if not RegQueryStringValue(PathKeyRoot(), PathKey(), 'Path', Paths) then exit;
  App := ExpandConstant('{app}');
  Padded := ';' + Paths + ';';
  P := Pos(';' + Uppercase(App) + ';', Uppercase(Padded));
  if P = 0 then exit;
  Delete(Padded, P, Length(App) + 1);  { removes ';<app>', keeps the next ';' }
  Paths := Copy(Padded, 2, Length(Padded) - 2);
  RegWriteExpandStringValue(PathKeyRoot(), PathKey(), 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then AddToPath();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then RemoveFromPath();
end;
