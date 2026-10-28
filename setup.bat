@echo off
rem Sets up YAAAS on Windows: Python, ffmpeg, a venv in .venv, and the Python packages.
rem Double-click it or run it from a terminal:  setup.bat [cpu | gpu]
setlocal EnableExtensions
cd /d "%~dp0"

set "TORCH=auto"
if not defined TORCH_INDEX set "TORCH_INDEX=https://download.pytorch.org/whl/cu128"

:args
if "%~1"=="" goto args_done
if /i "%~1"=="cpu" set "TORCH=cpu"
if /i "%~1"=="--cpu" set "TORCH=cpu"
if /i "%~1"=="gpu" set "TORCH=gpu"
if /i "%~1"=="--gpu" set "TORCH=gpu"
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
if "%~1"=="/?" goto usage
shift
goto args
:args_done

if not exist yaaas.py (
    echo error: yaaas.py isn't next to this script
    goto fail
)

rem ---- Python 3.10 - 3.12 (whisperx and torch lag behind new releases)
call :find_python
if defined PY goto have_python
echo.
echo ==^> No Python 3.10 - 3.12 found, installing 3.12 with winget
where winget >nul 2>&1
if errorlevel 1 goto no_winget
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto have_python
echo.
echo Python was installed, but this window can't see it yet.
echo Close it, open a new one and run setup.bat again.
goto fail

:no_winget
echo.
echo winget isn't available. Install Python 3.12 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then run setup.bat again.
goto fail

:have_python
echo Using: %PY%

%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 echo warning: this Python has no tkinter, so the GUI won't start (use --nogui for the command line)

rem ---- ffmpeg
where ffmpeg >nul 2>&1
if not errorlevel 1 goto have_ffmpeg
echo.
echo ==^> ffmpeg not found, installing it with winget
where winget >nul 2>&1
if errorlevel 1 goto no_ffmpeg
winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
where ffmpeg >nul 2>&1
if not errorlevel 1 goto have_ffmpeg
:no_ffmpeg
echo warning: ffmpeg still isn't available. Install it from https://ffmpeg.org/download.html
echo and make sure ffmpeg.exe is on the PATH, or YAAAS can't convert downloads.
:have_ffmpeg

rem ---- virtual environment
if exist ".venv\Scripts\python.exe" goto have_venv
echo.
echo ==^> Creating .venv
%PY% -m venv .venv
if errorlevel 1 (
    echo error: couldn't create the virtual environment
    goto fail
)
:have_venv
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip wheel
if errorlevel 1 goto pip_failed

rem ---- torch: the default Windows wheel is CPU only, so the GPU build comes from pytorch.org
if /i not "%TORCH%"=="auto" goto torch_chosen
where nvidia-smi >nul 2>&1
if errorlevel 1 (set "TORCH=cpu") else (set "TORCH=gpu")
:torch_chosen
if /i "%TORCH%"=="gpu" goto torch_gpu
echo.
echo ==^> Using the CPU build of torch
goto torch_done
:torch_gpu
echo.
echo ==^> Installing the CUDA build of torch from %TORCH_INDEX%  (a large download)
"%VPY%" -m pip install torch torchaudio --index-url %TORCH_INDEX%
if errorlevel 1 goto pip_failed
:torch_done

echo.
echo ==^> Installing Python packages
"%VPY%" -m pip install yt-dlp ytmusicapi rapidfuzz requests mutagen numpy demucs whisperx faster-whisper
if errorlevel 1 goto pip_failed

echo.
echo ==^> Checking the install
"%VPY%" yaaas.py --check > "%TEMP%\yaaas_check.txt" 2>&1
set "CHECK_RC=%errorlevel%"
type "%TEMP%\yaaas_check.txt"
if not "%CHECK_RC%"=="0" echo warning: the check failed, see above
if /i not "%TORCH%"=="gpu" goto check_done
findstr /c:"cuda available: False" "%TEMP%\yaaas_check.txt" >nul 2>&1
if not errorlevel 1 (
    echo warning: no CUDA GPU is visible to torch. A later install step may have replaced the GPU build.
    echo Fix it with:  %VPY% -m pip install --force-reinstall torch torchaudio --index-url %TORCH_INDEX%
)
:check_done
del "%TEMP%\yaaas_check.txt" >nul 2>&1

echo.
echo Done. Start it with run.bat
exit /b 0

:usage
echo Usage: setup.bat [cpu ^| gpu]
echo.
echo   cpu   use the CPU build of torch
echo   gpu   install the CUDA build of torch from pytorch.org (NVIDIA cards)
echo.
echo With neither it picks gpu when nvidia-smi is found. Set TORCH_INDEX to use a
echo different CUDA build, for example https://download.pytorch.org/whl/cu126
exit /b 0

:pip_failed
echo.
echo error: pip failed, see the messages above
:fail
echo.
pause
exit /b 1

rem ---- helpers (only reached through call)
:find_python
set "PY="
for %%V in (3.12 3.11 3.10) do call :try_python %%V
if defined PY exit /b 0
python -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
exit /b 0

:try_python
if defined PY exit /b 0
py -%1 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -%1"
exit /b 0
