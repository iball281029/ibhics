@echo off
rem ibHiCS guest tools for Windows Server 2012 R2 / 2012 / 2008 R2 and 8.1 / 8 / 7
rem (virtio-win-guest-tools.exe needs Windows 10 or later). Run as Administrator.
rem Drivers come from virtio-win 0.1.189, signed so these Windows trust them.
setlocal enabledelayedexpansion
set ROOT=%~dp0..\
for /f "tokens=4-5 delims=. " %%a in ('ver') do set NT=%%a.%%b
if "%NT%"=="6.3" (set V1=2k12R2& set V2=w8.1)
if "%NT%"=="6.2" (set V1=2k12& set V2=w8)
if "%NT%"=="6.1" (set V1=2k8R2& set V2=w7)
if not defined V1 (echo This script is for Windows 7 / 8 / 8.1 and Server 2008 R2 / 2012 / 2012 R2. & pause & exit /b 1)

echo 1/4 Removing Red Hat driver packages left by a newer virtio-win (Code 52)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = @(pnputil -e); for ($i = 0; $i -lt $p.Count; $i++) { if ($p[$i] -match '(oem\d+\.inf)') { $n = $matches[1]; if (($p[$i..($i+3)] -join ' ') -match 'Red Hat') { Write-Host '  ' $n; pnputil -f -d $n | Out-Null } } }"

echo 2/4 QEMU guest agent...
msiexec /i "%~dp0qemu-ga-x86_64.msi" /qn /norestart

echo 3/4 VirtIO drivers for %V1% / %V2%...
for /d %%D in ("%ROOT%*") do (
  if /i not "%%~nxD"=="viogpudo" if /i not "%%~nxD"=="qxldod" (
    for %%V in (%V1% %V2%) do (
      if exist "%%D\%%V\amd64\*.inf" for %%F in ("%%D\%%V\amd64\*.inf") do (
        echo   %%~nxD\%%V\%%~nxF
        pnputil -i -a "%%F" >nul
      )
    )
  )
)

echo 4/4 Memory balloon service (Windows reports its memory use)...
if exist "%ROOT%Balloon\%V1%\amd64\blnsvr.exe" (
  copy /y "%ROOT%Balloon\%V1%\amd64\blnsvr.exe" "%SystemRoot%\System32\blnsvr.exe" >nul
  "%SystemRoot%\System32\blnsvr.exe" -i
)
echo Done. Restart Windows, then "Switch disks to VirtIO" in the VM's menu if you like.
pause
