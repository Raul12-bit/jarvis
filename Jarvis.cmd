@echo off
rem Первый запуск ставит всё само, дальше Джарвис стартует сразу и без консоли.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "JARVIS_DIR=%LOCALAPPDATA%\Jarvis"
set "VENV=%JARVIS_DIR%\venv"

if exist "%VENV%\Scripts\pythonw.exe" if exist "%VENV%\requirements.lock" (
    fc /b "requirements.txt" "%VENV%\requirements.lock" >nul 2>&1 && goto run
)

echo.
echo   Джарвис: первая настройка, это займёт пару минут.
echo.

set "PY="
py -3.12 -c "" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 -c "" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3.11 -c "" >nul 2>&1 && set "PY=py -3.11"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo   Не найден Python 3.11 или новее.
    echo   Установите его командой:  winget install Python.Python.3.12
    echo   и запустите этот файл ещё раз.
    echo.
    pause
    exit /b 1
)

if not exist "%JARVIS_DIR%" mkdir "%JARVIS_DIR%"
if not exist "%VENV%\Scripts\python.exe" (
    echo   [1/2] Создаю окружение...
    %PY% -m venv "%VENV%" || goto failed
)
echo   [2/2] Устанавливаю компоненты...
"%VENV%\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto failed
copy /y "requirements.txt" "%VENV%\requirements.lock" >nul
echo.
echo   Готово. Запускаю Джарвиса.

:run
start "" "%VENV%\Scripts\pythonw.exe" -m jarvis
exit /b 0

:failed
echo.
echo   Установка не удалась. Проверьте интернет и запустите файл ещё раз.
echo.
pause
exit /b 1
