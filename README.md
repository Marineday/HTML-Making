# 핫딜 봇 (카카오 오픈채팅 / 텔레그램 / 디스코드)

핫딜을 모아 **단가 기준으로 평가한 뒤** 채팅방에 자동으로 올리는 봇.
"12,900원"이 아니라 **"개당 645원, 30일 중앙값 대비 29% 저렴 — 초특가"** 를 알려준다.

수집·평가·전송이 분리되어 있어 어댑터만 갈아끼우면 카카오·텔레그램·디스코드
어디로든 보낼 수 있고, 소스도 커뮤니티 RSS·HTML·토스 앱을 섞어 쓸 수 있다.

```
🔥 농심 신라면 20개입 12,900원
📍 토스핫딜
💰 12,900원
📊 🔥🔥🔥 초특가 · 645원/개 · 30일 중앙값 910원 대비 29%↓ · 역대 최저 · 표본 6건
🔗 토스 앱 → 홈 → 핫딜 에서 확인
```

- **외부 의존성 0** — 파이썬 3.11 표준 라이브러리만 사용한다. `pip install` 불필요.
- **테스트 332개** — `python3 -m unittest discover -s tests -t .`

| 문서 | 내용 |
|---|---|
| [docs/pricing.md](docs/pricing.md) | 단가 계산과 초특가 판정 |
| [docs/toss-capture.md](docs/toss-capture.md) | 토스 앱에서 핫딜 수집 |
| [docs/kakao-setup.md](docs/kakao-setup.md) | 카카오 오픈채팅 연결 |
| [docs/ip-allocation.md](docs/ip-allocation.md) | VPN 출구 IP 배정 (`ipalloc`) — 핫딜 봇과 별개 도구 |

---

## ⚠️ 먼저 읽어야 할 제약

이 프로젝트를 쓰기 전에 반드시 알아야 하는 사실 세 가지다.

### 1. 카카오톡 오픈채팅에는 공식 봇 API가 없다

카카오가 제공하는 "오픈채팅봇"은 **환영 메시지 / 공지 / 키워드 자동응답** 세 가지
템플릿뿐이다. 외부 서버가 원하는 시점에 메시지를 밀어넣는 기능은 없다.
카카오 비즈니스 API(알림톡·친구톡)는 **채널 1:1 대화**용이지 오픈채팅방이 아니다.

따라서 이 저장소의 카카오 경로는 **안드로이드 기기에서 돌아가는 비공식 자동화**
(메신저봇R 등, 알림 접근 권한 이용)에 의존한다.

### 2. 카카오 경로는 계정 정지 위험이 있다

비공식 자동화는 카카오 운영정책 위반 소지가 있고, **계정 영구정지 사례가 실제로 있다.**
방장 계정이 정지되면 오픈채팅방도 함께 잃는다. 24시간 켜둔 안드로이드 기기도 필요하다.

> **권장 순서**: 텔레그램/디스코드로 코어를 먼저 검증한 뒤, 위험을 감수할 수 있다고
> 판단했을 때만 카카오 어댑터를 켠다. 코어는 100% 그대로 재사용된다.

### 3. 토스 앱은 화면을 읽지, 트래픽을 뚫지 않는다

토스 핫딜은 앱 안에만 있고 공개 API가 없다. 이 저장소는 **접근성 트리(UI 덤프)와
알림 캡처**로 수집한다 — 사람이 화면에서 보는 것과 같은 정보다.

**앱 트래픽 가로채기(TLS MITM, 인증서 피닝 우회)는 만들지 않는다.** 허가 여부와
무관하게, 앱 업데이트마다 무너져서 24시간 돌아야 하는 봇의 데이터 소스로 쓸 수 없다.

절차는 [docs/toss-capture.md](docs/toss-capture.md). 커뮤니티 피드에서 `토스` 키워드로
거르는 방법도 함께 쓸 수 있다 — 토스 특가는 커뮤니티에도 몇 분 내로 올라온다.

---

## 빠른 시작 (5분)

```bash
git clone <이 저장소>
cd <저장소>

cp config.example.json config.json

# 1) 설정한 피드가 실제로 살아있는지 먼저 확인한다.
#    커뮤니티 RSS 주소는 예고 없이 바뀐다. FAIL/EMPTY 가 나오면 주소를 고쳐야 한다.
python3 run.py verify

# 2) 발송 없이 무엇이 나갈지 눈으로 본다.
python3 run.py once --dry-run

# 3) 콘솔로 실제 파이프라인을 돌려본다 (기본 sender 가 console).
python3 run.py once

# 4) 문제없으면 상시 운영.
python3 run.py loop
```

> **최초 실행은 아무것도 보내지 않는다.** 현재 피드 전체를 "이미 본 것"으로 기록만
> 하고 끝난다(`seed_on_first_run`). 이게 없으면 첫 실행에 밀린 피드 수백 건이
> 한꺼번에 방으로 쏟아져 확실하게 차단당한다. 두 번째 주기부터 새 딜만 나간다.

---

## 명령어

| 명령 | 설명 |
|---|---|
| `run.py verify` | 각 소스를 실제로 때려보고 살아있는지 표로 보고. `--show 3` 으로 미리보기 |
| `run.py once` | 1회 수집·발송 |
| `run.py once --dry-run` | 발송 없이 대상만 출력 |
| `run.py loop` | 주기 폴링 (상시 운영) |
| `run.py seed` | 발송 없이 현재 피드를 기준선으로 기록 (봇을 오래 껐다 켤 때) |
| `run.py bridge` | 카카오 브리지 큐 서버 실행 |
| `tools/toss_capture.py parse <file>` | 토스 UI 덤프 파싱 결과 확인 (기기 불필요) |
| `tools/toss_capture.py run` | 토스 앱에서 수집해 브리지로 전송 |

---

## 설정

`config.example.json` 을 `config.json` 으로 복사해서 쓴다. `//` 줄 주석을 지원한다.
`config.json` 은 `.gitignore` 되어 있다.

### 토큰은 환경변수로

설정 파일에 토큰을 평문으로 적지 말 것. `${ENV_VAR}` 로 참조하면 로드 시 치환된다.
참조한 환경변수가 없으면 **즉시 실패한다** — 빈 토큰으로 돌다가 401을 맞는 것보다 낫다.

```json
{ "type": "telegram", "bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "${TELEGRAM_CHAT_ID}" }
```

### 주요 옵션

| 키 | 기본값 | 의미 |
|---|---|---|
| `poll_interval_seconds` | 300 | 폴링 주기. **60 미만은 거부된다** (사이트에서 차단당한다) |
| `max_sends_per_run` | 5 | 한 주기 최대 발송 딜 수. 초과분은 다음 주기로 이월 |
| `send_gap_seconds` | 2.0 | 메시지 묶음 사이 간격. 카카오 도배 판정 회피 |
| `seed_on_first_run` | true | 최초 실행 시 발송 없이 기준선만 기록 |
| `dedup_retention_days` | 14 | 중복제거 기록 보관 기간 |
| `respect_robots` | true | robots.txt 준수 |
| `min_interval_per_host` | 3.0 | 같은 호스트 연속 요청 최소 간격 |

### 필터

```json
"filters": {
  "include_keywords": ["토스", "토스페이", "8시특가"],
  "exclude_keywords": ["토스트", "토스터", "토스기"],
  "require_all_keywords": false,
  "min_price_krw": null,
  "max_price_krw": null,
  "max_age_minutes": 180
}
```

**`exclude_keywords` 는 선택이 아니라 필수다.** 한국어에는 단어 경계가 없어서
`"토스"` 로 필터링하면 `"토스터기"`, `"토스트기"` 가 전부 걸린다. 운영하면서 오탐
단어를 계속 추가해 나가야 한다.

전체 핫딜을 받고 싶으면 `include_keywords` 를 `[]` 로 비우면 된다.

- 게시 시각을 못 얻은 딜은 `max_age_minutes` 검사를 **통과**시킨다.
  (pubDate 없는 소스를 통째로 죽이지 않기 위한 의도적 동작)
- 가격을 못 읽은 딜도 가격 필터를 **통과**시킨다.

### 단가 평가

`pricing` 과 `filters.min_grade` 로 설정한다. 자세한 내용은
[docs/pricing.md](docs/pricing.md).

**첫 2~4주는 `min_grade` 를 비워두고 이력을 쌓아라.** 비교 표본이 없으면
등급을 매길 수 없고, 등급으로 거르면 아무것도 안 나간다.

### 소스 추가

**RSS 를 우선하라.** HTML 스크래핑은 사이트 개편 한 번에 깨진다.

```json
{ "name": "뽐뿌", "type": "rss", "url": "https://www.ppomppu.co.kr/rss.php?id=ppomppu" }
```

RSS 가 없는 사이트는 `html_list` 로 긁는다. 게시글 링크의 `href` 정규식만 주면 된다.

```json
{
  "name": "퀘이사존-지름",
  "type": "html_list",
  "url": "https://quasarzone.com/bbs/qb_saleinfo",
  "base_url": "https://quasarzone.com",
  "link_pattern": "/bbs/qb_saleinfo/views/\\d+"
}
```

> `config.example.json` 의 피드 주소들은 **실제 접속으로 검증되지 않았다.**
> 반드시 `run.py verify` 로 확인하고, FAIL/EMPTY 가 나오면 해당 사이트에서
> RSS 주소를 직접 찾아 고칠 것.

---

## 전송 경로

### 텔레그램 (권장 — 공식 API, 제재 위험 없음)

1. 텔레그램에서 `@BotFather` 에게 `/newbot` → 토큰 발급
2. 봇을 채널/그룹에 초대하고 **관리자 권한** 부여
3. 설정:

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="@my_channel"   # 또는 숫자 chat_id
```

```json
{ "type": "telegram", "enabled": true,
  "bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "${TELEGRAM_CHAT_ID}", "batch_size": 1 }
```

### 디스코드 (가장 간단)

채널 설정 → 연동 → 웹훅 생성 → URL 복사.

```json
{ "type": "discord", "enabled": true, "webhook_url": "${DISCORD_WEBHOOK_URL}", "batch_size": 3 }
```

### 카카오 오픈채팅 (비공식 — 위험 감수 시에만)

전체 절차는 **[docs/kakao-setup.md](docs/kakao-setup.md)** 참고.

```
run.py loop ──POST /enqueue──> 브리지 큐 서버 <──GET /pull── 메신저봇R (안드로이드)
                                                                    │ bot.send()
                                                             카카오 오픈채팅방
```

안드로이드 기기는 공인 IP가 없고 배터리 최적화 때문에 인바운드 연결을 유지하지
못한다. 그래서 서버가 밀어넣지 않고 **기기가 당겨 간다(pull)**.

메시지는 `pull` 시 임대(lease)만 걸리고, 카톡 전송에 성공해 `ack` 를 받아야
삭제된다. 임대가 만료되면 다시 대기 상태로 돌아간다 — **최소 1회 전달**이 보장된다.

---

## 구조

```
run.py                    CLI 진입점
hotdeal/
  config.py               JSON 설정 로드·검증, ${ENV} 치환
  models.py               Deal 값 객체, URL 정규화, 가격 파싱
  http.py                 robots.txt 준수 + 호스트별 스로틀 + 백오프
  store.py                SQLite 중복제거 (URL 해시 + 제목 키)
  filters.py              키워드·가격·신선도 필터
  formatter.py            plain / html / markdown 메시지 포맷
  pricing.py              수량 파싱, 단가 계산, 상품 동일성 판정
  price_store.py          가격 이력 + N일 중앙값 대비 등급
  pipeline.py             수집 → 가격기록 → 필터 → 중복제거 → 등급 → 전송
  sources/                rss.py, html_list.py, ingest.py
  senders/                telegram.py, discord.py, kakao_bridge.py, console.py
bridge/server.py          메시지 큐(lease/ack) + 딜 인박스(롤링 버퍼)
messengerbot/hotdeal.js   안드로이드 메신저봇R 스크립트
tools/toss_capture.py     토스 앱 UI 덤프 → 딜 추출 → 브리지 전송
tests/                    unittest 332개 (핫딜 봇 212 + ipalloc 120)

ipalloc/                  VPN 출구 IP 배정 (핫딜 봇과 독립)
  models.py               User / Endpoint / Assignment / Group 값 객체와 검증
  pool.py                 출구 IP 풀, 터널 대역 분할, 소요 IP 수 산정
  allocator.py            묶기·배정·증분 재배정·불변식 검사
  capacity.py             동시접속·대역폭·전송량 산정 (포아송 + 최악)
  cost.py                 조달 방식별 월 비용과 손익분기 GB 단가
  csvio.py                명부/IP목록/배정표 CSV 입출력
  wireguard.py            서버·사용자 설정과 키 생성 스크립트 생성
  cli.py                  python3 -m ipalloc
```

### 설계상 지키는 것

- **소스 하나가 죽어도 나머지는 계속 돈다.** 커뮤니티 사이트는 자주 죽는다.
- **전송 어댑터 하나가 죽어도 나머지는 계속 보낸다.**
- **어디에도 못 나간 딜은 '봤음' 표시를 하지 않는다** → 다음 주기에 자동 재시도.
- **부분 성공을 정확히 기록한다.** 3건 묶음 2통 중 두 번째가 실패하면 첫 묶음만 기록.
- **중복제거는 2단계.** 정규화 URL 해시(추적 파라미터 제거) + 제목 키(여러 커뮤니티에
  동시에 올라온 같은 딜).
- **확신이 없으면 침묵한다.** 단가를 못 구하거나 비교 표본이 부족하면 등급을
  붙이지 않는다. 틀린 "초특가"가 봇의 신뢰를 가장 빨리 죽인다.
- **가격 이력은 필터와 무관하게 전부 쌓는다.** 토스 딜만 기록해서는 "평소보다
  싸다"를 판단할 기준이 생기지 않는다.

---

## 운영 시 주의

- **첫 실행 후 반드시 `--dry-run` 으로 며칠 관찰하라.** 필터가 덜 여문 상태로 방에
  붙이면 오탐이 그대로 나간다.
- `max_sends_per_run` 을 크게 잡지 마라. 카카오에서 한 번에 여러 통이 나가면
  도배로 인식된다. 5 이하를 권한다.
- 브리지 큐는 방당 500통에서 적재를 거부한다. 안드로이드 기기가 죽어 있는 동안
  무한히 쌓이면, 살아난 순간 한꺼번에 쏟아져 확실하게 차단당하기 때문이다.
- `BRIDGE_TOKEN` 은 16자 이상이어야 한다. 생성:
  `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`
- 브리지 서버를 인터넷에 노출한다면 반드시 HTTPS 리버스 프록시(caddy, nginx) 뒤에 둘 것.
  토큰이 평문으로 흐른다.

---

## 부록: ipalloc — VPN 출구 IP 배정

핫딜 봇과는 **별개의 도구**다. 같은 저장소에 있을 뿐 코드를 공유하지 않는다.

자체 운영하는 영상 서비스의 사용자를 N 명씩 묶어, 한 묶음이 항상 같은 출구 IP 로
나가게 만든다. 배정표(CSV)와 WireGuard 설정을 생성하고, 용량이 실제로 충분한지
계산한다.

```bash
python3 -m ipalloc plan --users 1000 --per-ip 20
# → 출구 IP 50개 필요 · IP 당 월 15 GB · 최악(20명 동시) 100 Mbps

python3 -m ipalloc cost --users 1000 --dc-ip-monthly 3.6 --res-per-gb 2.0
# → 데이터센터 vs 레지덴셜 월 비용과 손익분기 GB 단가

python3 -m ipalloc assign --users users.csv --endpoints endpoints.csv --out out/
python3 -m ipalloc wg --assignments out/assignments.csv --out out/wg/
sh out/wg/genkeys.sh    # 키 생성 (wireguard-tools 필요)
```

핵심 성질 두 가지:

- **결정적·증분적** — `--existing` 으로 기존 배정표를 주면 이미 배정된 사람은
  움직이지 않는다. 빠진 자리부터 채운다.
- **키를 만들지 않는다** — 설정 파일에는 자리표시자만 넣고, `wg genkey` 로 채우는
  스크립트를 같이 내보낸다. 파이썬으로 암호키를 만드는 것보다 안전하다.

절차와 주의사항은 [docs/ip-allocation.md](docs/ip-allocation.md) 에 있다.

---

## 테스트

```bash
python3 -m unittest discover -s tests -t . -v
```

브리지 서버 테스트는 실제 소켓을 열어 HTTP 로 검증한다(임대 만료 재전달, 토큰 인증,
큐 상한, FIFO 순서 포함).

## 검증 상태

| 구성요소 | 상태 |
|---|---|
| 코어 파이프라인 (수집·필터·중복제거·배치·재시도) | ✅ 단위 테스트 + 로컬 HTTP 종단 테스트 통과 |
| RSS / Atom / HTML 목록 파서 | ✅ 테스트 통과 |
| 단가 계산 · 상품 식별 · 등급 판정 | ✅ 테스트 통과 |
| 브리지 큐 + 인박스 (lease/ack/인증) | ✅ 실제 소켓 종단 테스트 통과 |
| kakao_bridge 어댑터 → 큐 적재 → pull/ack | ✅ 로컬 종단 확인 |
| 토스 UI 덤프 파서 | ✅ 픽스처 테스트 통과 (⚠️ 실제 토스 화면 XML 로는 미검증) |
| UI 덤프 → ingest → 인박스 → 등급 → 카카오 큐 전 구간 | ✅ 로컬 종단 확인 |
| `tools/toss_capture.py` 의 adb 구동부 | ⚠️ **실기기 미검증** — adb 없는 환경 |
| 텔레그램 / 디스코드 전송 | ⚠️ 개발 환경 방화벽으로 실제 API 호출 미검증 |
| `config.example.json` 의 커뮤니티 피드 주소 | ⚠️ **미검증** — `run.py verify` 로 직접 확인 필요 |
| `messengerbot/hotdeal.js` (안드로이드) | ⚠️ **실기기 미검증** — 문법 검사만 통과. 버전별 API 차이는 런타임 탐지로 대응 |
| `ipalloc` 배정·검증·용량 산정 | ✅ 테스트 통과 (1,000명 → 50 IP 종단 확인) |
| `ipalloc` WireGuard 설정 생성 + `genkeys.sh` | ⚠️ **실서버 미검증** — 설정 생성과 키 치환 로직은 `wg` 스텁으로 확인. 실제 터널 수립·SNAT 동작은 미확인 |
