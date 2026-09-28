# 매월 링크 자동 발송 (`invite.py`)

전화번호 명단에 있는 사람들에게 **매월 정해진 날** 링크를 자동으로 보낸다.
수신자마다 문자 / 카카오 알림톡 / 텔레그램 중 하나를 고른다.

```
크론(매일) ─> invite.py send ─┬─ 발송일 전? ─────────────> 종료
                              ├─ 이번 달 미승인? ──> 운영자에게 승인 요청(1회) ─> 종료
                              └─ 승인됨 ─> 아직 안 받은 사람에게만 발송 ─> 결과 보고
```

- **같은 달, 같은 번호로는 두 번 나가지 않는다.** 크론을 하루에 몇 번 돌려도 안전하다.
- **실패하면 다음 실행에서 다시 시도한다.** `max_attempts`(기본 3)를 넘으면 그달은 포기하고 보고한다.
- **받았는지 알 수 없는 건(타임아웃·서버 오류)은 자동으로 다시 보내지 않는다.** `❓ 불명` 으로 표시하고 보고한다 — 중복 발송 방지.
- **`send` 는 동시에 하나만 돈다** (파일 잠금). 크론이 겹쳐도 두 통이 나가지 않는다.
- **사람 승인 게이트**(`approval_required`, 기본 켜짐): 링크를 매달 바꾸는 경우에
  지난달 링크가 그대로 나가는 사고를 막는다.

---

## 빠른 시작

```bash
cp invite.example.json invite.json          # 둘 다 .gitignore 됨
cp recipients.example.csv recipients.csv    # 전화번호 명단 — 절대 커밋 금지

python3 invite.py check      # 설정·명단 검증 + 수신자별 경로 / 문자 길이(SMS·LMS)
python3 invite.py solapi-check  # (솔라피 사용 시) 잔액·알림톡 템플릿 승인·변수 점검, 발송 안 함
python3 invite.py preview    # 이번 달 나갈 메시지 전부 확인
python3 invite.py approve    # 이번 달 승인
python3 invite.py send --now # 발송 (예시 설정은 console 경로라 화면에만 찍힘)
python3 invite.py status     # 누가 받았고 누가 실패했는지
```

흐름을 확인한 뒤 `invite.json` 의 `provider` 를 `console` → `solapi` / `telegram` 으로 바꾼다.

### 명단 (`recipients.csv`)

```csv
name,phone,channel,link,active,memo
홍길동,010-1234-5678,sms,,y,
김철수,+82 10 2345 6789,kakao,,y,
이영희,01034567890,telegram,,y,
박민수,010-4567-8901,,https://example.com/personal,y,개인 링크
```

| 열 | 필수 | 설명 |
|---|---|---|
| `name` | ✅ | 메시지의 `{name}` |
| `phone` | ✅ | 하이픈·`+82` 모두 허용. 한국 휴대폰 번호만 |
| `channel` | | `sms` / `kakao` / `telegram`. 비우면 `default_channel` |
| `link` | | 이 사람에게만 다른 링크. 비우면 설정의 `link` |
| `active` | | `n` 이면 건너뜀 (행을 지우지 않고 잠시 빼기) |
| `memo` | | 메시지의 `{memo}` |

엑셀에서 **"CSV UTF-8"** 로 저장하면 된다. 엑셀이 번호의 앞자리 0 을 지워도(`1012345678`) 복구한다.
번호가 겹치면 로드 단계에서 거부한다.

메시지 자리표시자: `{name}` `{link}` `{link_noscheme}`(https:// 뺀 링크, 알림톡 버튼용) `{month}` `{year}` `{memo}`

### 매월 자동 실행 (크론)

```cron
# 매일 10:05 (KST 서버 기준). 발송일 전이면 아무것도 안 하고 끝난다.
5 10 * * *  cd /path/to/repo && python3 invite.py send >> logs/invite.log 2>&1
```

매일 돌리는 이유: 1일에 서버가 꺼져 있었거나 승인을 늦게 했어도 다음 날 따라잡는다.
이미 받은 사람은 건너뛰므로 중복 발송은 없다.

---

## 발송 경로

### 문자 (솔라피)

1. [솔라피](https://solapi.com) 가입 → API Key / Secret 발급
2. **발신번호 사전등록** (전기통신사업법상 의무 — 등록 안 된 번호로는 발송 불가)
3. 설정:

```bash
export SOLAPI_API_KEY=...  SOLAPI_API_SECRET=...
```
```json
"sms": { "provider": "solapi", "api_key": "${SOLAPI_API_KEY}",
         "api_secret": "${SOLAPI_API_SECRET}", "from": "01000000000" }
```

- 한글 약 45자(EUC-KR 90바이트)를 넘으면 LMS(장문)로 발송되어 요금이 오른다. `check` 에서 바이트 수가 보인다.
- 문자에 들어간 링크, 특히 **단축 URL은 통신사 스팸 필터에 걸리기 쉽다.** 원래 주소를 그대로 쓰는 걸 권한다.

### 카카오톡 (알림톡)

전화번호로 카톡을 보내는 공식 방법은 알림톡뿐이다. **비즈니스 인증 채널(= 사업자등록)이 필요**하고,
템플릿을 카카오가 심사한다. 절차는 **[kakao-alimtalk.md](kakao-alimtalk.md)** 에 있다.

- 본문은 **템플릿 그대로** 나간다. 설정의 `message` 는 알림톡에 쓰이지 않고, 바뀌는 부분은 `variables` 로 채운다.
- `sms_fallback: true` 면 알림톡이 실패했을 때(카톡 미사용 등) 문자로 대신 나간다(문자 요금).
- 발송 전에 `invite.py solapi-check` 로 템플릿 승인 여부와 변수 일치를 확인할 것.

### 텔레그램

텔레그램 봇은 **전화번호만으로 먼저 말을 걸 수 없다.** 수신자가 한 번 연결해 줘야 한다.

1. `@BotFather` → `/newbot` → 토큰
2. 설정: `"telegram": { "provider": "telegram", "bot_token": "${TELEGRAM_BOT_TOKEN}" }`
3. `python3 invite.py telegram-link` 실행 (연결 기간 동안 켜둔다)
4. 수신자에게 `https://t.me/<봇이름>` 을 한 번 보낸다 → 수신자가 `/start` → **[📱 내 번호 공유]**
5. 명단에 있는 번호면 연결 완료. `check` 에서 "연결됨" 으로 바뀐다.

본인 번호가 아닌 연락처를 전달하거나, 명단에 없는 번호이거나, 그룹 대화방에서 보낸 요청은 거부한다.

### 운영자 알림

`owner.telegram_chat_id` 를 채우면 **승인 요청**과 **발송 결과**를 텔레그램으로 받는다
(`channels.telegram.bot_token` 필요). 비워두면 로그에만 남는다.

---

## 명령어

| 명령 | 설명 |
|---|---|
| `invite.py check` | 설정·명단 검증, 수신자별 경로·문자 길이·텔레그램 연결 상태 |
| `invite.py preview [--period YYYY-MM]` | 나갈 메시지 전부 출력 |
| `invite.py approve [--period]` | 그달 발송 승인 |
| `invite.py send` | 크론용. 발송일 전·미승인이면 종료. `--period` 를 직접 주면 발송일 검사 생략 |
| `invite.py send --now` | 발송일 검사 생략 (승인은 여전히 필요) |
| `invite.py send --dry-run` | 대상만 출력 |
| `invite.py status [--period]` | 그달 발송 현황 (✅ 발송 / ⏳ 대기 / ⚠️ 실패 N회 / ❓ 불명 / ⛔ 포기) |
| `invite.py solapi-check` | 솔라피 잔액, 알림톡 템플릿 상태·변수 점검 (조회만 함) |
| `invite.py reset --phone 010... [--period]` | 한 명 기록 삭제 → 재발송 |
| `invite.py reset --all [--period]` | 그달 전체 기록 삭제 |
| `invite.py telegram-link` | 텔레그램 번호 연결 대기 |

## 주의

- **발송 요청은 재시도하지 않는다.** 실패는 두 종류로 나눈다.
  - 확실히 안 나감 (4xx, 접수 거부, 연결 실패) → 다음 실행에서 자동 재시도
  - 나갔는지 모름 (타임아웃, 5xx) → `❓ 불명`, 자동 재시도 안 함. 솔라피 콘솔 발송 내역을 확인하고
    안 나갔으면 `invite.py reset --phone 010…` 후 다시 `send`
- 솔라피의 "접수 성공"은 **통신사 도달**이 아니다. 최종 도달 여부는 솔라피 콘솔에서 확인한다.
  API 상세는 [solapi.md](solapi.md).
- 승인은 **달 단위**다. 승인한 뒤 명단에 추가한 사람도 그달 발송에 포함된다.
- 수신자 동의 없이 보내거나 광고성 내용을 담으면 정보통신망법 적용 대상이 된다.
  명단은 받기로 한 사람만으로 유지할 것.

## 검증 상태

| 구성요소 | 상태 |
|---|---|
| 명단 파싱 · 번호 정규화 · 설정 검증 | ✅ 단위 테스트 |
| 월별 중복 방지 · 재시도 한도 · 승인 게이트 · 동시 실행 잠금 | ✅ 단위 테스트 + CLI 종단 테스트(console) |
| "확실히 안 나감 / 모름" 판정 (4xx·5xx·연결 거부) | ✅ 로컬 HTTP 서버로 종단 테스트 |
| 텔레그램 번호 연결 (본인 확인·명단 대조·offset) | ✅ 가짜 API로 테스트 |
| 솔라피 요청·응답 형식, HMAC 서명, 변수 규칙 | ✅ **공식 SDK 소스(Python 5.0.3 / Node 6.0.1)와 대조** · ⚠️ 실제 API 미호출 |
| `solapi-check` (잔액·템플릿 조회) | ⚠️ 가짜 API로만 테스트. 템플릿 변수 응답 형식(`#{이름}` / `이름`)은 둘 다 받도록 처리 |
| 알림톡 실제 발송·대체문자 | ⚠️ 미검증 — 채널·템플릿 준비 후 본인 번호로 시험 필요 |
