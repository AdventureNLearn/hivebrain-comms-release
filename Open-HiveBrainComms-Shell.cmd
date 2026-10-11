@echo off
rem Detached launch. Closing this console does not close the app.
rem If the app is already open, bring that window forward instead of starting another.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Focus-Or-Start-Shell.ps1"
