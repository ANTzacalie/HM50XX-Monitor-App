#ifndef AppVersion
  #define AppVersion "1.3.2"
#endif
#define AppName "HM50xx Spectral Monitor"
#define AppFolder "HM50xx-Spectral-Monitor"
#define AppExecutable AppFolder + ".exe"
#define AppIdentifier "{43FCA985-F10E-45C1-A821-E6AC026A5E99}"
#define DeveloperName "Mihalcea Catalin Antonio"
#define ProjectURL "https://github.com/ANTzacalie/HM50XX-Monitor-App"
#define ReleaseDate "23/11/2026"
#define LegacyName "HAMEG Spectrum Studio"
#define LegacyExecutableName "HAMEG-Spectrum-Studio.exe"

[Setup]
AppId={{#AppIdentifier}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#DeveloperName}
AppPublisherURL={#ProjectURL}
AppComments=Released on {#ReleaseDate}
VersionInfoCompany={#DeveloperName}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoVersion={#AppVersion}.0
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0.10240
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename={#AppFolder}-Setup
SetupIconFile=spectral-monitor.ico
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#AppExecutable}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#AppFolder}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExecutable}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExecutable}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExecutable}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdentifier}_is1';

var
  LegacyExecutable: String;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PreviousName, PreviousDirectory: String;
begin
  Result := '';
  LegacyExecutable := '';
  if RegQueryStringValue(HKCU, UninstallKey, 'DisplayName', PreviousName) and
    (Pos('{#LegacyName} ', PreviousName) = 1) and
    RegQueryStringValue(HKCU, UninstallKey, 'InstallLocation', PreviousDirectory) and
    (CompareText(RemoveBackslashUnlessRoot(PreviousDirectory),
      RemoveBackslashUnlessRoot(WizardDirValue)) = 0) then
    LegacyExecutable := AddBackslash(WizardDirValue) + '{#LegacyExecutableName}';
end;

procedure RegisterExtraCloseApplicationsResources;
begin
  if (LegacyExecutable <> '') and FileExists(LegacyExecutable) then
#if Ver >= EncodeVer(7, 0, 0, 0)
    RegisterExtraCloseApplicationsResource(LegacyExecutable);
#else
    RegisterExtraCloseApplicationsResource(False, LegacyExecutable);
#endif
end;

procedure RemoveLegacyFile(const Path: String);
begin
  if FileExists(Path) and not DeleteFile(Path) then
    Log('Could not remove legacy file: ' + Path);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (LegacyExecutable <> '') and
    FileExists(ExpandConstant('{app}\{#AppExecutable}')) then
  begin
    RemoveLegacyFile(LegacyExecutable);
    RemoveLegacyFile(ExpandConstant('{autoprograms}\{#LegacyName}.lnk'));
    RemoveLegacyFile(ExpandConstant('{autodesktop}\{#LegacyName}.lnk'));
  end;
end;
