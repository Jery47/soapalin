@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%soapalin.py" %*
) else (
  python "%ROOT%soapalin.py" %*
)
exit /b %ERRORLEVEL%
