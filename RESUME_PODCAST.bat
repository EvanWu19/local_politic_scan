@echo off
REM Re-enables the morning podcast publish + evening fetch jobs.

echo Enabling LocalPoliticsPublish (07:00 daily podcast)...
schtasks /change /tn "LocalPoliticsPublish" /enable
echo.
echo Enabling LocalPoliticsFetch (22:00 evening prefetch)...
schtasks /change /tn "LocalPoliticsFetch" /enable
echo.
echo Current state:
schtasks /query /tn "LocalPoliticsPublish" /fo LIST | findstr /R "TaskName Status"
schtasks /query /tn "LocalPoliticsFetch" /fo LIST | findstr /R "TaskName Status"
pause
