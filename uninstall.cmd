@echo off
rem NLC uninstaller wrapper (ASCII only -- see nlc.cmd for why).
rem Passes every argument straight through to uninstall_windows.py.
setlocal EnableExtensions
chcp 65001 >nul 2>nul
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "PYEXE="
for %%C in (python.exe py.exe python3.exe) do (
    if not defined PYEXE (
        where %%C >nul 2>nul && set "PYEXE=%%C"
    )
)

if not defined PYEXE goto :nopython
"%PYEXE%" "%HERE%packaging\uninstall_windows.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:nopython
echo.
echo   [!] Python 3.8+ was not found in PATH.
echo       Install it from https://www.python.org/downloads/windows/
echo       and run this again.
echo.
pause
exit /b 2
