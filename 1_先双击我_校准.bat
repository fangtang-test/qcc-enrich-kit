@echo off
cd /d "%~dp0"
echo ==============================================================
echo   STEP 1  CALIBRATION - query 1 company only
echo   Output: probe_dump.txt (open it to verify the fields)
echo ==============================================================
python qcc_enrich.py --probe
pause
