"""카카오 '나에게 보내기'용 refresh_token 1회 발급.

사용:
  1) .env 에 KAKAO_REST_API_KEY 설정
     (클라이언트 시크릿 ON 이면 KAKAO_CLIENT_SECRET 도)
  2) 콘솔 앱 > 플랫폼 키 > REST API 키 에
     Redirect URI http://127.0.0.1:8765/callback 등록
  3) 동의항목 talk_message 를 선택/이용중 동의로 설정
  4) python scripts/kakao_login.py
  5) 브라우저에서 동의 후 출력된 KAKAO_REFRESH_TOKEN 을
     .env / GitHub Secrets 에 저장
"""

from __future__ import annotations

import asyncio
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.kakao import auth_url, exchange_code  # noqa: E402

REDIRECT_URI = "http://127.0.0.1:8765/callback"
HOST, PORT = "127.0.0.1", 8765


class Handler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        Handler.code = (qs.get("code") or [None])[0]
        Handler.error = (qs.get("error") or [None])[0]
        body = (
            "<h2>카카오 연결 완료</h2><p>이 창을 닫고 터미널을 확인하세요.</p>"
            if Handler.code
            else f"<h2>실패</h2><p>{Handler.error}</p>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003
        return


async def main() -> int:
    url = auth_url(REDIRECT_URI)
    print("브라우저에서 카카오 로그인/동의를 진행하세요:")
    print(url)
    webbrowser.open(url)

    server = HTTPServer((HOST, PORT), Handler)
    print(f"콜백 대기중: {REDIRECT_URI}")
    while Handler.code is None and Handler.error is None:
        server.handle_request()

    if Handler.error or not Handler.code:
        print(f"인가 실패: {Handler.error}", file=sys.stderr)
        return 1

    tokens = await exchange_code(Handler.code, REDIRECT_URI)
    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    print("\n===== GitHub Secrets / .env 에 넣을 값 =====")
    print(f"KAKAO_REFRESH_TOKEN={refresh}")
    if access:
        print("(access_token 은 자동 갱신되므로 저장 불필요)")
    print("=========================================")
    if not refresh:
        print("refresh_token 이 없습니다. 동의항목(talk_message)을 확인하세요.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
