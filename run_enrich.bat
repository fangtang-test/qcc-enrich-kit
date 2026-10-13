@echo off
cd /d "%~dp0"
echo ==============================================================
echo   GENERIC ENTRY - add your own args, e.g.
echo   run_enrich.bat --limit=50 --file=company_list.txt
echo ==============================================================
python qcc_enrich.py %*
pause
