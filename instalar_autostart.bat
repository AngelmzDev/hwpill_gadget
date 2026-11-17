@echo off
:: hwpill_gadget - autoarranque (tarea programada al iniciar sesion, con privilegios). Se auto-eleva con UAC.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "DIR=%~dp0"
set "PYW="
if exist "%DIR%venv\Scripts\pythonw.exe" set "PYW=%DIR%venv\Scripts\pythonw.exe"
if not defined PYW for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%P"
if not defined PYW (
  echo No se encontro pythonw. Instala Python 3.11 o superior y vuelve a ejecutar este archivo.
  pause
  exit /b 1
)

echo Creando la tarea programada "hwpill_gadget" (al iniciar sesion, con privilegios)...
schtasks /Create /TN "hwpill_gadget" /SC ONLOGON /RL HIGHEST /F /TR "\"%PYW%\" \"%DIR%gadget.py\""
if %errorlevel% neq 0 (
  echo ERROR al crear la tarea.
  pause
  exit /b 1
)

echo Lanzando el gadget ahora...
schtasks /Run /TN "hwpill_gadget"
echo.
echo Listo. El gadget arrancara solo cada vez que inicies sesion.
echo Para quitarlo: schtasks /Delete /TN "hwpill_gadget" /F
timeout /t 5 >nul
