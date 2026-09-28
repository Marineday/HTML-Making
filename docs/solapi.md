# 솔라피(SOLAPI) 분석

`invite.py` 에서 문자·알림톡을 보내는 데 쓰는 솔라피 API를 분석한 문서다.
분석 결과를 PoC에 어떻게 반영했는지도 함께 적었다.

## 0. 근거와 한계 (먼저 읽을 것)

| 근거 | 신뢰도 | 비고 |
|---|---|---|
| **공식 Python SDK `solapi` 5.0.3** (PyPI 소스) | 높음 | 인증·요청·응답 모델을 코드로 직접 확인했다 |
| **공식 Node SDK `solapi` 6.0.1** (npm 소스) | 높음 | 스키마, 에러 타입, 템플릿·채널 API 경로를 확인했다 |
| 웹 검색 요약 (가격, 알림톡 심사·채널 요건) | 중간 | 원문 페이지는 개발 환경에서 접속이 막혀 **검색 요약만 봤다** |
| 실제 API 호출 | **없음** | `api.solapi.com` 이 개발 환경에서 차단돼 있다 |

그래서 이 문서의 **API 형식은 SDK 소스 기준으로 사실**이다. 하지만 **서버의 실제 동작**
(시간 오차 허용 범위, 에러 코드 목록, 템플릿 변수 응답 형식)과 **가격**은 검증하지 못했다.
첫 발송 전에 `invite.py solapi-check` 를 돌리고, 본인 번호 한 개로 시험할 것.

---

## 1. 인증 — HMAC-SHA256

```
Authorization: HMAC-SHA256 apiKey={API_KEY}, date={ISO8601}, salt={랜덤}, signature={hex(HMAC_SHA256(API_SECRET, date + salt))}
```

| 항목 | Node SDK | Python SDK | 이 저장소 |
|---|---|---|---|
| 키 이름 | `apiKey`, `date` | `ApiKey`, `Date` (대문자) | `apiKey`, `date` |
| date | `formatISO(new Date())` (서버가 UTC면 `…Z`) | 로컬시각 + 오프셋 (`…+09:00`) | UTC `…Z` |
| salt | 영숫자 32자 | `uuid1().hex` (32자) | `secrets.token_hex(16)` (32자) |

- 두 SDK 의 키 이름 대소문자가 다르다. 그러니 서버는 **대소문자를 구분하지 않고**, **오프셋 있는 ISO 8601 이면 받는다**고 봐도 된다.
- 요청마다 date 와 salt 를 새로 만든다. 서버가 date 기준으로 오래된 요청을 거부할 가능성이 높다
  (정확한 허용 오차는 미확인). **서버 시계를 NTP 로 맞춰 둘 것.**
- API Secret 은 서명에만 쓰이고 전송되지 않는다. 이 저장소는 오류 메시지에 Secret 이 섞여 나가면 `***` 로 가린다.

## 2. 발송 — `POST /messages/v4/send-many/detail`

두 SDK 모두 단건·다건 발송에 이 엔드포인트 하나를 쓴다.

### 요청

```json
{
  "messages": [
    { "to": "01012345678", "from": "0212345678", "text": "본문", "autoTypeDetect": true }
  ],
  "showMessageList": true,
  "allowDuplicates": false,
  "scheduledDate": "2026-10-01T10:00:00+09:00"
}
```

| 필드 | 설명 |
|---|---|
| `messages[].to` / `from` | 하이픈 없는 숫자. `from` 은 **사전등록된 발신번호**여야 한다 |
| `messages[].type` | `SMS`/`LMS`/`MMS`/`ATA`(알림톡)/`CTA`(친구톡) 등. 생략 시 `autoTypeDetect` |
| `messages[].autoTypeDetect` | SDK 기본값은 `true`. 길이로 SMS/LMS 를 고르고, `kakaoOptions` 가 있으면 알림톡으로 판정 |
| `messages[].subject` | LMS·MMS 제목 (선택) |
| `messages[].country` | 기본 `"82"` |
| `messages[].customFields` | 문자열 키·값. 조회할 때 되돌려 받는다 → 멱등 키 후보 |
| `messages[].kakaoOptions` | `pfId`, `templateId`, `variables`, `disableSms`, `adFlag`, `buttons`, `imageId` |
| `showMessageList` | **기본 false. false 면 응답에 `messageList`(메시지 ID)가 없다** |
| `allowDuplicates` | 기본 false. 한 요청 안의 같은 수신번호 중복을 막는다 |
| `scheduledDate` | 예약 발송. `DELETE /messages/v4/groups/{groupId}/schedule` 로 취소 |

### 응답 (HTTP 200)

```json
{
  "groupInfo": { "groupId": "G4V…", "status": "SENDING",
                 "count": { "total": 1, "registeredSuccess": 1, "registeredFailed": 0, … } },
  "failedMessageList": [
    { "to": "…", "statusCode": "1062", "statusMessage": "…", "messageId": "…", "type": "SMS" }
  ],
  "messageList": [ { "messageId": "M4V…", "statusCode": "2000", "statusMessage": "…" } ]
}
```

**중요한 의미 3가지:**

1. **HTTP 200 이어도 일부 또는 전부가 거부됐을 수 있다.** 거부된 건 `failedMessageList` 에 담긴다.
   Python SDK 는 **전부 실패했을 때만** 예외를 던진다(`count.total == registeredFailed`).
   부분 실패는 호출한 쪽이 직접 확인해야 한다.
2. **"접수(registered)"는 "도달"이 아니다.** 통신사·카카오까지의 결과는 비동기로 나온다.
   조회는 `GET /messages/v4/list` (Node SDK, 필드 `status`/`statusCode`/`reason`/`networkName`)
   또는 웹훅(Python SDK 에 `single_report`/`group_report` 모델이 있다)으로 한다.
3. **알림톡 대체문자**가 나가면 `count.sentReplacement` 가 오른다. 요금은 문자 요금이 붙는다.

### 오류

| 상황 | 형식 | 이 저장소의 판정 |
|---|---|---|
| 4xx | `{"errorCode": "...", "errorMessage": "..."}` | **확실히 안 나감** → 다음 실행에서 재시도 |
| 5xx | 본문 텍스트 | **모름** → 자동 재시도 안 함 |
| 타임아웃, 응답 도중 끊김 | — | **모름** |
| DNS 실패, 연결 거부, TLS 실패 | — | **확실히 안 나감** |
| 200 + `failedMessageList` | 위 JSON | **확실히 안 나감** (접수 거부) |

Python SDK 는 `httpx.HTTPTransport(retries=3)` 를 쓰는데, 이건 **연결 단계 재시도**라 중복 위험이 작다.
이 저장소는 한 단계 더 보수적으로 발송 요청을 아예 재시도하지 않는다.

## 3. 점검에 쓰는 조회 API

| API | 용도 | 주요 필드 |
|---|---|---|
| `GET /cash/v1/balance` | 잔액 | `balance`, `point`, `lowBalanceAlert`, `autoRecharge` |
| `GET /kakao/v2/templates/{templateId}` | 알림톡 템플릿 | `status`(`PENDING`/`INSPECTING`/`APPROVED`/`REJECTED`), `content`, `variables[{name}]`, `channelId`, `messageType`(`BA`/`EX`/`AD`/`MI`), `buttons`, `comments` |
| `GET /kakao/v2/channels` | 연동 채널 목록 | `channelId`(= `pfId`), `searchId` |
| `POST /kakao/v2/channels/token` → `POST /kakao/v2/channels` | 채널 연동 | `searchId`, `phoneNumber`(채널 관리자), `categoryCode`, `token` |

`invite.py solapi-check` 가 위의 잔액 API 와 템플릿 API 를 호출한다. 확인하는 것은 **잔액 0**,
**템플릿 미승인**, **템플릿 변수 ↔ 설정 불일치**, **채널 불일치**다. 문자는 보내지 않는다.

## 4. 알림톡 변수 규칙 (SDK 검증 로직 기준)

- 키는 `#{변수명}` 형식이다. Node SDK 는 `이름` 만 줘도 `#{이름}` 으로 감싼다. 이 저장소는 헷갈리지 않도록 `#{…}` 형식만 받는다.
- **변수명에 점(`.`)은 쓸 수 없다** (`VariableValidationError`).
- 값은 문자열이다.
- 템플릿에 있는 변수는 전부 채워야 한다. 빠지면 접수가 거부되거나 `#{이름}` 이 그대로 노출된다(서버 동작은 미확인). → `solapi-check` 로 미리 잡는다.

## 5. 비용 (⚠️ 검색 요약, 공식 가격표로 재확인 필요)

검색 결과에서 본 **직접 연동 단가**(VAT 별도): SMS 13원 · LMS 29원 · 알림톡 8원.
알림톡 구간 할인표는 월 1만 건 미만일 때 13원으로 나와 있어서 **출처끼리 값이 다르다.**
월 기본료는 없다고 한다.

| 시나리오 (월) | 대략 비용 |
|---|---|
| 20명 × SMS | 약 260원 |
| 20명 × LMS (한글 45자 초과) | 약 580원 |
| 20명 × 알림톡 | 약 160~260원 (+ 대체문자로 나간 건은 문자 요금) |

소규모 명단에서는 비용이 의사결정 요소가 아니다. **발송 준비 절차와 심사가 더 큰 비용이다.**

## 6. 법·운영 제약

- **발신번호 사전등록** (전기통신사업법): 등록하지 않은 번호로는 접수가 거부된다. 유선번호도 등록할 수 있다.
- **광고성 정보** (정보통신망법): 내용이 광고면 `(광고)` 표기, 수신거부 방법 안내, 야간(21~08시) 발송 시 별도 동의가 필요하다. 초대 링크 안내를 정보성으로 볼지는 내용에 따라 다르다. 광고 요소(할인, 가입 권유)는 넣지 말 것.
- **스팸 필터**: 단축 URL, 본문에 링크만 있는 메시지는 통신사 필터에 걸리기 쉽다.
- **알림톡은 사업자만 쓸 수 있다**: 비즈니스 인증 채널이 필요하다 → [kakao-alimtalk.md](kakao-alimtalk.md)

## 7. PoC 에 반영한 것

| # | 발견 | 이전 PoC | 수정 |
|---|---|---|---|
| 1 | `showMessageList` 기본 false → `messageList` 없음 | 메시지 ID 대신 그룹 ID 를 저장 | `showMessageList: true` 로 메시지 ID 저장 |
| 2 | SDK 는 `autoTypeDetect: true` 를 명시해서 보냄 | 생략 (서버 기본값에 의존) | 명시 |
| 3 | 4xx 본문이 `errorCode`/`errorMessage` | 원문 300자 | `코드: 메시지` 로 파싱 |
| 4 | 타임아웃·5xx 는 접수됐을 수 있음 | 실패로 기록 → **다음 날 재발송 → 중복 가능** | `unknown` 상태로 기록, 자동 재시도 안 함, 운영자에게 보고 |
| 5 | 변수명 점(.) 금지, `#{}` 형식 | 검증 없음 | 설정 로드 시 거부 |
| 6 | 템플릿 미승인·변수 불일치가 알림톡 실패의 주원인 | 발송일에야 발견 | `solapi-check` 로 사전 점검 |
| 7 | 웹링크 버튼은 `https://` 고정 + 나머지만 변수 | `{link}` 만 있음 | `{link_noscheme}` 자리표시자 추가 |
| 8 | LMS 한도 2,000바이트 | 표시 없음 | `check` 에 초과 경고 |

## 8. 남은 위험 (비관적으로 본 것)

- **서버 동작 미검증**: 형식은 SDK 와 같지만 실제 응답을 한 번도 받아보지 못했다. 특히 템플릿 API 의 `variables[].name` 이 `#{이름}` 인지 `이름` 인지 확정하지 못해서 둘 다 받도록 해 두었다.
- **"모름" 판정이 늘 옳지는 않다**: 연결 타임아웃은 실제로는 안 나갔을 수도 있는데 `unknown` 으로 분류된다. 대신 사람이 확인해야 하는 건이 생긴다. 중복 발송보다는 이쪽이 낫다고 판단했다.
- **도달 확인 없음**: 접수까지만 추적한다. 번호가 바뀐 사람, 스팸 차단한 사람은 "성공"으로 보인다. 필요하면 `GET /messages/v4/list` 로 도달 여부를 조회하는 기능을 추가해야 한다.
- **솔라피 장애나 정책 변경이 곧 발송 불가**: 발송 경로는 `Channel` 인터페이스로 분리해 두었다. 다른 대행사를 붙이려면 클래스 하나만 추가하면 된다.
