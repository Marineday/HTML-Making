# 토스 핫딜 수집

토스 핫딜은 **앱 안에만 있다.** 공개 웹페이지도, 공개 API도 없다.
그래서 이미 카카오 봇을 돌리고 있는 **그 안드로이드 기기에서 직접 읽는다.**

## 무엇을 하고, 무엇을 하지 않는가

| | 방식 | 이 저장소 |
|---|---|---|
| ❌ | 앱 트래픽 가로채기 (TLS MITM, 인증서 피닝 우회) | **만들지 않는다** |
| ✅ | UI 덤프 — 접근성 트리에서 화면의 텍스트를 읽는다 | `tools/toss_capture.py` |
| ✅ | 알림 캡처 — 토스 핫딜 푸시 알림을 받아 넘긴다 | Tasker / MacroDroid 레시피 |

**TLS MITM 을 만들지 않는 이유는 허가 여부와 무관하다.** 앱이 업데이트될 때마다
무너지고, 무너졌을 때 원인을 알기도 어렵다. 24시간 돌아야 하는 봇의 데이터
소스로는 쓸 수 없다. UI 덤프와 알림 캡처는 암호화·피닝과 아무 상관이 없고,
사람이 화면에서 보는 것과 같은 정보를 읽는다.

두 방식 모두 **같은 엔드포인트**(`POST /ingest`)로 밀어넣으므로, 하나가 막히면
다른 하나로 갈아타도 파이프라인은 그대로다.

```
안드로이드 기기                          서버
┌──────────────────┐
│ 토스 앱          │
│   ↓ UI 덤프      │  ──POST /ingest──>  브리지 인박스
│ 또는 알림        │                          │
└──────────────────┘                          │ GET /inbox
                                              ▼
                                    파이프라인 (단가 판정)
                                              │ POST /enqueue
                                              ▼
┌──────────────────┐                    브리지 아웃박스
│ 메신저봇R        │  <──GET /pull──────────  │
│   ↓              │
│ 카카오 오픈채팅   │
└──────────────────┘
```

---

## 방법 A: UI 덤프 (권장 — 목록 전체를 한 번에 읽음)

### 준비

1. 기기: 설정 → 개발자 옵션 → **USB 디버깅** 켜기
2. PC 에 Android Platform Tools 설치 (`adb`)
3. 연결 확인:

```bash
adb devices          # 기기가 device 로 뜨면 OK
```

무선으로 쓰려면 (USB 한 번 연결한 뒤):

```bash
adb tcpip 5555
adb connect 192.168.0.20:5555
```

### 파서 맞추기

토스 앱 화면 구조는 언제든 바뀐다. **먼저 덤프를 떠서 무엇이 잡히는지 확인한다.**

```bash
# 1) 기기에서 토스 앱 → 핫딜 화면을 열어둔다 (화면 켜짐 + 잠금 해제 필수)
# 2) 현재 화면을 덤프
python3 tools/toss_capture.py dump --out screen.xml

# 3) 기기 없이 파싱 결과만 확인 — 몇 번이고 반복 가능
python3 tools/toss_capture.py parse screen.xml
```

정상이면 이렇게 나온다:

```
  [     12,900원] 농심 신라면 20개입 12,900원
  [     14,900원] 코카콜라 제로 190ml 30캔 14,900원

총 2건
```

아무것도 안 잡히면:

| 증상 | 조치 |
|---|---|
| `잡힌 딜이 없습니다` | `--min-texts 1` 로 낮춰본다 |
| 가격만 없는 카드가 많다 | `--include-priceless` 로 확인 |
| 상품명 자리에 "더보기" 같은 UI 문구 | `tools/toss_capture.py` 의 `_CHROME_WORDS` 에 추가 |
| 카드가 통째로 하나로 잡힘 | 화면 구조가 바뀐 것. `find_cards` 의 조건을 조정 |
| `uiautomator 덤프 실패` | 화면이 꺼졌거나 잠금 상태다 |

### 수집 실행

```bash
export BRIDGE_TOKEN="브리지 토큰"

# 먼저 전송 없이 확인
python3 tools/toss_capture.py run --dry-run --scrolls 3

# 실제 전송
python3 tools/toss_capture.py run \
  --bridge http://127.0.0.1:8080 \
  --source 토스핫딜 \
  --scrolls 3 \
  --open-app
```

`--scrolls 3` 은 화면을 읽고 아래로 스크롤하기를 3번 반복한다는 뜻이다.
목록이 길면 늘리고, 배터리·시간이 아까우면 줄인다.

### 주기 실행

cron 으로 돌린다. **너무 자주 돌리지 마라** — 기기 배터리와 발열이 실제 문제다.

```cron
*/15 * * * * cd /path/to/repo && BRIDGE_TOKEN=... python3 tools/toss_capture.py run --scrolls 3 >> /var/log/toss.log 2>&1
```

### 한계 (알고 쓸 것)

- **화면이 켜져 있고 잠금이 풀려 있어야 한다.** 기기를 상시 충전 + 화면 켜짐으로 두거나,
  잠금 해제를 자동화해야 한다.
- **토스 앱에 앱 잠금(생체인증)이 걸려 있으면 자동화가 멈춘다.** 봇 전용 기기라면 꺼두는 편이 낫다.
- 토스가 화면 구조를 바꾸면 파서를 조정해야 한다. 고칠 곳은 `tools/toss_capture.py` 한 파일이다.
- **카드에 웹 링크가 없다.** 그래서 메시지에는 링크 대신 "토스 앱 → 홈 → 핫딜 에서 확인"이 나간다.

---

## 방법 B: 알림 캡처 (더 잘 버티지만 물량이 적음)

토스 핫딜 푸시 알림을 받아서 그대로 넘기는 방식이다. 화면을 켜둘 필요도,
앱을 열 필요도 없다. 대신 **알림이 오는 딜만** 잡힌다.

### Tasker / MacroDroid 설정

1. 토스 앱에서 핫딜 알림을 켠다
2. 트리거: **알림 수신** — 패키지 `viva.republica.toss`
3. 액션: **HTTP Request**
   - Method: `POST`
   - URL: `http://192.168.0.10:8080/ingest`
   - Headers: `X-Bot-Token: <브리지 토큰>`, `Content-Type: application/json`
   - Body:

```json
{"source":"토스핫딜","deals":[{"title":"%ntitle %ntext"}]}
```

`%ntitle` / `%ntext` 는 Tasker 의 알림 변수다 (MacroDroid 는 `{noti_title}` / `{noti_text}`).
제목에 가격이 들어 있으면 `price_krw` 를 따로 넣지 않아도 파이프라인이 파싱한다.

### 확인

```bash
curl -H "X-Bot-Token: $BRIDGE_TOKEN" http://127.0.0.1:8080/inbox?limit=10
```

---

## 파이프라인에 연결

`config.json` 에서 `ingest` 소스를 켠다.

```json
{
  "name": "토스핫딜",
  "type": "ingest",
  "url": "http://127.0.0.1:8080",
  "token": "${BRIDGE_TOKEN}",
  "enabled": true,
  "max_items": 50
}
```

`name` 은 `toss_capture.py --source` 값과 맞출 필요는 없지만, 맞춰두면 로그를 읽기 쉽다.

인박스는 **읽어도 지워지지 않는 롤링 버퍼**(최대 2000건)다. 파이프라인이 죽어도
딜이 유실되지 않고, 같은 딜을 여러 번 읽어도 파이프라인의 지문 기반 중복제거가 거른다.

---

## 확인 절차

```bash
# 1) 인박스에 딜이 들어왔는지
curl -H "X-Bot-Token: $BRIDGE_TOKEN" http://127.0.0.1:8080/inbox?limit=5

# 2) 파이프라인이 읽고 판정하는지 (전송은 안 함)
python3 run.py once --dry-run

# 3) 실제 발송
python3 run.py once
```
