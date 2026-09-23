@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem «Аким на 5 часов» — запуск одной командой (Windows): run.bat
rem   1) создаёт виртуальное окружение .venv, если его нет;
rem   2) ставит зависимости из requirements.txt и requirements-ui.txt
rem      (повторно — только если файлы зависимостей изменились);
rem   3) создаёт .env из .env.example, если его нет;
rem   4) запускает приложение и открывает http://localhost:8501.
rem Настройки (необязательно):
rem   set PYTHON=py -3.12   — выбрать интерпретатор (нужен Python 3.11–3.14)
rem   set PORT=8502         — другой порт
rem   set NO_BROWSER=1      — не открывать браузер

if not defined PORT set "PORT=8501"
set "URL=http://localhost:%PORT%"
set "VENV_PY=.venv\Scripts\python.exe"

rem 1. Виртуальное окружение
if exist "%VENV_PY%" (
    echo [1/4] Виртуальное окружение .venv уже есть.
    goto deps
)
call :find_python
if not defined PY (
    echo Не найден Python 3.11-3.14. Установите его с https://www.python.org/downloads/
    echo и отметьте галочку "Add python.exe to PATH", затем запустите run.bat снова.
    goto fail
)
echo [1/4] Создаю виртуальное окружение .venv: %PY%
%PY% -m venv .venv
if errorlevel 1 (
    echo Не удалось создать виртуальное окружение .venv.
    goto fail
)

rem 2. Зависимости — повторно ставим, только если изменились файлы requirements
:deps
copy /b /y requirements.txt + requirements-ui.txt .venv\requirements.new >nul
fc /b .venv\requirements.new .venv\requirements.installed >nul 2>&1
if not errorlevel 1 (
    echo [2/4] Зависимости уже установлены.
    del .venv\requirements.new >nul 2>&1
    goto env
)
echo [2/4] Устанавливаю зависимости, в первый раз это займёт несколько минут...
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt -r requirements-ui.txt
if errorlevel 1 (
    echo Не удалось установить зависимости: проверьте интернет и запустите run.bat снова.
    goto fail
)
move /y .venv\requirements.new .venv\requirements.installed >nul

rem 3. Настройки ИИ-советника
:env
if exist ".env" (
    echo [3/4] Файл .env уже есть.
) else (
    copy /y .env.example .env >nul
    echo [3/4] Создан .env из .env.example: советник работает офлайн, пока в .env не указаны ключ и модель.
)

rem 4. Запуск. Браузер открываем сами, когда сервер ответит: в режиме headless
rem    Streamlit не спрашивает e-mail при первом запуске и не зависает на вводе.
echo [4/4] Запускаю приложение: %URL%   остановить — Ctrl+C
if not defined NO_BROWSER start "" /b powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; for($i=0;$i -lt 120;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 '%URL%/_stcore/health'|Out-Null; Start-Process '%URL%'; break}catch{Start-Sleep -Seconds 1}}"
"%VENV_PY%" -m streamlit run app.py --server.port %PORT% --server.headless true --browser.gatherUsageStats false
exit /b %errorlevel%

rem Подходящий Python: переменная PYTHON, затем py-лаунчер 3.13/3.12/3.11/3.14, затем python из PATH
:find_python
set "PY="
if defined PYTHON (
    set "PY=%PYTHON%"
    exit /b 0
)
for %%V in (3.13 3.12 3.11 3.14) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if defined PY exit /b 0
python -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)" >nul 2>&1 && set "PY=python"
exit /b 0

:fail
echo.
pause
exit /b 1
