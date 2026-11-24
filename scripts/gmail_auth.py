"""지메일 읽기 전용 권한의 갱신 토큰을 한 번 발급받는다.

준비: 구글 클라우드에서 Gmail API를 켜고 "데스크톱 앱" 유형의 OAuth 클라이언트를 만든 뒤
      .env 에 GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET 을 넣는다.
실행: python -m scripts.gmail_auth
      브라우저에서 전용 메일 계정으로 동의하면 GMAIL_REFRESH_TOKEN 값이 출력된다.

요청하는 권한은 gmail.readonly 하나뿐이라 메일을 보내거나 지우거나 읽음 표시를 바꿀 수 없다.
"""
import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import env  # noqa: E402

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class _Handler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Handler.result = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("동의가 끝났습니다. 이 창을 닫고 터미널로 돌아가십시오.".encode("utf-8"))

    def log_message(self, *args):
        pass


def main() -> None:
    client_id, client_secret = env("GMAIL_CLIENT_ID"), env("GMAIL_CLIENT_SECRET")
    if not (client_id and client_secret):
        sys.exit(".env 에 GMAIL_CLIENT_ID 와 GMAIL_CLIENT_SECRET 을 먼저 넣으십시오.")

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("브라우저에서 동의 화면을 엽니다. 열리지 않으면 아래 주소를 직접 여십시오.\n" + url)
    webbrowser.open(url)
    server.handle_request()
    result = _Handler.result
    if result.get("state") != state or "code" not in result:
        sys.exit(f"동의가 완료되지 않았습니다: {result.get('error', '응답 없음')}")

    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "code": result["code"],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as resp:
        token = json.loads(resp.read().decode())
    if "refresh_token" not in token:
        sys.exit("갱신 토큰을 받지 못했습니다. 구글 계정의 앱 접근 권한을 지운 뒤 다시 시도하십시오.")
    print("\n아래 줄을 .env 에 넣으십시오.\nGMAIL_REFRESH_TOKEN=" + token["refresh_token"])
    print("받은 권한:", token.get("scope"))


if __name__ == "__main__":
    main()
