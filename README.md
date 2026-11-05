# es-search-mcp

백엔드 팀이 만든 **Elasticsearch 검색 API**를 [FastMCP](https://gofastmcp.com) 서버로
감싸서, MCP 클라이언트(Claude Desktop, Claude Code 등)가 도구로 호출할 수 있게 만든
프로젝트입니다.

백엔드의 두 엔드포인트를 각각 하나의 MCP 도구로 노출합니다.

| MCP 도구 | 백엔드 엔드포인트 | 하는 일 |
| --- | --- | --- |
| `get_indices` | `GET /indices` | 검색 가능한 인덱스 이름과 설명을 반환 |
| `retrieve` | `POST /retrieve-{method}` | 선택한 검색 전략으로 여러 인덱스에서 문서를 검색 |

검색 전략은 백엔드에서 각각 별도의 엔드포인트(`/retrieve-bm25`, `/retrieve-knn`,
`/retrieve-cc`, `/retrieve-rrf`)로 제공되며, MCP에서는 하나의 도구에 `method` 인자로
노출합니다. 기본값은 `rrf`입니다. `retrieve`는 인덱스를 여러 개 받을 수 있습니다.

두 도구 모두 읽기 전용(`readOnlyHint`)이며, 구조화 로깅과 중앙집중식 에러 처리를
거칩니다. **API 키는 서버에 저장되지 않고**, MCP 클라이언트가 요청 헤더로 보낸 값을
그대로 백엔드로 전달합니다 (§3 참고).

---

## 1. 아키텍처

```
MCP 클라이언트
      │  (stdio / http)
      ▼
┌──────────────────────────────────────────────┐
│ server.py          컴포지션 루트              │
│  ├─ middleware.py  호출 로깅 · 에러 백스톱     │
│  └─ tools/         MCP 도구 정의              │
│       ├─ indices.py     get_indices           │
│       └─ retrieve.py    retrieve              │
│            │                                  │
│            ├─ credentials.py 호출자 키 추출    │
│            ├─ errors.py     도구 에러 경계     │
│            ▼                                  │
│      backend/client.py  HTTP · 재시도 · 에러변환│
│            │                                  │
│      models.py      응답 정규화 (Pydantic)     │
└──────────────────────────────────────────────┘
      │  HTTPS
      ▼
백엔드 검색 API ──▶ Elasticsearch
```

계층별 책임은 다음과 같습니다.

| 모듈 | 책임 |
| --- | --- |
| `config.py` | 환경변수 기반 설정 (`ES_MCP_*`), 검증과 정규화 |
| `logging.py` | 구조화 로깅, 호출 상관관계(correlation) 컨텍스트, 민감정보 마스킹 |
| `credentials.py` | 요청 헤더에서 호출자의 키 2개를 읽어 백엔드로 전달 |
| `exceptions.py` | 도메인 예외 계층과 안정적인 에러 코드 |
| `errors.py` | 도메인 예외 → 클라이언트에게 보낼 `ToolError` 변환 경계 |
| `models.py` | 백엔드 응답을 MCP 클라이언트용 스키마로 정규화 |
| `backend/client.py` | HTTP 호출, 타임아웃, 재시도, 상태코드 → 도메인 예외 매핑 |
| `middleware.py` | 도구 호출 단위 로깅, 도구 바깥에서 난 에러의 백스톱 |
| `tools/` | MCP 도구 정의 (입력 검증 + 백엔드 호출 + 결과 로깅) |
| `server.py` | 설정·클라이언트·미들웨어·도구를 조립하는 유일한 진입점 |
| `scripts/fake_backend.py` | 로컬 확인용 가짜 백엔드 (표준 라이브러리만 사용) |
| `Dockerfile` | 2단계 빌드 · 비루트 실행 이미지 (§6) |

**의존성은 명시적으로 주입합니다.** 도구는 `register(mcp, client, settings)` 형태로
등록되며 전역 상태를 참조하지 않습니다. 덕분에 테스트에서 스텁 클라이언트를 그대로
꽂아 넣을 수 있습니다.

---

## 2. 백엔드 API 계약

이 서버가 기대하는 백엔드 응답 형태입니다. 백엔드가 다른 이름을 쓰더라도
`models.py`가 흔한 별칭을 흡수하므로, 대부분의 경우 코드 수정 없이 붙습니다.

### `GET /indices`

```json
[
  { "name": "faq", "description": "고객 FAQ 문서", "document_count": 1240 },
  { "name": "manuals", "description": "제품 매뉴얼" }
]
```

* 허용 별칭: `name` / `index` / `index_name`, `description` / `desc` / `summary`,
  `document_count` / `doc_count` / `count`
* 봉투(envelope) 형태도 허용: `{"indices": [...]}`, `{"items": [...]}`,
  `{"data": [...]}`, `{"results": [...]}`

### `POST /retrieve-{bm25,knn,cc,rrf}`

검색 전략마다 별도의 엔드포인트가 있습니다. **전략은 경로로 선택되며 요청 본문에는
들어가지 않습니다.**

| `method` | 엔드포인트 | 성격 |
| --- | --- | --- |
| `rrf` *(기본값)* | `POST /retrieve-rrf` | 어휘 + 벡터 결과의 순위 융합 (RRF) |
| `bm25` | `POST /retrieve-bm25` | 어휘 기반 키워드 매칭 |
| `knn` | `POST /retrieve-knn` | 밀집 벡터 의미 검색 |
| `cc` | `POST /retrieve-cc` | 어휘·벡터 점수의 볼록 결합 |

네 엔드포인트의 요청/응답 형태는 동일합니다. 요청 본문:

```json
{
  "Index_name": "faq,manuals",
  "query": "환불 규정",
  "top_k": 5,
  "permission_groups": ["rag-public"]
}
```

본문 필드에 두 가지 주의점이 있습니다.

* **`Index_name`** — 대문자 `I`이며, 인덱스가 여러 개여도 **리스트가 아니라 콤마로 이은
  하나의 문자열**로 보냅니다. 도구 쪽 인자는 `indices: list[str]`이고 이 변환은
  `backend/client.py`의 `INDEX_FIELD` 근처에서만 일어납니다.
* **`permission_groups`** — 호출자가 준 값을 가공 없이 그대로 싣습니다. 주지 않았으면
  `ES_MCP_DEFAULT_PERMISSION_GROUPS`(기본 `["rag-public"]`)가 들어갑니다. 항상 포함되는
  필드입니다.

응답:

```json
{
  "documents": [
    { "id": "doc-1", "score": 1.82, "content": "환불은 ...", "metadata": { "lang": "ko" } }
  ]
}
```

* 허용 별칭: `id` / `_id` / `doc_id`, `score` / `_score`, `content` / `text` / `body` /
  `chunk`, `metadata` / `meta` / `_source`
* 봉투 형태도 허용: `{"documents": [...]}`, `{"hits": [...]}`, `{"results": [...]}`,
  Elasticsearch 원형인 `{"hits": {"hits": [...]}}`

파싱할 수 없는 응답은 `BACKEND_INVALID_PAYLOAD` 에러가 됩니다. 백엔드가 위 형태에서
많이 벗어난다면 `models.py`의 별칭만 손보면 됩니다.

경로 규칙이 다르다면 `ES_MCP_BACKEND_RETRIEVE_PATH_TEMPLATE`만 바꾸면 됩니다
(예: `/v2/search/{method}`). `{method}` 자리표시자가 없으면 기동 시 거부됩니다.

---

## 3. 인증: 호출자 키 전달

이 서버는 **자체 자격증명을 갖지 않습니다.** 백엔드가 인증하는 대상은 이 서버가 아니라
최종 사용자이므로, MCP 클라이언트가 매 요청 헤더로 키 2개를 보내고 서버는 그것을 그대로
백엔드로 전달합니다.

```
MCP 클라이언트 ──[ Authorization, X-API-Key ]──▶ MCP 서버 ──(그대로)──▶ 백엔드 API
```

* 키는 **저장하지도, 캐시하지도, 기본값을 두지도 않습니다.** 서버가 보관하지 않는
  자격증명은 서버가 유출할 수도 없습니다.
* 요청마다 새로 읽으므로 동시에 들어온 호출자끼리 키가 섞이지 않습니다.
* 도구의 **입력 인자가 아닙니다.** 키가 인자였다면 LLM 컨텍스트와 대화 로그에 그대로
  남게 됩니다.
* 로그에도 값이 남지 않습니다 (`SENSITIVE_KEYS` 마스킹).

헤더 이름은 설정으로 바꿀 수 있습니다.

| 환경변수 | 기본값 |
| --- | --- |
| `ES_MCP_AUTH_HEADER` | `authorization` |
| `ES_MCP_API_KEY_HEADER` | `x-api-key` |

둘 중 하나라도 없거나 비어 있으면 백엔드를 호출하지 않고 바로 실패합니다.

```
[MISSING_CREDENTIALS] This server forwards the caller's credentials to the search
backend, but the request is missing: x-api-key. ...
```

> **stdio 트랜스포트에서는 동작하지 않습니다.** 헤더는 HTTP 요청에만 존재하므로,
> `stdio`로 띄우면 모든 도구 호출이 `MISSING_CREDENTIALS`로 실패합니다. 그래서 기본
> 트랜스포트가 `http`입니다. stdio를 써야 한다면 키를 환경변수로 받도록
> `credentials.py`의 `resolve_credentials()`에 폴백을 추가하면 됩니다.

---

## 4. 설정

모든 설정은 `ES_MCP_` 접두사를 가진 환경변수 또는 `.env` 파일로 주입합니다.
전체 목록과 기본값은 [`.env.example`](.env.example)에 있습니다.

```bash
cp .env.example .env
```

자주 쓰는 값:

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `ES_MCP_BACKEND_BASE_URL` | `http://localhost:8000` | 백엔드 API 주소 |
| `ES_MCP_BACKEND_RETRIEVE_PATH_TEMPLATE` | `/retrieve-{method}` | 검색 엔드포인트 경로 규칙 |
| `ES_MCP_AUTH_HEADER` / `ES_MCP_API_KEY_HEADER` | `authorization` / `x-api-key` | 전달할 자격증명 헤더 이름 |
| `ES_MCP_REQUEST_TIMEOUT` | `30.0` | 요청당 타임아웃(초) |
| `ES_MCP_MAX_RETRIES` | `2` | 일시적 실패에 대한 재시도 횟수 |
| `ES_MCP_DEFAULT_TOP_K` / `ES_MCP_MAX_TOP_K` | `5` / `50` | 검색 결과 개수 기본값과 상한 |
| `ES_MCP_DEFAULT_RETRIEVE_METHOD` | `rrf` | `method`를 지정하지 않았을 때의 전략 |
| `ES_MCP_DEFAULT_PERMISSION_GROUPS` | `["rag-public"]` | `permission_groups`를 주지 않았을 때의 값 |
| `ES_MCP_LOG_LEVEL` / `ES_MCP_LOG_FORMAT` | `INFO` / `json` | 로그 레벨과 형식 |
| `ES_MCP_MASK_ERROR_DETAILS` | `true` | 예기치 못한 에러의 내부 정보를 감출지 여부 |
| `ES_MCP_TRANSPORT` | `http` | `http`, `sse`, `stdio` (stdio는 자격증명 전달 불가) |

API 키는 여기에 넣지 않습니다 — §3을 보세요.

---

## 5. 실행

### 설치

`uv`를 쓰는 경우:

```bash
uv sync --all-groups
```

**표준 `python` / `pip`만 쓰는 경우** (uv 불필요):

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

`pip install -e .`는 런타임 의존성만 설치합니다. 테스트·린트까지 돌리려면 개발 도구를
따로 넣으세요 (pip 25.1 이상이면 `pip install --group dev`로도 됩니다).

```bash
pip install pytest pytest-asyncio respx ruff mypy
```

### 기동

```bash
ES_MCP_BACKEND_BASE_URL=https://search-api.internal \
ES_MCP_TRANSPORT=http ES_MCP_HOST=127.0.0.1 ES_MCP_PORT=8080 \
  python -m es_search_mcp
```

설치하면 콘솔 스크립트도 생기므로 `es-search-mcp`로도 동일하게 뜹니다
(`uv` 사용 시에는 `uv run es-search-mcp`).

환경변수를 매번 나열하기 번거로우면 `.env`에 넣으면 됩니다.

```bash
cp .env.example .env    # 값을 채운 뒤
python -m es_search_mcp
```

기동에 성공하면 엔드포인트는 **`http://127.0.0.1:8080/mcp/`** 입니다.

### 로컬에서 통째로 굴려보기

백엔드가 아직 없어도 동봉된 가짜 백엔드로 전 구간을 확인할 수 있습니다. 표준
라이브러리만 쓰므로 추가 설치가 필요 없습니다.

```bash
# 터미널 1 — 가짜 백엔드 (인덱스 목록 + 4개 retrieve 엔드포인트)
python scripts/fake_backend.py 9000

# 터미널 2 — MCP 서버
ES_MCP_BACKEND_BASE_URL=http://127.0.0.1:9000 \
ES_MCP_TRANSPORT=http ES_MCP_PORT=8080 ES_MCP_LOG_FORMAT=text \
  python -m es_search_mcp
```

가짜 백엔드는 받은 자격증명 헤더를 그대로 찍어주므로, 키가 제대로 전달되는지 눈으로
확인할 수 있습니다.

```
[backend] POST /retrieve-bm25  Authorization='Bearer MY-TOKEN'  X-API-Key='MY-KEY'
```

### MCP 클라이언트 등록

자격증명은 클라이언트가 헤더로 보냅니다.

```json
{
  "mcpServers": {
    "es-search": {
      "url": "http://127.0.0.1:8080/mcp/",
      "headers": {
        "Authorization": "Bearer <첫 번째 키>",
        "X-API-Key": "<두 번째 키>"
      }
    }
  }
}
```

### 호출 예시 (Python)

가장 간단한 확인 방법입니다.

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    "http://127.0.0.1:8080/mcp/",
    headers={"Authorization": "Bearer MY-TOKEN", "X-API-Key": "MY-KEY"},
)


async def main():
    async with Client(transport) as client:
        print([t.name for t in await client.list_tools()])
        result = await client.call_tool(
            "retrieve",
            {"indices": ["faq", "manuals"], "query": "환불 규정", "method": "bm25", "top_k": 2},
        )
        print(result.structured_content)


asyncio.run(main())
```

### 호출 예시 (curl)

MCP Streamable HTTP는 **세션**을 씁니다. `initialize` 응답의 `mcp-session-id`를 받아
이후 요청에 붙여야 하고, 그 사이에 `notifications/initialized`를 한 번 보내야 합니다.
(경로는 `/mcp` — `/mcp/`로 보내면 307로 리다이렉트되므로 `curl -L`이 필요합니다.)

```bash
AUTH=(-H "Authorization: Bearer MY-TOKEN" -H "X-API-Key: MY-KEY"
      -H "Content-Type: application/json"
      -H "Accept: application/json, text/event-stream")

# 1) 세션 열기 — 응답 헤더에서 세션 id를 꺼낸다
SID=$(curl -sS -D - -o /dev/null -X POST http://127.0.0.1:8080/mcp "${AUTH[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2025-06-18","capabilities":{},
       "clientInfo":{"name":"curl","version":"1"}}}' \
  | tr -d '\r' | sed -n 's/^mcp-session-id: //Ip')

# 2) 초기화 완료 통지
curl -sS -o /dev/null -X POST http://127.0.0.1:8080/mcp "${AUTH[@]}" \
  -H "mcp-session-id: $SID" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 3) 도구 호출 (응답은 SSE이므로 `data:` 줄만 추린다)
curl -sS -N -X POST http://127.0.0.1:8080/mcp "${AUTH[@]}" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
       "name":"retrieve",
       "arguments":{"indices":["faq","manuals"],"query":"환불 규정","method":"bm25"}}}' \
  | sed -n 's/^data: //p'
```

---

## 6. Docker

```bash
docker build -t es-search-mcp:0.1.0 .

docker run --rm -p 8080:8080 \
  -e ES_MCP_BACKEND_BASE_URL=https://search-api.internal \
  es-search-mcp:0.1.0
```

컨테이너는 `http://<host>:8080/mcp/`에서 MCP Streamable HTTP를 서빙합니다.

### 이미지 설계

| 결정 | 이유 |
| --- | --- |
| 2단계 빌드 | 빌더에만 `uv`와 빌드 도구가 들어가고, 런타임 이미지에는 venv만 남습니다 |
| 의존성 → 소스 순으로 COPY | `uv.lock`이 바뀌지 않으면 의존성 레이어를 재사용하므로, 코드 수정 시 빌드가 빠릅니다 |
| `uv sync --frozen` | `uv.lock`에 고정된 버전 그대로 설치합니다. 빌드마다 결과가 달라지지 않습니다 |
| `uv`를 PyPI에서 설치 | 레지스트리를 하나 더(ghcr 등) 뚫지 않아도 됩니다. 베이스 이미지 + 패키지 인덱스면 충분합니다 |
| `UV_PYTHON_DOWNLOADS=never` | uv가 자체 파이썬을 내려받으면 venv가 런타임 스테이지에 없는 인터프리터를 가리키게 됩니다 |
| `--no-editable` | venv 안에 실제 사본이 설치되므로, 런타임 스테이지는 `/app/.venv`만 있으면 됩니다 |
| 비루트(uid 10001) 실행 | 프로세스가 필요한 건 자기 코드 읽기와 소켓 열기뿐입니다 |
| ENTRYPOINT exec 형식 | 서버가 PID 1이 되어 `docker stop`의 SIGTERM을 직접 받고 연결을 정리합니다 |

**`ES_MCP_HOST`는 이미지에서 `0.0.0.0`으로 바꿔 둡니다.** 앱 기본값인 `127.0.0.1`은
컨테이너 안에서만 닿을 수 있어 `-p`로 포트를 열어도 접속되지 않습니다.

파이썬 버전은 빌드 인자입니다.

```bash
docker build --build-arg PYTHON_VERSION=3.13 -t es-search-mcp:0.1.0 .
```

### 설정 주입

이미지에는 **비밀값이 들어 있지 않습니다.** API 키는 빌드 타임에도, 환경변수로도
필요하지 않습니다 — 호출자가 매 요청 헤더로 보냅니다 (§3).

주입해야 할 것은 백엔드 주소 정도이고, 나머지는 §4의 `ES_MCP_*`를 `-e`나
`--env-file`로 덮어쓰면 됩니다.

```bash
docker run --rm -p 8080:8080 --env-file .env es-search-mcp:0.1.0
```

### 헬스체크

`HEALTHCHECK`는 포트가 열려 있는지만 확인합니다. MCP 호출에는 세션과 호출자
자격증명이 필요해서, 인증 없이 찔러볼 수 있는 엔드포인트가 없기 때문입니다.
백엔드 도달 가능성까지 보는 readiness 프로브가 필요하면 앱에 `/health` 라우트를
추가하고 헬스체크를 그쪽으로 바꾸세요.

### 로그

로그는 stderr로 나가는 JSON 한 줄 단위라 `docker logs`와 로그 수집기가 그대로
읽습니다. 사람이 볼 용도면 `-e ES_MCP_LOG_FORMAT=text`.

```bash
docker logs -f <container> | jq 'select(.event | startswith("tool."))'
```

---

## 7. 커스텀 로깅

`logging.py`가 담당하며, 설계상 두 가지 제약을 지킵니다.

1. **로그는 항상 stderr로 나갑니다.** `stdio` 트랜스포트에서 stdout은 JSON-RPC
   프로토콜 전용이라, 한 줄이라도 섞이면 통신이 깨집니다. `StderrHandler`는 매 출력마다
   `sys.stderr`를 다시 조회하므로, 프로세스가 스트림을 교체해도 안전합니다.
2. **도구 호출 하나가 상관관계의 단위입니다.** `request_id`, `tool_name`, `client_id`를
   `ContextVar`에 담아 두고, 호출이 살아 있는 동안 발생한 모든 레코드에 자동으로
   덧붙입니다. 로거를 인자로 들고 다닐 필요가 없습니다.

`ES_MCP_LOG_FORMAT=json`일 때 한 줄이 하나의 JSON 객체입니다.

```json
{"timestamp":"2026-09-03T00:34:57Z","level":"INFO","logger":"es_search_mcp.middleware",
 "event":"tool.call.start","arguments":{"indices":["faq"],"query":"환불"},
 "request_id":"8accd74976a34db7","tool_name":"retrieve"}
```

주요 이벤트:

| 이벤트 | 의미 |
| --- | --- |
| `server.starting` / `server.stopped` | 라이프스팬 시작·종료 |
| `tool.call.start` / `tool.call.finished` | 도구 호출 시작·성공 (`duration_ms` 포함) |
| `tool.call.failed` / `tool.call.crashed` | 분류된 실패 / 예상 못 한 실패 |
| `backend.request` / `backend.response` | 백엔드 호출 (`status_code`, `elapsed_ms`) |
| `backend.retry` | 재시도 (`attempt`, `delay_seconds`, `error_code`) |
| `tool.error` / `tool.unhandled_error` | 에러 경계가 잡은 실패의 상세 |
| `tool.retrieve.result` | 사용한 `indices`, `method`, `top_k`, `permission_groups`, 결과 개수 |

`request_id`는 백엔드로 나가는 요청의 `X-Request-ID` 헤더로도 전달되므로, MCP 서버
로그와 백엔드 로그를 같은 키로 이어 붙일 수 있습니다.

`authorization`, `x-api-key`, `api_key`, `password`, `secret`, `token` 필드는 출력
직전에 `***`로 치환됩니다 (`SENSITIVE_KEYS`). 호출자의 키는 애초에 도구 인자가 아니라
헤더로 오기 때문에 `tool.call.start`의 `arguments`에도 들어가지 않습니다.

사용법:

```python
from es_search_mcp.logging import get_logger

logger = get_logger(__name__)
logger.info("backend.request", fields={"method": "GET", "path": "/indices"})
```

---

## 8. 커스텀 에러 처리

### 예외 계층

모든 실패는 `SearchMCPError`의 하위 타입으로 표현하고, 각각 **안정적인 코드**와
**재시도 가능 여부**를 갖습니다.

| 에러 코드 | 언제 | 재시도 |
| --- | --- | --- |
| `INVALID_INPUT` | 도구 인자가 잘못됨 (백엔드 호출 전에 차단) | ✗ |
| `MISSING_CREDENTIALS` | 호출자가 자격증명 헤더를 보내지 않음 | ✗ |
| `CONFIGURATION_ERROR` | 서버 설정이 잘못됨 | ✗ |
| `BACKEND_UNAVAILABLE` | 백엔드에 연결 불가 | ✓ |
| `BACKEND_TIMEOUT` | 타임아웃 | ✓ |
| `BACKEND_UNAUTHORIZED` | 401 / 403 | ✗ |
| `BACKEND_NOT_FOUND` | 404 | ✗ |
| `BACKEND_RATE_LIMITED` | 429 | ✓ |
| `BACKEND_BAD_REQUEST` | 그 밖의 4xx | ✗ |
| `BACKEND_SERVER_ERROR` | 5xx | ✓ |
| `BACKEND_INVALID_PAYLOAD` | 응답을 해석할 수 없음 | ✗ |
| `INTERNAL_ERROR` | 예상하지 못한 예외 | ✗ |

### 처리 경계

```
tools/*.py         async with tool_error_boundary():   ← 실제 변환이 일어나는 곳
      ▼
errors.py          SearchMCPError → ToolError("[CODE] 메시지")
                   그 밖의 예외     → 로그(traceback) + 일반 메시지
      ▼
middleware.py      도구 "바깥"에서 난 에러의 백스톱 + 호출 결과 로깅
```

변환이 미들웨어가 아니라 **도구 본문 안**에서 일어나는 이유가 있습니다. FastMCP는
도구에서 빠져나온 예외를 미들웨어가 보기 **전에** 가로채서
`Error calling tool '...'`로 덮어씁니다. 따라서 유용한 메시지를 남기려면 그보다 안쪽,
즉 도구 본문에서 잡아야 합니다.

클라이언트가 받는 메시지는 항상 다음 형태입니다.

```
[BACKEND_TIMEOUT] The backend API did not respond within 30s. The request may succeed if retried.
[INVALID_INPUT] `query` must not be empty. Provide the text to search for.
[INVALID_INPUT] `method`: Input should be 'bm25', 'knn', 'cc' or 'rrf'
[MISSING_CREDENTIALS] ... the request is missing: x-api-key. ...
```

접두사 코드로 모델이 "다시 시도할 것"과 "인자를 고칠 것"을 구분할 수 있습니다.

스키마 위반(잘못된 `method` 값 등)은 도구 본문이 실행되기도 전에 FastMCP가 잡습니다.
이 경우도 마스킹하지 않고 어떤 인자가 무엇을 기대하는지 그대로 알려줍니다 — 내부 정보가
아니라 호출자가 고쳐야 할 정보이기 때문입니다.

### 정보 노출 차단

`ES_MCP_MASK_ERROR_DETAILS=true`(기본값)일 때, 예상하지 못한 예외는 클라이언트에게
단일한 일반 메시지로만 전달되고 원본은 traceback과 함께 로그에만 남습니다. 백엔드
에러 응답 본문도 마찬가지로 로그에만 기록되고 클라이언트로는 나가지 않습니다.
개발 중에는 `false`로 두면 원본 메시지가 그대로 보입니다.

### 재시도

`backend/client.py`가 재시도 가능한 에러(연결 실패, 타임아웃, 429, 5xx)에 대해서만
**full jitter 지수 백오프**로 재시도합니다. 재시도할 수 없는 에러는 즉시 올라갑니다.

---

## 9. 개발

`uv`를 쓰는 경우:

```bash
uv sync --all-groups

uv run pytest              # 테스트
uv run ruff check .        # 린트
uv run ruff format .       # 포매팅
uv run mypy                # 타입 검사 (strict)
```

표준 `python` / `pip`만 쓰는 경우 (§5의 venv를 활성화한 상태에서):

```bash
pip install -e . && pip install pytest pytest-asyncio respx ruff mypy

pytest
ruff check .
ruff format .
mypy
```

### 규칙

* **레이어를 건너뛰지 않습니다.** 도구는 `backend/client.py`를 통해서만 네트워크에
  접근하고, `httpx` 예외가 도구 계층까지 올라오지 않습니다.
* **새 실패 모드는 `exceptions.py`에 코드와 함께 추가합니다.** 문자열 메시지로만
  구분하지 않습니다.
* **로그는 `logger.info("event.name", fields={...})` 형태로 남깁니다.** 메시지에
  값을 문자열 보간하지 않습니다 — 이벤트 이름은 검색 가능한 상수여야 합니다.
* **도구 docstring은 모델이 읽는 문서입니다.** 언제 이 도구를 쓰는지, 다른 도구와
  어떤 순서로 쓰는지를 적습니다. 반환 스키마는 Pydantic 모델이 담당합니다.
* **모든 도구 본문은 `tool_error_boundary()`로 감쌉니다.**
* **자격증명은 요청마다 `resolve_credentials()`로 읽습니다.** 클라이언트나 모듈 전역에
  보관하지 않습니다 — 보관하면 동시 호출자끼리 키가 섞입니다.
* **검색 전략을 추가할 때는 `RetrieveMethod`에 값을 넣고 `RETRIEVE_METHOD_GUIDE`에
  설명을 씁니다.** 설명은 도구 docstring에 자동으로 채워져 모델이 읽게 됩니다.
* `tools/` 모듈은 `from __future__ import annotations`를 쓰지 않습니다. 도구 시그니처는
  등록 시점에 평가되어 MCP 입력 스키마가 되는데, 지연 평가된 문자열 애노테이션은
  클로저 변수(`settings`)를 해석하지 못합니다.

### 테스트

| 파일 | 범위 |
| --- | --- |
| `test_config.py` | 설정 정규화와 검증 |
| `test_models.py` | 백엔드 응답 별칭·봉투 정규화 |
| `test_backend_client.py` | 상태코드 → 예외 매핑, 재시도, method별 엔드포인트, 헤더 전파 (`respx`) |
| `test_credentials.py` | 헤더 추출, 누락 처리, 헤더 이름 설정 |
| `test_errors.py` | 에러 경계의 변환과 마스킹 |
| `test_logging.py` | 포맷터, 컨텍스트 바인딩, 마스킹, stderr 보장 |
| `test_server_tools.py` | 실제 ASGI 앱을 통한 전 계층 통합 (도구명·인자·자격증명) |

통합 테스트는 실제 ASGI 앱을 인프로세스로 띄워(`httpx2.ASGITransport`, 소켓 없음)
자격증명 헤더까지 실제 경로로 지나갑니다. MCP 클라이언트는 `httpx2`를, 백엔드 클라이언트는
`httpx`를 쓰기 때문에 `respx`는 백엔드 호출만 정확히 가로챕니다.
