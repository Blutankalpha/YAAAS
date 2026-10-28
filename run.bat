@echo off
rem Starts YAAAS from the venv made by setup.bat.
rem   run.bat                           opens the GUI
rem   run.bat --nogui songs.txt -o D:\Music
rem   run.bat --nogui --scan D:\Music   --nogui switches to the command line version
rem   run.bat --nogui --help            lists the command line options
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo No environment yet. Run setup.bat first.
    pause
    exit /b 1
)

rem ffmpeg may have been installed by winget after this window was opened
where ffmpeg >nul 2>&1
if errorlevel 1 set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
rem CUDA libraries that pip put next to torch (faster-whisper needs to find them)
set "PATH=%CD%\.venv\Lib\site-packages\torch\lib;%PATH%"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem the GUI is the default; --nogui (or --help) hands everything else to the command line version
set "GUI=1"
set "ARGS="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--nogui" goto p_nogui
if /i "%~1"=="--gui" goto p_next
if /i "%~1"=="--help" goto p_help
if /i "%~1"=="-h" goto p_help
set ARGS=%ARGS% %1
goto p_next
:p_nogui
set "GUI=0"
goto p_next
:p_help
set "GUI=0"
set ARGS=%ARGS% %1
:p_next
shift
goto parse
:parsed

if "%GUI%"=="0" goto cli
if defined ARGS echo note: the GUI ignores arguments, add --nogui to use the command line version 1>&2
"%PY%" yaaas_gui.py
exit /b %errorlevel%

:cli
"%PY%" yaaas.py %ARGS%
exit /b %errorlevel%
