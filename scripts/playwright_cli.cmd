@echo off
setlocal
if defined USERPROFILE pushd "%USERPROFILE%"
call npx --yes --package @playwright/cli playwright-cli %*
set "holeclaw_exit=%ERRORLEVEL%"
if defined USERPROFILE popd
exit /b %holeclaw_exit%
