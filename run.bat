@echo off
cd /d "%~dp0"
python linkedin_jobs.py %*
if errorlevel 1 pause
