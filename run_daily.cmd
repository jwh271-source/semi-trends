@echo off
rem SemiTrends-Daily7am 스케줄 작업용 래퍼.
rem update.py의 stdout/stderr를 로그 파일에 남겨 실패 원인 추적 가능하게 한다.
cd /d C:\Users\2075586\semi-trends
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\update.log
"C:\Users\2075586\AppData\Local\Programs\Python\Python314\python.exe" update.py >> logs\update.log 2>&1
