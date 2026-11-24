"""메일 본문을 모델과 저장소로 넘기기 전에 다듬는다: 인용문 제거, 비밀값 가리기, 언어 판별."""
import re
from html.parser import HTMLParser

MAX_CHARS = 3000
MASK = "[가림]"

_QUOTE_START = [
    re.compile(r"^\s*-{2,}\s*(Original Message|원본 메일|원본 메시지|原始邮件|原始郵件)\s*-{2,}", re.I),
    re.compile(r"^\s*On .{5,80} wrote:\s*$", re.I),
    re.compile(r"^\s*在.{4,60}写道[:：]\s*$"),
    re.compile(r"^\s*.{4,80}님이 작성[:：]?\s*$"),
    re.compile(r"^\s*(From|보낸 사람|보낸사람|发件人|發件人)\s*[:：].+"),
    re.compile(r"^\s*_{8,}\s*$"),
]
_SIGNATURE = re.compile(r"^\s*--\s*$")

_SECRET_WORD = (
    r"password|passwd|pwd|비밀번호|비번|패스워드|암호(?!화)|密码|密碼|口令|"
    r"api[ _-]?key|secret|token|토큰|令牌"
)
# 낱말 뒤 12자 안(한글·한자·공백)에 이어지는 ASCII 문자열을 값 후보로 본다.
# 예: "비밀번호는 Abc123!", "密码请设为 Abc123!", "password: abc"
_SECRET_KV = re.compile(
    r"(?i)(?P<key>" + _SECRET_WORD + r")"
    r"(?P<mid>[^\x21-\x7e\n]{0,12}?[:=]?[^\x21-\x7e\n]{0,2}?)"
    r"(?P<val>[\x21-\x7e]{4,})"
)
_NOT_SECRET = re.compile(r"^[\d.:/\-,()%]+$")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]{16,}=*")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}
    _SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_quoted(text: str) -> str:
    """답장에 딸려 온 이전 글과 서명을 잘라 낸다."""
    kept = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _SIGNATURE.match(line):
            break
        if any(p.match(line) for p in _QUOTE_START):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line.rstrip())
    out = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _looks_secret(value: str) -> bool:
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    has_symbol = any(not c.isalnum() for c in value)
    return (has_digit and (has_alpha or has_symbol)) or (has_alpha and has_symbol)


def mask_secrets(text: str) -> tuple[str, int]:
    count = 0

    def kv(m: re.Match) -> str:
        nonlocal count
        value = m.group("val")
        has_sep = any(c in m.group("mid") for c in ":=：")
        if _NOT_SECRET.match(value):
            return m.group(0)
        if has_sep or _looks_secret(value):
            count += 1
            return m.group("key") + m.group("mid") + MASK
        return m.group(0)

    def plain(_m: re.Match) -> str:
        nonlocal count
        count += 1
        return MASK

    def token(m: re.Match) -> str:
        nonlocal count
        value = m.group(0)
        mixed = any(c.isdigit() for c in value) and any(c.islower() for c in value) \
            and any(c.isupper() for c in value)
        if _HEX_ONLY.match(value) or not mixed:
            return value
        count += 1
        return MASK

    text = _PRIVATE_KEY.sub(plain, text)
    text = _BEARER.sub(plain, text)
    text = _SECRET_KV.sub(kv, text)
    text = _LONG_TOKEN.sub(token, text)
    return text, count


def detect_lang(text: str) -> str:
    hangul = sum(1 for c in text if "가" <= c <= "힣")
    han = sum(1 for c in text if "一" <= c <= "鿿")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    if hangul and hangul >= han * 0.3:
        return "ko"
    if han:
        return "zh"
    if latin:
        return "en"
    return "unknown"


def hangul_ratio(text: str) -> float:
    """한글·한자 글자 가운데 한글의 비율. 따옴표로 인용한 부분은 세지 않는다."""
    body = re.sub(r"[\"“”‘’「」『』《》'][^\"“”‘’「」『』《》']{0,60}[\"“”‘’「」『』《》']", "", text)
    hangul = sum(1 for c in body if "가" <= c <= "힣")
    han = sum(1 for c in body if "一" <= c <= "鿿")
    total = hangul + han
    return hangul / total if total else 0.0


def clean_body(raw_text: str, max_chars: int = MAX_CHARS) -> dict:
    text = strip_quoted(raw_text or "")
    text, masked = mask_secrets(text)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"text": text, "masked": masked, "truncated": truncated, "lang": detect_lang(text)}
