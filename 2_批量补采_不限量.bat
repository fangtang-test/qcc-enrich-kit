@echo off
cd /d "%~dp0"
echo ==============================================================
echo   STEP 2  BATCH COLLECT - crawl every company in the list
echo   Not found -> marked and skipped, never stuck
echo   Ctrl+C anytime; rerun resumes automatically
echo ==============================================================
python qcc_enrich.py
pause
