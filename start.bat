@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "NO_PAUSE="
set "CHECK_MODE="
:collect_options
if "%~1"=="" goto options_done
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
if /I "%~1"=="--check" set "CHECK_MODE=1"
shift
goto collect_options
:options_done

set "PYTHON="
where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON set "PYTHON=%%P"
)
if not defined PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON set "PYTHON=%%P"
    )
)

if not defined PYTHON (
    echo [错误] 未检测到 Python 3.11 或更高版本。
    echo 请安装：https://www.python.org/downloads/windows/
    echo 或执行：winget install Python.Python.3.12
    echo 安装时请勾选 Add python.exe to PATH，然后重新运行本脚本。
    goto :failed
)

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前 Python 版本低于 3.11：
    "%PYTHON%" --version
    echo 请安装：https://www.python.org/downloads/windows/
    goto :failed
)

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo [信息] 正在创建项目虚拟环境...
    "%PYTHON%" -m venv "%~dp0.venv"
    if errorlevel 1 (
        echo [错误] 无法创建虚拟环境，当前 Python 可能缺少 venv/ensurepip。
        echo 请重新安装 Python，并确认启用了 pip 和 venv 组件。
        goto :failed
    )
)

"%VENV_PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [信息] 虚拟环境缺少 pip，正在尝试修复...
    "%VENV_PYTHON%" -m ensurepip --upgrade
    if errorlevel 1 (
        echo [错误] 无法准备 pip。
        echo 请执行："%VENV_PYTHON%" -m ensurepip --upgrade
        goto :failed
    )
)

if exist "%~dp0requirements.txt" (
    echo [信息] 正在检查项目依赖...
    "%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [错误] 依赖安装失败。请联网后手动执行：
        echo "%VENV_PYTHON%" -m pip install -r "%~dp0requirements.txt"
        echo Python 下载：https://www.python.org/downloads/windows/
        goto :failed
    )
)

if defined CHECK_MODE (
    echo [信息] 正在执行环境检查...
    "%VENV_PYTHON%" -m gcash_linker --check
) else (
    echo [信息] 正在启动本地 Web 控制台...
    "%VENV_PYTHON%" -m gcash_linker.web --host 127.0.0.1 --port 8765
)
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [错误] 程序退出，代码：%EXIT_CODE%
    goto :failed_with_code
)
goto :success

:failed
set "EXIT_CODE=1"
:failed_with_code
if not defined NO_PAUSE pause
exit /b %EXIT_CODE%

:success
if not defined NO_PAUSE pause
exit /b 0
