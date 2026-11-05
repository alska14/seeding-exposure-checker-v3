@echo off
chcp 65001 >nul
cd /d "%~dp0"
python run_helper.py
if errorlevel 9009 (
  echo.
  echo Python 을 찾을 수 없습니다. https://www.python.org 에서 Python 3.10 이상을 설치한 뒤 다시 실행하세요.
  pause
)
