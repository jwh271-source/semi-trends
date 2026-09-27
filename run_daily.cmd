@echo off
rem SemiTrends-Daily7am 스케줄 작업용 래퍼.
rem update.py의 stdout/stderr를 로그 파일에 남겨 실패 원인 추적 가능하게 한다.
rem PYTHONIOENCODING=utf-8: 로그 리다이렉트 시 기본 cp949 인코딩으로 인한
rem 유니코드 print 크래시를 막는다 (update.py/fetcher.py 등 자식 전체에 적용).
cd /d C:\Users\2075586\semi-trends
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\update.log
set PYTHONIOENCODING=utf-8
"C:\Users\2075586\AppData\Local\Programs\Python\Python314\python.exe" update.py >> logs\update.log 2>&1
