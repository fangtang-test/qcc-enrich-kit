@echo off
cd /d "%~dp0"
echo ==============================================================
echo   STEP 3  AI REVIEW (DeepSeek)
echo   Reads the step-2 result file, writes the AI review file
echo   Tags: chain A-H, AI level L0-L4, anomaly flag
echo   Only real conflicts get the review flag
echo ==============================================================
python ai_audit.py
pause
