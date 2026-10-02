@echo off
REM 日常启动一致（无杀进程），守护由 run_server.py 自带 Mutex + instance.json 完成
set PORT=5000
set PYTHON=C:\veighna_studio\python.exe
set PROJECT=C:\Quant_2026\期货执行策略\GreeksDashboard_v0.1
cd /d "%PROJECT%"
"%PYTHON%" run_server.py
pause
