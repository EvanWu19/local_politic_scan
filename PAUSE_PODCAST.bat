@echo off
REM Pauses the every-morning podcast publish job and the evening fetch job.
REM Created 2026-04-27. Run as a normal user (no admin needed for /change).
REM To resume later, run RESUME_PODCAST.bat.

echo Disabling LocalPoliticsPublish (07:00 daily podcast)...
schtasks /change /tn "LocalPoliticsPublish" /disable
echo.
echo Disabling LocalPoliticsFetch (22:00 evening prefetch)...
schtasks /change /tn "LocalPoliticsFetch" /disable
echo.
echo Current state:
schtasks /query /tn "LocalPoliticsPublish" /fo LIST | findstr /R "TaskName Status"
schtasks /query /tn "LocalPoliticsFetch" /fo LIST | findstr /R "TaskName Status"
echo.
echo Done. Both jobs are paused. Run RESUME_PODCAST.bat to re-enable.
pause
