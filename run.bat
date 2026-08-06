@echo off
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if not exist .env (
  copy .env.example .env >nul
  echo.
  echo [안내] .env 파일이 생성되었습니다. UNIPASS_API_KEY 를 입력하세요.
  echo.
)
echo.
echo http://127.0.0.1:8787 에서 실행합니다.
echo.
uvicorn app.main:app --reload --host 127.0.0.1 --port 8787
