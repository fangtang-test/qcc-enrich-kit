@echo off
cd /d "%~dp0"
echo ==============================================================
echo   STEP 4  CUSTOM LIST - edit company_list.txt (one per line)
echo   Output: custom_result.csv
echo ==============================================================
python qcc_enrich.py --file=company_list.txt --out=custom_result.csv
pause
