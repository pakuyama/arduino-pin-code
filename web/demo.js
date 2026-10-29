// 브라우저 데모: PC 연결 프로그램(pin_bridge.py) 없이 화면을 체험할 수 있게 같은 규칙을 흉내 냅니다.
// GitHub Pages처럼 127.0.0.1이 아닌 곳에서 열면 app.js가 이 데모를 씁니다.
// PIN은 이 탭의 메모리에만 있고 저장하지 않습니다. 새로고침하면 1234로 돌아갑니다.
// 데모를 쓸 때만 createPinDemo()를 불러 타이머를 시작합니다.
window.createPinDemo = () => {
  const PIN_MIN = 4, PIN_MAX = 8, RESULT_MS = 3000, LOCK_MS = 30000, MAX_FAILS = 3;
  const listeners = {};
  let pin = "1234", isDefault = true;
  let signal = 0, entry = [], fails = 0, lockedUntil = null, resultOffAt = null, lastResult = null;
  let changeFails = 0, changeBlockUntil = null, nextRequest = 100;

  const nowText = () => new Date().toTimeString().slice(0, 8);
  function emit(name, data) {
    (listeners[name] || []).forEach((cb) => cb({ data: JSON.stringify(data) }));
  }
  function snapshot() {
    return {
      sim: true, demo: true, link_open: true, board_age_ms: 40, profile_ok: true, fw_ok: true, profile: "P03",
      signal, entry_len: entry.length, pin_min: PIN_MIN, pin_max: PIN_MAX, fails, max_fails: MAX_FAILS,
      locked_ms: lockedUntil === null ? null : Math.max(0, lockedUntil - Date.now()), lock_seconds: LOCK_MS / 1000,
      last_result: lastResult, default_pin: isDefault,
      message: "브라우저 데모입니다. 실제 보드 없이 같은 규칙으로 동작합니다.",
    };
  }
  const publishState = () => emit("state", snapshot());
  function notice(text, level = "info") { emit("notice", { text, level, time: nowText() }); }

  // 보드에 명령을 보내고 응답(ack)을 받는 흐름을 짧은 지연으로 흉내 냅니다.
  function send(command, origin) {
    const request = nextRequest = nextRequest >= 65535 ? 100 : nextRequest + 1;
    emit("command", { request, command, origin, status: "sent" });
    setTimeout(() => {
      if (command.startsWith("SIGNAL ")) signal = +command.split(" ")[1];
      if (command === "STOP") signal = 0;
      emit("command", { request, command, origin, status: "applied" });
      publishState();
    }, 60);
    return request;
  }

  function handleKey(key, source) {
    if (lockedUntil !== null) { notice("잠금 중이라 키 입력을 무시했습니다."); return; }
    if (/^[0-9]$/.test(key)) {
      if (entry.length >= PIN_MAX) notice(`PIN은 최대 ${PIN_MAX}자리입니다. # 로 확인하거나 * 로 지우세요.`, "warn");
      else entry.push(key);
    } else if (key === "*") {
      if (entry.length) { entry = []; notice("입력을 지웠습니다."); }
    } else if (key === "#") submit(source);
    else notice(`${key} 키는 쓰지 않습니다. 숫자, * (지우기), # (확인)만 씁니다.`);
  }

  function submit(source) {
    if (!entry.length) { notice("입력한 숫자가 없습니다. 숫자를 누른 뒤 # 를 누르세요."); return; }
    const tried = entry.join(""), length = tried.length;
    entry = [];
    const ok = length >= PIN_MIN && tried === pin;
    let result, reason;
    if (ok) {
      fails = 0; result = "allowed"; reason = "PIN 일치";
      send("SIGNAL 3", "result"); resultOffAt = Date.now() + RESULT_MS;
    } else {
      fails += 1;
      reason = length < PIN_MIN ? `${PIN_MIN}자리보다 짧음` : "PIN 불일치";
      if (fails >= MAX_FAILS) {
        result = "locked";
        reason += ` · ${MAX_FAILS}회 연속 실패로 ${LOCK_MS / 1000}초 잠금`;
        lockedUntil = Date.now() + LOCK_MS; resultOffAt = null;
        send("SIGNAL 2", "lock");
      } else {
        result = "denied";
        send("SIGNAL 1", "result"); resultOffAt = Date.now() + RESULT_MS;
      }
    }
    lastResult = { result, reason, time: nowText(), length, fails, source };
    emit("attempt", lastResult);
  }

  setInterval(() => {
    const now = Date.now();
    if (lockedUntil !== null && now >= lockedUntil) {
      lockedUntil = null; fails = 0;
      send("SIGNAL 0", "unlock");
      notice("잠금이 풀렸습니다. 다시 입력할 수 있습니다.");
    }
    if (resultOffAt !== null && now >= resultOffAt) { resultOffAt = null; send("SIGNAL 0", "result_end"); }
  }, 250);
  setInterval(publishState, 1000);

  function reply(ok, message, extra) { return Promise.resolve(Object.assign({ ok, message }, extra || {})); }

  return {
    // EventSource와 같은 모양으로 이벤트를 받습니다.
    addEventListener(name, cb) {
      (listeners[name] = listeners[name] || []).push(cb);
      if (name === "state") setTimeout(() => cb({ data: JSON.stringify(snapshot()) }), 0);
    },
    post(url, data) {
      data = data || {};
      if (url === "/api/key") {
        const k = data.key;
        if (!(typeof k === "string" && k.length === 1 && "0123456789ABCD*#".includes(k))) return reply(false, "키패드에 없는 키입니다.");
        handleKey(k, "screen"); publishState();
        return reply(true);
      }
      if (url === "/api/signal") {
        if (lockedUntil !== null) return reply(false, "잠금 중에는 신호등을 직접 바꿀 수 없습니다. 잠금이 풀린 뒤 다시 시도하세요.");
        resultOffAt = null;
        return reply(true, "", { request: send(`SIGNAL ${data.value}`, "manual") });
      }
      if (url === "/api/stop") { resultOffAt = null; return reply(true, "", { request: send("STOP", "manual") }); }
      if (url === "/api/pin") {
        const now = Date.now();
        if (changeBlockUntil && now < changeBlockUntil) {
          return reply(false, `현재 PIN을 여러 번 틀려 ${Math.ceil((changeBlockUntil - now) / 1000)}초 뒤에 다시 시도할 수 있습니다.`, { field: "current" });
        }
        if (!/^\d{4,8}$/.test(data.new || "")) return reply(false, `새 PIN은 숫자 ${PIN_MIN}~${PIN_MAX}자리여야 합니다.`, { field: "new" });
        if (data.current !== pin) {
          changeFails += 1;
          if (changeFails >= MAX_FAILS) {
            changeFails = 0; changeBlockUntil = now + LOCK_MS;
            return reply(false, `현재 PIN이 맞지 않습니다. ${MAX_FAILS}회 틀려 ${LOCK_MS / 1000}초 동안 변경을 막습니다.`, { field: "current" });
          }
          return reply(false, "현재 PIN이 맞지 않습니다.", { field: "current" });
        }
        if (data.current === data.new) return reply(false, "새 PIN이 현재 PIN과 같습니다. 다른 숫자를 입력하세요.", { field: "new" });
        changeFails = 0; pin = data.new; isDefault = false; entry = [];
        notice("PIN을 바꿨습니다. 다음 입력부터 새 PIN을 씁니다.");
        publishState();
        return reply(true, "PIN을 바꿨습니다.");
      }
      return reply(false, "없는 주소입니다.");
    },
  };
};
