@echo off
cd /d "%~dp0"
echo 安裝相依套件...
python -m pip install -r requirements.txt -q
echo.
echo 啟動台股空頭訊號監控儀表板...
echo 請開啟瀏覽器前往: http://localhost:8000
echo 按 Ctrl+C 停止服務
echo.
python main.py
pause
