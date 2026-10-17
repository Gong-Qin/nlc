@echo off
rem ===========================================================================
rem  NLC - Natural Language Command-line  --  portable launcher (Windows)
rem
rem  Double-click this file to open the GUI.  No install needed.
rem  This script is deliberately ASCII-only: cmd.exe reads batch files in the
rem  OEM code page, and mixing Chinese text in here causes garbled output on
rem  non-Chinese Windows.  All user-facing Chinese text lives in Python instead.
rem
rem  The window title is set from Python (see nlc/gui.py APP_TITLE), not here --
rem  putting it in this file would be exactly the mistake this comment warns of.
rem
rem  Usage from a terminal:
rem      nlc.cmd                     open the GUI
rem      nlc.cmd tr "disk usage"     translate one sentence
rem      nlc.cmd risk "del /f /s /q C:\"
rem ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>nul

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

rem --- find a Python that can actually import tkinter -------------------------
rem
rem 2026-10-03: do NOT just take the first "python.exe" in PATH.  On this
rem machine the WorkBuddy-managed Python 3.13 comes first in PATH and it has
rem NO tkinter, so "nlc.cmd" picked it and the GUI died with
rem "No module named 'tkinter'".  So we actually ASK each candidate whether it
rem can import tkinter, and only accept one that says yes.  A wrong Python is
rem worse than no Python: it fails at the moment the user double-clicks.
set "PYEXE="
for %%C in (python.exe py.exe python3.exe) do (
    if not defined PYEXE (
        for /f "usebackq delims=" %%P in (`where %%C 2^>nul`) do (
            if not defined PYEXE (
                rem %%~P is a full path; probe it for tkinter
                "%%~P" -c "import tkinter,sys" >nul 2>nul && set "PYEXE=%%~P"
            )
        )
    )
)

rem 3rd try: "py -3" launcher, asking it for an interpreter that has tkinter
if not defined PYEXE (
    where py.exe >nul 2>nul && (
        for /f "usebackq delims=" %%P in (`py -3 -c "import sys,tkinter;print(sys.executable)" 2^>nul`) do (
            if not defined PYEXE set "PYEXE=%%P"
        )
    )
)

if not defined PYEXE goto :nopython

rem --- run from this directory (python -m nlc needs cwd on sys.path) ---------
pushd "%HERE%" >nul 2>nul
if "%~1"=="" (
    "%PYEXE%" -m nlc gui
) else (
    "%PYEXE%" -m nlc %*
)
set "RC=%ERRORLEVEL%"
popd >nul 2>nul
exit /b %RC%

:nopython
echo.
echo   [!] NLC needs Python 3.8 or newer WITH tkinter, but none was found.
echo.
echo   "With tkinter" matters: the graphical interface is a tkinter app, and a
echo   Python built without tkinter cannot show it.
echo.
echo   Fix: install Python from https://www.python.org/downloads/windows/
echo         (tick "Add python.exe to PATH" during setup -- the official
echo          installer already bundles tkinter), then run this again.
echo.
echo   Already have several Pythons? NLC picks the first one that can import
echo   tkinter, so you do not need to uninstall anything.
echo.
echo   Note: NLC uses only the Python standard library -- no extra packages.
echo.
pause
exit /b 2
