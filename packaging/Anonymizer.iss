#define MyAppName "Anonymizer"
#define MyAppVersion "2.2.0"
#define MyAppExeName "Anonymizer.exe"
#define WebView2Setup "MicrosoftEdgeWebview2Setup.exe"

[Setup]
AppId={{E62FD6ED-6A47-48D7-92F8-96822B64B5C8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\Anonymizer
DefaultGroupName=Anonymizer
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Anonymizer-Setup-2.2.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=app.ico
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes
CloseApplications=yes

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\QUICKSTART.md"; DestDir: "{app}"; Flags: ignoreversion
#ifexist "{#WebView2Setup}"
; Загрузчик компонента окна: ставится только там, где его ещё нет.
Source: "{#WebView2Setup}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not WebView2Installed
#endif

[Icons]
Name: "{group}\Anonymizer"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Anonymizer"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked
Name: "addin"; Description: "Подключить к Word, Excel и PowerPoint (кнопка Anonymizer и работа в фоне)"; GroupDescription: "Office:"

[Run]
#ifexist "{#WebView2Setup}"
Filename: "{tmp}\{#WebView2Setup}"; Parameters: "/silent /install"; StatusMsg: "Установка компонента Microsoft Edge WebView2…"; Flags: waituntilterminated; Check: not WebView2Installed
#endif
; Сертификат локальной страницы add-in, регистрация в Office и автозапуск. Windows сам спросит подтверждение сертификата.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install-addin"; StatusMsg: "Подключение к Word, Excel и PowerPoint…"; Flags: waituntilterminated runasoriginaluser; Tasks: addin
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить Anonymizer"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall-addin"; RunOnceId: "RemoveAddin"; Flags: waituntilterminated runhidden

[Code]
const
  WebView2Key = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function WebView2Version(Root: Integer; const Key: String): String;
begin
  if not RegQueryStringValue(Root, Key, 'pv', Result) then
    Result := '';
end;

// Компонент Evergreen ставится на компьютер (HKLM, 32-разрядный раздел реестра) или на одного пользователя (HKCU).
function WebView2Installed: Boolean;
var
  Version: String;
begin
  Version := WebView2Version(HKLM32, WebView2Key);
  if (Version = '') or (Version = '0.0.0.0') then
    Version := WebView2Version(HKCU, WebView2Key);
  Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup: Boolean;
begin
  Result := True;
#ifnexist "{#WebView2Setup}"
  if not WebView2Installed then
    MsgBox('Для окна программы нужен компонент Microsoft Edge WebView2 Runtime, он не найден. Установите его с сайта Microsoft ' +
           '(поиск: WebView2 Runtime) до первого запуска программы.', mbInformation, MB_OK);
#endif
end;
