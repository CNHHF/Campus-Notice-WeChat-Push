@echo off
REM ============================================================
REM  start-all.bat - one-click start for the notice push pipeline
REM    1) OpenClaw gateway   2) school-radar   3) weixin-bridge
REM  Safe to run again: parts that are already running are skipped.
REM
REM  Copy this file to your deploy-work folder, fix the paths below
REM  if they do not match your machine, then use it directly or via
REM  the desktop shortcut created by make-desktop-shortcut.vbs.
REM ============================================================
set "GATEWAY_VBS=%USERPROFILE%\.openclaw\gateway.vbs"
set "RADAR_DIR=%USERPROFILE%\deploy-work\school-radar-pkg\school-radar"
set "BRIDGE_VBS=%USERPROFILE%\deploy-work\weixin-bridge\weixin-bridge.vbs"

echo [1/3] OpenClaw gateway (ws://127.0.0.1:18789)...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -like '*openclaw*index.js gateway*' } | Select-Object -First 1; if ($p) { Write-Host ('  already running (pid ' + $p.ProcessId + ')') } else { Start-Process -WindowStyle Hidden -FilePath 'wscript.exe' -ArgumentList '%GATEWAY_VBS%'; Write-Host '  starting (hidden, takes a few seconds)...' }"
echo [2/3] school-radar (http://127.0.0.1:8765)...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"name='school-radar.exe'\" | Select-Object -First 1; if ($p) { Write-Host ('  already running (pid ' + $p.ProcessId + ')') } else { Start-Process -FilePath '%RADAR_DIR%\school-radar.exe' -WorkingDirectory '%RADAR_DIR%'; Write-Host '  starting...' }"
echo [3/3] weixin-bridge (push loop)...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { $_.CommandLine -like '*weixin-bridge*bridge.py*' } | Select-Object -First 1; if ($p) { Write-Host ('  already running (pid ' + $p.ProcessId + ')') } else { Start-Process -WindowStyle Hidden -FilePath 'wscript.exe' -ArgumentList '%BRIDGE_VBS%'; Write-Host '  starting...' }"
echo.
echo Done. Push log: deploy-work\weixin-bridge\bridge.log
pause
