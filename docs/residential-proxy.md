# 레지덴셜 프록시 경로

데이터센터 고정 IP 대신 레지덴셜/ISP 프록시를 출구로 쓰는 구성이다.
기본 배정 개념은 [ip-allocation.md](ip-allocation.md) 와 같고, **출구를 만드는
방식만 다르다.**

```bash
python3 -m ipalloc proxy-export --assignments out/assignments.csv \
    --proxies proxies.csv --entry-host vpn.example.com --out out/px

cd out/px
sh genkeys.sh                              # WireGuard 키
IPALLOC_PROXY_PASSWORD='...' sh fill-secrets.sh
sh iptables.sh up

python3 -m ipalloc proxy-check --assignments out/assignments.csv --proxies proxies.csv
```

---

## 1. 구조가 달라진다

프록시는 HTTP/SOCKS 엔드포인트라 WireGuard 서버를 띄울 수도, 사용자가 인바운드로
붙을 수도 없다. 그래서 체인이 한 단 늘어난다.

```
사용자 → WireGuard 진입 호스트 → 묶음별 redsocks → 묶음별 sticky 프록시 → 서비스
        (공인 IP 1개)            (127.0.0.1:12000+)
```

**진입 IP 는 1개면 된다.** 출구를 구분하는 것이 진입 IP 가 아니라 프록시이기
때문이다. 데이터센터 방식에서 50개가 필요했던 IP 가 여기서는 1개(이중화하면 2개)로
줄어든다 — 이게 이 경로의 유일한 구조적 이점이다.

묶음 구분은 터널 서브넷이 한다. iptables 가 출발지 서브넷을 보고 그 묶음의 redsocks
포트로 보내고, redsocks 가 그 묶음의 프록시 자격증명으로 업스트림에 붙는다.

| | 데이터센터 | 레지덴셜 |
|---|---|---|
| 진입 공인 IP | 묶음 수만큼 (50개) | 1~2개 |
| 출구 결정 | 호스트의 SNAT 규칙 | 업스트림 프록시 |
| 출구 고정성 | 완전 고정 | **sticky (보장 아님)** |
| UDP | 정상 | **죽는다** (3장) |
| 홉 | 1 | 2 |
| 과금 | IP 월정액 | 대개 트래픽 GB |

비용 비교는 `python3 -m ipalloc cost` 로 직접 견적을 넣어 계산한다.

---

## 2. "고정 IP" 가 아니라 sticky 다

레지덴셜 풀은 로테이션이 기본이다. 공급사는 보통 **사용자명에 세션 토큰을 끼워
넣으면** 일정 시간 같은 출구를 준다.

```
customer1-session-82cd913c29
         ^^^^^^^^^^^^^^^^^^^ 이 토큰이 같으면 같은 출구를 주려고 시도한다
```

`ipalloc` 은 묶음마다 결정적인 토큰을 만든다. 같은 묶음은 언제 실행해도 같은 토큰을
요청한다 — 재시작마다 난수를 쓰면 "묶음은 늘 같은 IP" 전제가 매번 깨지기 때문이다.

공급사마다 토큰을 넣는 자리가 다르므로 `sticky_template` 으로 맞춘다.

```csv
host,port,protocol,username,password,sticky_template
gw1.example.com,8000,socks5,customer1,${PROXY_PW},{username}-session-{session}
gw2.example.com,8000,socks5,customer1,${PROXY_PW},{username}_sess{session}_kr
```

> **세션 유효시간은 보통 10~30분이고, 보장이 아니다.** 한 달에 한 번 20분 접속하는
> 사용 패턴이면 매번 새 세션이 열리는 셈이라, 같은 묶음이 달마다 다른 출구를 받을
> 가능성이 높다. 이 경로에서 "고정 IP" 를 기대하면 안 된다. 실제로 어떻게 동작하는지는
> 4장의 측정으로만 알 수 있다.

`--salt` 를 바꾸면 전 묶음의 세션이 한꺼번에 갈린다. 출구를 통째로 교체할 때 쓴다.

---

## 3. UDP 가 죽는다 — 가장 큰 함정

SOCKS5 의 UDP ASSOCIATE 를 지원하는 레지덴셜 공급사는 드물다. 그런데 **요즘 영상
스트리밍은 QUIC(UDP 443)을 먼저 쓴다.** 그대로 두면 재생이 안 되거나 한참 기다렸다
붙는다. 게다가 이 증상은 "가끔 느리다" 로 보고되기 때문에 원인을 찾기 어렵다.

생성되는 `iptables.sh` 는 터널에서 나가는 UDP 443 을 **거부(reject)** 한다.

```
run $OP FORWARD -i "$WG" -p udp --dport 443 -j REJECT --reject-with icmp-port-unreachable
```

drop 이 아니라 reject 여야 한다. drop 하면 클라이언트가 타임아웃을 기다린 뒤에야 TCP 로
되돌아가서 체감이 더 나빠진다.

DNS(UDP 53)는 프록시를 타지 않고 진입 호스트의 일반 경로로 나간다. 조회 출처가 진입
호스트 IP 가 되지만, 자체 서비스이므로 숨길 이유가 없고 UDP 를 SOCKS5 에 태우는 것보다
훨씬 안정적이다.

---

## 4. 반드시 측정하고 넣을 것

sticky 가 실제로 유지되는지는 문서가 아니라 측정으로만 안다.

```bash
# 1차 측정
python3 -m ipalloc proxy-check --assignments out/assignments.csv \
    --proxies proxies.csv --out run1.csv

# 30분쯤 뒤 2차 측정
python3 -m ipalloc proxy-check --assignments out/assignments.csv \
    --proxies proxies.csv --out run2.csv

# 비교
python3 -m ipalloc proxy-sticky --before run1.csv --after run2.csv
```

```
비교 대상 50개 · 유지 42개 · 변경 8개
  g003: 198.51.100.12 → 203.0.113.88
  ...
출구가 바뀐 묶음이 있다. sticky 세션 유효시간이 측정 간격보다 짧다는 뜻이다.
```

`proxy-check` 는 두 가지를 잡는다.

- **묶음이 같은 출구를 공유하는 경우** — sticky 토큰이 묶음별로 갈리지 않았다는 뜻이다.
  `sticky_template` 이 공급사 규칙과 맞는지 확인할 것.
- **인증 실패** — 사용자명 형식이 틀렸을 때 가장 흔하다.

에코 URL 은 기본값 대신 **자체 서비스의 엔드포인트**를 쓰는 편이 정확하다. 서비스가
실제로 보는 IP 가 무엇인지가 알고 싶은 것이지, 제3자 서비스가 보는 IP 가 아니다.

```bash
python3 -m ipalloc proxy-check ... --echo-url http://내서비스/whoami
```

> `proxy-check` 는 `http://` 만 지원한다. 출구 IP 확인에는 평문으로 충분하고, 터널 위에
> TLS 를 다시 올리면 표준 라이브러리만으로는 복잡해진다.

---

## 5. 비밀번호를 다루는 방식

`proxies.csv` 의 `password` 컬럼에는 평문 대신 `${ENV_VAR}` 를 적는다.

```csv
host,port,protocol,username,password
gw1.example.com,8000,socks5,customer1,${PROXY_PW}
```

- `ipalloc` 은 이 참조를 **그대로 들고 다닌다.** 실제 값은 프록시에 붙는 순간
  (`proxy-check`)에만 환경변수에서 읽는다.
- 생성되는 `redsocks/*.conf` 에는 `__PROXY_PASSWORD_g001__` 자리표시자만 들어간다.
  그래서 생성물을 저장소나 배포 아티팩트에 두어도 비밀번호가 따라가지 않는다.
- 서버에서 `fill-secrets.sh` 가 환경변수로 채운다.

```bash
export IPALLOC_PROXY_PASSWORD='...'        # 전 묶음 공통
export IPALLOC_PROXY_PASSWORD_G001='...'   # 묶음별로 다르면 이쪽이 우선
sh fill-secrets.sh
```

**채운 뒤의 `redsocks/*.conf` 에는 평문 비밀번호가 들어간다.** 서버 밖으로 복사하지 말 것.
평문 비밀번호를 CSV 에 직접 적으면 `proxy-export` 가 경고한다.

---

## 6. 서버 구성

```bash
# 진입 호스트 (공인 IP 1개)
apt install wireguard redsocks iptables
sysctl -w net.ipv4.ip_forward=1

cp out/px/wg0.conf /etc/wireguard/
wg-quick up wg0

# 묶음마다 redsocks 인스턴스
for conf in out/px/redsocks/*.conf; do
    redsocks -c "$conf"
done

sh out/px/iptables.sh up
```

`iptables.sh up` 은 적용 전에 기존 규칙을 먼저 걷어낸다. 두 번 돌려도 REDIRECT 가
중복으로 쌓이지 않는다. `sh iptables.sh down` 으로 철거하며, 규칙 하나가 없어도
나머지를 마저 지운다.

---

## 7. 먼저 알아야 할 것

**1. 출구가 고정되지 않을 가능성이 높다.**
한 달에 한 번 20분 접속하는 패턴에서는 매번 새 세션이 열린다. 2장·4장 참고.
**고정 IP 가 요구사항이라면 이 경로는 요구사항을 충족하지 못한다.**

**2. 홉이 하나 늘어 지연과 장애 지점이 늘어난다.**
사용자 → 진입 호스트 → 프록시 → 서비스. 프록시 공급사가 죽으면 그 묶음이 죽는다.
데이터센터 방식에서는 내 호스트만 보면 됐다.

**3. 진입 호스트가 단일 장애점이다.**
IP 1개로 줄인 대가다. 죽으면 1,000명 전원이 끊긴다. 이중화하려면 진입 호스트를
2대 두고 DNS 라운드로빈이나 keepalived 를 붙여야 한다.

**4. 트래픽 과금이면 전체 터널은 위험하다.**
`--allowed-ips 0.0.0.0/0` 이면 사용자가 VPN 을 켜둔 동안의 **모든 트래픽**이 종량
과금을 탄다. 이 경로에서는 서비스 대역만 보내는 분할 터널이 사실상 필수다.

```bash
python3 -m ipalloc proxy-export ... --allowed-ips 198.51.100.0/24
```

**5. 배정표의 `egress_ip` 는 이 경로에서 쓰이지 않는다.**
`assign` 이 묶음을 만들려면 IP 목록이 필요하지만, 실제 출구는 프록시가 정한다.
그 컬럼을 서비스의 허용 IP 목록에 넣으면 안 된다. 허용목록에 넣을 값은
`proxy-check` 가 관측한 실제 출구 IP 다 — 그리고 그건 바뀔 수 있다(1번).

**6. 레지덴셜 풀의 동원 출처.**
상당수 풀이 앱에 번들된 SDK 로 수집된다. 해당 가정이 자기 회선이 쓰인다는 것을
제대로 알고 동의했는지 불명확한 경우가 많고, 고객의 영상 트래픽이 모르는 사람의
가정 회선을 지나간다. 공급사를 고를 때 동원 방식을 확인할 것.

---

## 명령 요약

| 명령 | 용도 |
|---|---|
| `proxy-export --assignments F --proxies F --entry-host H` | 서버·사용자 설정 일습 |
| `proxy-check --assignments F --proxies F [--out F]` | 묶음별 실제 출구 IP 관측 |
| `proxy-sticky --before F --after F` | 두 관측을 비교해 sticky 유지 여부 |
| `cost --users N --dc-ip-monthly P --res-per-gb P` | 데이터센터와 비용 비교 |

`proxy-export` 주요 옵션: `--entry-port`(기본 51820), `--allowed-ips`,
`--dns`, `--wg-interface`(기본 wg0), `--redsocks-base-port`(기본 12000),
`--salt`.
