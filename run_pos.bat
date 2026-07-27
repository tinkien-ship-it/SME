@echo off
cd /d %~dp0
call pos_env\Scripts\activate
python app.py
pause