#define MyAppName "Collector POS"
#define MyAppVersion "3.0.0-preview"
#define MyAppPublisher "Collector POS"
#define MyAppExeName "CollectorPOS.exe"

[Setup]
AppId={{D5C922C1-6F17-4E77-AF9E-97C1842F1174}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Collector POS
DefaultGroupName=Collector POS
DisableProgramGroupPage=yes
OutputDir=..\dist-installer
OutputBaseFilename=CollectorPOS-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=CollectorPOS.ico
UninstallDisplayIcon={app}\CollectorPOS.exe
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\dist\CollectorPOS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\task-price-refresh.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\task-backup.cmd"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Collector POS"; Filename: "{app}\CollectorPOS.exe"
Name: "{autodesktop}\Collector POS"; Filename: "{app}\CollectorPOS.exe"

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Collector POS Customer Display"""; Flags: runhidden waituntilterminated; StatusMsg: "Actualizando regla de red..."
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Collector POS Customer Display"" dir=in action=allow protocol=TCP localport=8765 profile=private"; Flags: runhidden waituntilterminated; StatusMsg: "Habilitando pantalla del cliente en red local..."
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /SC DAILY /ST 00:00 /TN ""Collector POS - Price Refresh 00"" /TR ""{app}\task-price-refresh.cmd"""; Flags: runhidden waituntilterminated
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /SC DAILY /ST 12:00 /TN ""Collector POS - Price Refresh 12"" /TR ""{app}\task-price-refresh.cmd"""; Flags: runhidden waituntilterminated
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /SC DAILY /ST 17:00 /TN ""Collector POS - Price Refresh 17"" /TR ""{app}\task-price-refresh.cmd"""; Flags: runhidden waituntilterminated
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /SC DAILY /ST 02:30 /TN ""Collector POS - Daily Backup"" /TR ""{app}\task-backup.cmd"""; Flags: runhidden waituntilterminated
Filename: "{app}\CollectorPOS.exe"; Description: "Abrir Collector POS"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""Collector POS - Price Refresh 00"""; Flags: runhidden waituntilterminated; RunOnceId: "DeleteTask00"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""Collector POS - Price Refresh 12"""; Flags: runhidden waituntilterminated; RunOnceId: "DeleteTask12"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""Collector POS - Price Refresh 17"""; Flags: runhidden waituntilterminated; RunOnceId: "DeleteTask17"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""Collector POS - Daily Backup"""; Flags: runhidden waituntilterminated; RunOnceId: "DeleteBackupTask"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Collector POS Customer Display"""; Flags: runhidden waituntilterminated; RunOnceId: "DeleteFirewall"

[Code]
var
  JustTCGPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  JustTCGPage := CreateInputQueryPage(wpSelectDir,
    'JustTCG',
    'Configura la búsqueda de TCG',
    'Pega la API key de JustTCG. Puedes dejarla vacía y configurarla después.');
  JustTCGPage.Add('API key:', True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir, EnvFile: String;
  Existing, NewText: AnsiString;
begin
  if CurStep = ssPostInstall then
  begin
    DataDir := ExpandConstant('{localappdata}\CollectorPOS');
    ForceDirectories(DataDir);
    EnvFile := DataDir + '\collector.env';
    Existing := '';
    if FileExists(EnvFile) then
      LoadStringFromFile(EnvFile, Existing);

    if JustTCGPage.Values[0] <> '' then
    begin
      NewText := Existing + #13#10 + 'JUSTTCG_API_KEY=' + JustTCGPage.Values[0] + #13#10;
      SaveStringToFile(EnvFile, NewText, False);
    end;
  end;
end;
