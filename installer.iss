; ============================================================
;  EMA (Smart Universal Email Assistant) - Production Inno Setup
; ============================================================

#define MyAppName "EMA"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "EmailAutomater Team"
#define MyAppURL "https://github.com"
#define MyAppExeName "EMA"

; Determine brand icon path safely
#if FileExists(SourcePath + "\frontend\favicon.ico")
  #define AppIconFile SourcePath + "\frontend\favicon.ico"
  #define InstalledIconPath "{app}\frontend\favicon.ico"
#elif FileExists(SourcePath + "\favicon.ico")
  #define AppIconFile SourcePath + "\favicon.ico"
  #define InstalledIconPath "{app}\favicon.ico"
#endif

[Setup]
AppId={{9B7E3481-8B6C-49D2-A62E-095DA2C9B7E3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; Install into per-user LocalAppData (No Administrator / UAC prompt required)
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Output
OutputBaseFilename=EMA_Setup

; Embed brand icon if available
#ifdef AppIconFile
SetupIconFile={#AppIconFile}
UninstallDisplayIcon={#InstalledIconPath}
#endif

Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

DisableDirPage=no
DisableProgramGroupPage=yes
AllowNoIcons=yes
WizardStyle=modern
WizardSizePercent=105
PrivilegesRequired=lowest
CloseApplications=no
RestartApplications=no

[Tasks]
; 1. Desktop shortcut
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

; 2. Windows Startup shortcut
Name: "startupicon"; Description: "Start EMA automatically on Windows startup (Runs in background)"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 1. Preserve existing user configuration and credentials across reinstalls/updates
Source: ".env"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

; 2. Bundle all production application files while strictly excluding development files, test harnesses, and logs
Source: "*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".env,*.key,*.pem,.git\*,.git,.gitattributes,.gitignore,.venv\*,.venv,build\*,build,dist\*,dist,dist_installer\*,Output\*,*.iss,*.spec,__pycache__\*,*\__pycache__\*,*.pyc,*.pyo,*.log,backend\*.log,*.db,*.db-journal,*.db-wal,*.db-shm,backend\*.db,backend\*.db-journal,backend\*.db-wal,backend\*.db-shm,token.json,backend\token.json,build_exe.bat,EmailAutomater.bat,setup_dependencies.bat,setup_app.bat,create_exe.bat,*.SED,system_health_check.py,check_project.py,check_*.py,test_*.py,mastertest*.py,full_project_audit.py,print_structure.py,project_structure.txt,.browser_session\*,.browser_session,app_profile\*,app_profile,.agents\*,.neon\*,package.json,package-lock.json,neon.ts,skills-lock.json,database\*,database"

[Icons]
; Run silently with zero command window flash via wscript.exe
#ifdef AppIconFile
Name: "{autodesktop}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_app.vbs"""; WorkingDir: "{app}"; IconFilename: "{#InstalledIconPath}"; Tasks: desktopicon; Comment: "Launch EMA - Smart Universal Email Assistant"
Name: "{group}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_app.vbs"""; WorkingDir: "{app}"; IconFilename: "{#InstalledIconPath}"; Comment: "Launch EMA"
Name: "{userstartup}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_app.vbs"""; WorkingDir: "{app}"; IconFilename: "{#InstalledIconPath}"; Tasks: startupicon; Comment: "EMA Background Email Assistant"
#else
Name: "{autodesktop}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_app.vbs"""; WorkingDir: "{app}"; Tasks: desktopicon; Comment: "Launch EMA - Smart Universal Email Assistant"
Name: "{group}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_app.vbs"""; WorkingDir: "{app}"; Comment: "Launch EMA"
Name: "{userstartup}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_app.vbs"""; WorkingDir: "{app}"; Tasks: startupicon; Comment: "EMA Background Email Assistant"
#endif

; Uninstaller Shortcut
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"; IconFilename: "{sys}\shell32.dll,31"

[Run]
; Launch application option upon installation completion
Filename: "{sys}\wscript.exe"; Parameters: """{app}\run_app.vbs"""; WorkingDir: "{app}"; Description: "Launch {#MyAppName} now"; Flags: postinstall nowait skipifsilent; Check: CheckDependencySuccess

[UninstallDelete]
; Settings & virtual environment
Type: files; Name: "{app}\.env"
Type: files; Name: "{app}\token.json"
Type: files; Name: "{app}\backend\token.json"
Type: filesandordirs; Name: "{app}\.venv"

; Desktop app browser profile and persistent WebView2 caches
Type: filesandordirs; Name: "{app}\app_profile"
Type: filesandordirs; Name: "{localappdata}\{#MyAppName}"
Type: filesandordirs; Name: "{localappdata}\EmailAutomater"

; Python Bytecode
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\backend\__pycache__"
Type: filesandordirs; Name: "{app}\backend\models\__pycache__"
Type: filesandordirs; Name: "{app}\backend\services\__pycache__"
Type: filesandordirs; Name: "{app}\backend\utils\__pycache__"
Type: filesandordirs; Name: "{app}\extractors\__pycache__"

; Logs
Type: files; Name: "{app}\install_log.txt"
Type: files; Name: "{app}\backend_crash.log"
Type: files; Name: "{app}\calendar_assistant.log"
Type: files; Name: "{app}\*.log"
Type: files; Name: "{app}\backend\*.log"

; SQLite database & cache files
Type: files; Name: "{app}\assistant_v2.db"
Type: files; Name: "{app}\assistant_v2.db-journal"
Type: files; Name: "{app}\assistant_v2.db-wal"
Type: files; Name: "{app}\assistant_v2.db-shm"
Type: files; Name: "{app}\*.db"
Type: files; Name: "{app}\*.db-*"
Type: files; Name: "{app}\backend\*.db"
Type: files; Name: "{app}\backend\*.db-*"

[Code]
var
  DependencyInstallSuccess: Boolean;

// -------------------------------------------------------------------
// 1. Terminate only EMA-specific processes (Preserves other Python apps)
// -------------------------------------------------------------------
procedure TerminateEMAProcesses();
var
  ResultCode: Integer;
  PsCmd: String;
begin
  PsCmd := '$targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ' +
           'Where-Object { $_.CommandLine -like "*desktop_app.py*" -or $_.CommandLine -like "*run_app.vbs*" }; ' +
           'if ($targets) { $targets | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }; ' +
           '$ports = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue; ' +
           'if ($ports) { $ports | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }';

  Exec('powershell.exe', '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + PsCmd + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  TerminateEMAProcesses();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    TerminateEMAProcesses();
  end;
end;

// -------------------------------------------------------------------
// 2. Python Availability Pre-Check
// -------------------------------------------------------------------
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  DependencyInstallSuccess := True;

  if not Exec(ExpandConstant('{cmd}'), '/c python --version >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    if not Exec(ExpandConstant('{cmd}'), '/c py -3 --version >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      if MsgBox('Python 3.10 or higher was not detected on your system PATH.' + #13#10 + #13#10 +
                'EMA requires Python 3.10+ to configure its background service and packages.' + #13#10 + #13#10 +
                'Would you like to proceed with the installation anyway?',
                mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;
  Result := True;
end;

function CheckDependencySuccess(): Boolean;
begin
  Result := DependencyInstallSuccess;
end;

// -------------------------------------------------------------------
// 3. Write Intelligent VBScript Launcher (Prioritizes .venv)
// -------------------------------------------------------------------
procedure EnsureVbsLauncher(AppDir: String);
var
  VbsLines: TArrayOfString;
  VbsPath: String;
begin
  VbsPath := AppDir + '\run_app.vbs';
  SetArrayLength(VbsLines, 14);
  VbsLines[0]  := 'Set WshShell = CreateObject("WScript.Shell")';
  VbsLines[1]  := 'Set fso = CreateObject("Scripting.FileSystemObject")';
  VbsLines[2]  := 'appDir = fso.GetParentFolderName(WScript.ScriptFullName)';
  VbsLines[3]  := 'WshShell.CurrentDirectory = appDir';
  VbsLines[4]  := 'venvPython = appDir & "\.venv\Scripts\pythonw.exe"';
  VbsLines[5]  := 'scriptPath = appDir & "\desktop_app.py"';
  VbsLines[6]  := '';
  VbsLines[7]  := 'If fso.FileExists(venvPython) Then';
  VbsLines[8]  := '    WshShell.Run """" & venvPython & """ """ & scriptPath & """", 0, False';
  VbsLines[9]  := 'Else';
  VbsLines[10] := '    WshShell.Run "pythonw """ & scriptPath & """", 0, False';
  VbsLines[11] := 'End If';
  VbsLines[12] := 'Set WshShell = Nothing';
  VbsLines[13] := 'Set fso = Nothing';

  SaveStringsToFile(VbsPath, VbsLines, False);
end;

// -------------------------------------------------------------------
// 4. Post-Install Dependency Configuration with Marquee Progress
// -------------------------------------------------------------------
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  AppDir: String;
  BatPath: String;
begin
  if CurStep = ssPostInstall then
  begin
    AppDir := ExpandConstant('{app}');
    EnsureVbsLauncher(AppDir);

    BatPath := AppDir + '\install_dependencies.bat';

    // If install_dependencies.bat exists, execute it with pulsing marquee
    if FileExists(BatPath) then
    begin
      WizardForm.StatusLabel.Caption := 'Configuring EMA environment and installing dependencies...';
      WizardForm.FilenameLabel.Caption := 'Installing packages (this may take 1-2 minutes)...';
      WizardForm.ProgressGauge.Position := 0;
      WizardForm.ProgressGauge.Style := npbstMarquee;
      WizardForm.Refresh();

      if not Exec(ExpandConstant('{cmd}'), '/c ""' + BatPath + '""', AppDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      begin
        DependencyInstallSuccess := False;
        WizardForm.ProgressGauge.Style := npbstNormal;
        WizardForm.ProgressGauge.Position := 0;
        WizardForm.StatusLabel.Caption := 'Dependency installation failed.';
        WizardForm.FilenameLabel.Caption := 'Check install_log.txt for details.';

        MsgBox('Python package setup encountered an issue (Exit Code: ' + IntToStr(ResultCode) + ').' + #13#10 + #13#10 +
               'Please verify your internet connection and ensure Python 3.10+ is installed.' + #13#10 + #13#10 +
               'Review "install_log.txt" in the installation folder for details.',
               mbError, MB_OK);
      end
      else
      begin
        DependencyInstallSuccess := True;
        WizardForm.ProgressGauge.Style := npbstNormal;
        WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Max;
        WizardForm.StatusLabel.Caption := 'Environment setup completed successfully.';
        WizardForm.FilenameLabel.Caption := 'Ready to launch.';
      end;
    end
    else
    begin
      DependencyInstallSuccess := True;
    end;
  end;
end;