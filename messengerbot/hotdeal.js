/*
 * 핫딜 봇 — 메신저봇R (Messenger Bot R) 스크립트
 * ================================================
 *
 * ⚠️ 경고
 *   카카오톡은 오픈채팅방에 외부 메시지를 보내는 공식 API를 제공하지 않는다.
 *   이 스크립트는 안드로이드 알림 접근 권한을 이용한 비공식 자동화이며,
 *   카카오 운영정책 위반으로 계정이 제재될 수 있다. 방장 계정이 정지되면
 *   오픈채팅방도 함께 잃는다. 위험을 감수할 수 있을 때만 사용할 것.
 *
 * 동작
 *   BRIDGE_URL 의 /pull 을 POLL_INTERVAL_MS 마다 폴링해서 메시지를 가져오고,
 *   카카오방에 전송한 뒤 /ack 로 확인 응답을 보낸다.
 *   전송에 실패하면 ack 하지 않으므로 임대 만료 후 서버가 다시 내어준다.
 *
 * 설치
 *   1) Play 스토어 또는 배포처에서 메신저봇R 설치
 *   2) 알림 접근 권한 + 배터리 최적화 예외 허용
 *   3) 새 스크립트 생성 후 이 파일 내용 붙여넣기
 *   4) 아래 설정 3개(BRIDGE_URL, BRIDGE_TOKEN, TARGET_ROOM) 수정
 *   5) ★ 중요 ★ 대상 오픈채팅방에서 아무 메시지나 한 번 수신해야
 *      봇이 그 방의 전송 세션을 확보한다. 그 전에는 send 가 실패한다.
 *      스크립트는 세션 없을 때 자동으로 재시도하니 방에 대화가 오면 풀린다.
 */

// ─────────────── 설정 ───────────────
const BRIDGE_URL      = "http://192.168.0.10:8080"; // 브리지 서버 주소 (외부면 https 권장)
const BRIDGE_TOKEN    = "여기에_BRIDGE_TOKEN_붙여넣기";
const TARGET_ROOM     = "여기에_오픈채팅방_정확한_이름";
const POLL_INTERVAL_MS = 60 * 1000;  // 폴링 주기. 너무 짧게 두면 배터리와 트래픽만 먹는다.
const MAX_PER_PULL     = 3;          // 한 번에 가져올 메시지 수. 크게 잡으면 도배로 보인다.
const SEND_GAP_MS      = 2500;       // 연속 전송 간격. 카톡 도배 판정 회피용.
const HTTP_TIMEOUT_MS  = 15000;
// ────────────────────────────────────

const Jsoup = org.jsoup.Jsoup;

/* 스크립트를 다시 컴파일하면 이전 타이머가 살아남아 중복 폴링이 된다.
   전역에 보관해 두고 반드시 취소한다. */
if (typeof global === "undefined") { var global = this; }

function log(message) {
    try { Log.i("[hotdeal] " + message); } catch (e) { /* Log 미지원 버전 */ }
}

function logError(message) {
    try { Log.e("[hotdeal] " + message); } catch (e) { log("ERROR " + message); }
}

// ─────────────── HTTP ───────────────

function httpGet(path) {
    return Jsoup.connect(BRIDGE_URL + path)
        .ignoreContentType(true)
        .ignoreHttpErrors(true)
        .header("X-Bot-Token", BRIDGE_TOKEN)
        .timeout(HTTP_TIMEOUT_MS)
        .method(org.jsoup.Connection.Method.GET)
        .execute()
        .body();
}

function httpPostJson(path, bodyObject) {
    return Jsoup.connect(BRIDGE_URL + path)
        .ignoreContentType(true)
        .ignoreHttpErrors(true)
        .header("X-Bot-Token", BRIDGE_TOKEN)
        .header("Content-Type", "application/json; charset=utf-8")
        .requestBody(JSON.stringify(bodyObject))
        .timeout(HTTP_TIMEOUT_MS)
        .method(org.jsoup.Connection.Method.POST)
        .execute()
        .body();
}

// ─────────────── 카카오 전송 ───────────────

/*
 * 메신저봇R 은 버전에 따라 전송 API 가 다르다.
 *   구버전: Api.replyRoom(room, msg)
 *   신버전: BotManager.getCurrentBot().send(room, msg)
 * 어느 쪽이 있는지 런타임에 탐지한다. 둘 다 없으면 false 를 반환해
 * ack 를 보내지 않게 하고, 메시지는 서버 큐에 남는다.
 */
function sendToRoom(room, text) {
    // 1) 신버전 BotManager API
    try {
        if (typeof BotManager !== "undefined") {
            const bot = BotManager.getCurrentBot();
            if (bot && typeof bot.send === "function") {
                const ok = bot.send(room, text);
                // send 는 boolean 을 반환한다. false 면 방 세션이 없다는 뜻.
                if (ok !== false) return true;
                logError("bot.send 가 false 반환 — '" + room + "' 방 세션이 아직 없습니다. "
                       + "해당 방에서 메시지를 한 번 수신해야 합니다.");
                return false;
            }
        }
    } catch (e) {
        logError("BotManager 전송 실패: " + e);
    }

    // 2) 구버전 Api.replyRoom
    try {
        if (typeof Api !== "undefined" && typeof Api.replyRoom === "function") {
            Api.replyRoom(room, text);
            return true;
        }
    } catch (e) {
        logError("Api.replyRoom 전송 실패: " + e);
        return false;
    }

    logError("사용 가능한 전송 API 를 찾지 못했습니다. 메신저봇R 버전을 확인하세요.");
    return false;
}

// ─────────────── 폴링 1회 ───────────────

function pollOnce() {
    let body;
    try {
        body = httpGet("/pull?room=" + encodeURIComponent(TARGET_ROOM) + "&max=" + MAX_PER_PULL);
    } catch (e) {
        // 네트워크 실패는 흔하다. 다음 주기에 그냥 다시 시도한다.
        logError("브리지 연결 실패: " + e);
        return;
    }

    let payload;
    try {
        payload = JSON.parse(body);
    } catch (e) {
        logError("브리지 응답 파싱 실패: " + String(body).substring(0, 200));
        return;
    }

    if (!payload.ok) {
        logError("브리지 오류: " + (payload.error || body));
        return;
    }

    const messages = payload.messages || [];
    if (messages.length === 0) return;

    log(messages.length + "건 수신");

    const acked = [];
    for (let i = 0; i < messages.length; i++) {
        const message = messages[i];
        if (sendToRoom(TARGET_ROOM, message.text)) {
            acked.push(message.id);
        } else {
            // 전송 실패 시 ack 하지 않는다 → 서버가 임대 만료 후 재전달.
            // 뒤 메시지도 어차피 같은 이유로 실패하므로 여기서 중단한다.
            break;
        }
        if (i < messages.length - 1) {
            java.lang.Thread.sleep(SEND_GAP_MS);
        }
    }

    if (acked.length > 0) {
        try {
            httpPostJson("/ack", { ids: acked });
            log(acked.length + "건 전송 완료 및 ack");
        } catch (e) {
            // ack 실패는 중복 전송으로 이어질 수 있으나, 유실보다는 낫다.
            logError("ack 실패 (임대 만료 시 재전달될 수 있음): " + e);
        }
    }
}

// ─────────────── 타이머 수명 관리 ───────────────

function startTimer() {
    stopTimer();
    const timer = new java.util.Timer();
    timer.scheduleAtFixedRate(
        new java.util.TimerTask({
            run: function () {
                try {
                    pollOnce();
                } catch (e) {
                    // 타이머 태스크에서 예외가 새어나가면 타이머 자체가 죽는다.
                    logError("폴링 중 예외: " + e);
                }
            }
        }),
        3000,               // 첫 실행 지연
        POLL_INTERVAL_MS
    );
    global.__hotdealTimer = timer;
    log("폴링 시작 (" + (POLL_INTERVAL_MS / 1000) + "초 주기, 방: " + TARGET_ROOM + ")");
}

function stopTimer() {
    if (global.__hotdealTimer) {
        try { global.__hotdealTimer.cancel(); } catch (e) { /* 이미 취소됨 */ }
        global.__hotdealTimer = null;
        log("이전 폴링 타이머 정리");
    }
}

// ─────────────── 메신저봇R 수명주기 훅 ───────────────

function onStartCompile() {
    // 스크립트 재컴파일 직전. 반드시 이전 타이머를 죽여야 중복 폴링을 막는다.
    stopTimer();
}

function response(room, msg, sender, isGroupChat, replier, imageDB, packageName) {
    /* 메시지 수신 자체는 봇 동작에 필요 없지만, 이 콜백이 불려야
       해당 방의 전송 세션이 확보된다. 그래서 비워두지 않고 유지한다.
       "!핫딜상태" 로 살아있는지 확인할 수 있게 해 뒀다. */
    if (room === TARGET_ROOM && msg === "!핫딜상태") {
        const alive = global.__hotdealTimer ? "폴링 중" : "정지 상태";
        replier.reply("핫딜봇: " + alive + " / 주기 " + (POLL_INTERVAL_MS / 1000) + "초");
    }
}

startTimer();
