@echo off
rem onec-lite: запуск веб-админки одним двойным кликом (пути задаются в браузере и сохраняются).
rem Требуется только uv: winget install astral-sh.uv  (или https://docs.astral.sh/uv/)
setlocal
where uv >nul 2>nul
if %errorlevel%==0 (
  uv run --directory "%~dp0." onec-lite admin %* || pause
  exit /b %errorlevel%
)
if exist "%USERPROFILE%\.local\bin\uv.exe" (
  "%USERPROFILE%\.local\bin\uv.exe" run --directory "%~dp0." onec-lite admin %* || pause
  exit /b %errorlevel%
)
echo uv не найден. Установите: winget install astral-sh.uv
pause
exit /b 1
