"""공공데이터포털(data.go.kr) 한국마사회 오픈API 클라이언트.

포털 API의 고질적인 함정을 한 곳에서 흡수한다:
  * 서비스키가 인코딩본/디코딩본 두 가지로 발급되는데, requests가 한 번 더
    인코딩하면 SERVICE_KEY_IS_NOT_REGISTERED_ERROR 가 난다.
  * ``_type=json`` 을 줘도 오류 응답만은 XML(OpenAPI_ServiceResponse)로 온다.
  * 정상 응답이어도 items 가 빈 문자열(``""``)로 오는 경우가 있다.
  * item 이 1건일 때 리스트가 아니라 dict 로 온다.
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import requests

log = logging.getLogger(__name__)

BASE = "https://apis.data.go.kr/B551015"

# 포털이 정상 처리했음을 뜻하는 결과코드들
OK_CODES = {"00", "0"}
# 재시도해도 의미가 없는(=설정이 틀린) 결과코드
FATAL_CODES = {
    "429",  # 호출 한도 — 재시도가 무의미하다
    "30",  # SERVICE_KEY_IS_NOT_REGISTERED_ERROR
    "31",  # DEADLINE_HAS_EXPIRED_ERROR
    "32",  # UNREGISTERED_IP_ERROR
    "22",  # LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR
}


def redact(text: str) -> str:
    """로그·예외 메시지에서 서비스키를 지운다.

    CI 로그는 저장소가 공개되면 함께 공개된다. 예외 메시지에 요청 URL을 그대로
    실으면 쿼리스트링의 serviceKey 가 영구히 노출되므로 반드시 가린다.
    """
    return re.sub(r"(serviceKey=)[^&\s]+", r"\1<redacted>", str(text))


class KraApiError(RuntimeError):
    """포털이 오류 코드를 반환했을 때."""

    def __init__(self, code: str, msg: str, url: str = ""):
        self.code = code
        self.msg = redact(msg)
        self.url = redact(url)
        super().__init__(f"[{code}] {self.msg}" + (f" ({self.url})" if self.url else ""))

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES


def read_service_key() -> str:
    """서비스키를 환경변수에서, 없으면 .env 에서 읽는다.

    환경변수를 우선하므로 CI 는 Secrets 만 넣으면 되고, 로컬은 .env(chmod 600)만
    두면 매번 export 하지 않아도 된다. 키 값 자체는 어떤 경로로도 로그에 남기지
    않는다 — 크론 로그는 사람 눈에 잘 안 띄는 만큼 유출 시 발견도 늦다.
    """
    key = os.environ.get("KRA_SERVICE_KEY", "").strip()
    if key:
        return key
    for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
        env = base / ".env"
        if not env.is_file():
            continue
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, val = line.partition("=")
            if name.strip() == "KRA_SERVICE_KEY":
                return val.strip().strip("'\"")
    return ""


def normalize_service_key(raw: str) -> str:
    """인코딩/디코딩 어느 쪽으로 받아왔든 '디코딩본'으로 통일한다.

    포털은 같은 키를 Encoding/Decoding 두 형태로 보여준다. requests 의 params 에
    넣으면 다시 인코딩되므로, 항상 디코딩본을 넣어야 이중 인코딩을 피할 수 있다.
    """
    key = (raw or "").strip()
    if not key:
        raise ValueError("서비스키가 비어 있습니다. .env 의 KRA_SERVICE_KEY 를 확인하세요.")
    # '%2B','%3D' 같은 퍼센트 이스케이프가 보이면 인코딩본으로 판단하고 되돌린다.
    if re.search(r"%[0-9A-Fa-f]{2}", key):
        key = urllib.parse.unquote(key)
    return key


def _extract_xml_error(text: str) -> Optional[tuple]:
    """XML 형태 오류 응답에서 (코드, 메시지)를 뽑는다. 오류가 아니면 None."""
    if "<" not in text[:200]:
        return None
    code = re.search(r"<returnReasonCode>\s*([^<]+)</returnReasonCode>", text)
    msg = re.search(r"<returnAuthMsg>\s*([^<]+)</returnAuthMsg>", text)
    if code:
        return code.group(1).strip(), (msg.group(1).strip() if msg else "unknown")
    # 서비스 자체가 XML 로 결과를 준 경우(정상)일 수 있으므로 resultCode 도 본다.
    code = re.search(r"<resultCode>\s*([^<]+)</resultCode>", text)
    msg = re.search(r"<resultMsg>\s*([^<]+)</resultMsg>", text)
    if code and code.group(1).strip() not in OK_CODES:
        return code.group(1).strip(), (msg.group(1).strip() if msg else "unknown")
    return None


def _as_list(items: Any) -> List[dict]:
    """items 필드를 항상 dict 리스트로 정규화."""
    if items in (None, "", [], {}):
        return []
    if isinstance(items, dict):
        inner = items.get("item", items)
        if isinstance(inner, dict):
            return [inner]
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        return []
    if isinstance(items, list):
        return [r for r in items if isinstance(r, dict)]
    return []


@dataclass
class KraClient:
    service_key: str
    # 연결과 응답을 따로 잡는다. 포털이 응답하지 않을 때 연결 단계에서 20초씩
    # 붙들리면, 재시도까지 겹쳐 한 날짜에 1분 반이 날아가고 배치가 통째로
    # 타임아웃된다(실제로 CI 에서 그렇게 죽었다). 연결은 못 하면 빨리 포기하고,
    # 연결된 뒤 계산이 오래 걸리는 응답만 넉넉히 기다린다.
    connect_timeout: float = 6.0
    timeout: float = 20.0
    max_retries: int = 4
    pause: float = 0.12          # 연속 호출 사이 간격(포털 초당 호출 보호)
    session: requests.Session = field(default_factory=requests.Session)
    _last_call: float = field(default=0.0, repr=False)

    @classmethod
    def from_env(cls, **kw) -> "KraClient":
        return cls(service_key=normalize_service_key(read_service_key()), **kw)

    def __post_init__(self):
        self.service_key = normalize_service_key(self.service_key)
        self.session.headers.update({"User-Agent": "horseai/1.0 (+data.go.kr open api client)"})

    # ---------------------------------------------------------------- 저수준

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last_call = time.monotonic()

    def raw(self, path: str, params: Dict[str, Any]) -> dict:
        """단일 페이지 호출. 성공 시 response.body 딕셔너리를 돌려준다."""
        url = f"{BASE}/{path.lstrip('/')}"
        q = {k: v for k, v in params.items() if v not in (None, "")}
        q.update({"serviceKey": self.service_key, "_type": "json"})

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(url, params=q,
                                     timeout=(self.connect_timeout, self.timeout))
            except requests.RequestException as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
                continue

            text = r.text or ""
            err = _extract_xml_error(text)
            if err:
                exc = KraApiError(err[0], err[1], url)
                if exc.fatal:
                    raise exc
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            if r.status_code == 429:
                # 일일/초당 호출 한도. 계속 두드리면 차단만 길어지므로 즉시 중단해
                # 호출자가 오늘 작업을 접고 내일 이어가도록 한다.
                raise KraApiError("429", "호출 한도 초과 (data.go.kr 일일 트래픽 제한)", url)
            if r.status_code >= 500:
                last_exc = KraApiError(str(r.status_code), "server error", url)
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                # raise_for_status 는 URL 을 그대로 담아 키를 노출한다.
                raise KraApiError(str(r.status_code), f"HTTP {r.status_code}", url)

            try:
                data = r.json()
            except ValueError:
                last_exc = KraApiError("PARSE", f"JSON 파싱 실패: {text[:200]}", url)
                time.sleep(1.0 * (attempt + 1))
                continue

            resp = data.get("response", data)
            header = resp.get("header", {}) or {}
            code = str(header.get("resultCode", "00"))
            if code not in OK_CODES:
                exc = KraApiError(code, str(header.get("resultMsg", "")), url)
                if exc.fatal:
                    raise exc
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            return resp.get("body", {}) or {}

        # 네트워크 예외(타임아웃 등)도 KraApiError 로 감싸 내보낸다.
        # 호출자가 requests 예외까지 따로 잡아야 한다면, 한 소스의 일시적 실패가
        # 배치 전체를 죽이는 사고가 반복된다.
        if isinstance(last_exc, KraApiError):
            raise last_exc
        raise KraApiError("NETWORK", f"요청 실패: {type(last_exc).__name__}", url) from last_exc

    # ---------------------------------------------------------------- 고수준

    def fetch(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        rows: int = 500,
        max_pages: int = 200,
    ) -> List[dict]:
        """전 페이지를 순회해 레코드 리스트로 돌려준다."""
        return list(self.iter_pages(path, params, rows=rows, max_pages=max_pages))

    def iter_pages(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        rows: int = 500,
        max_pages: int = 200,
    ) -> Iterator[dict]:
        params = dict(params or {})
        page = 1
        seen = 0
        while page <= max_pages:
            body = self.raw(path, {**params, "pageNo": page, "numOfRows": rows})
            records = _as_list(body.get("items"))
            for rec in records:
                yield rec
            seen += len(records)

            total = body.get("totalCount")
            try:
                total = int(total)
            except (TypeError, ValueError):
                total = None

            if not records:
                return
            if total is not None and seen >= total:
                return
            if len(records) < rows:
                return
            page += 1
