// PIN 출입 신호등 화면. 연결 프로그램(pin_bridge.py)의 이벤트만 받고, 보드에 직접 접속하지 않습니다.
(() => {
  const $ = (id) => document.getElementById(id);
  const SIGNAL_NAMES = { 0: "꺼짐", 1: "빨강", 2: "노랑", 3: "초록" };
  const RESULT_TEXT = { allowed: "허용", denied: "거부", locked: "거부 · 잠금" };
  const ORIGIN_TEXT = { manual: "직접 조작", result: "결과 표시", lock: "잠금 표시", unlock: "잠금 해제", result_end: "자동 끄기", connect: "연결 확인" };
  const STALE_MS = 3000;
  // 이 PC의 연결 프로그램에서 연 화면이 아니면(예: GitHub Pages) 브라우저 데모로 동작합니다.
  const DEMO = !["127.0.0.1", "localhost"].includes(location.hostname);
  const demo = DEMO ? window.createPinDemo() : null;

  let state = null;
  let lastEventAt = 0;
  let lastStateAt = 0;
  let lockEndsAt = null;
  let lastConn = "";
  let lastEntryLen = 0;
  let wasLocked = false;

  // ---------- 알림 ----------
  function announce(text) {
    const el = $("live");
    el.textContent = "";
    setTimeout(() => { el.textContent = text; }, 50);
  }
  function alertNow(text) {
    const el = $("alert");
    el.textContent = "";
    setTimeout(() => { el.textContent = text; }, 50);
  }

  // ---------- 연결 상태 ----------
  function connStatus() {
    if (!state || Date.now() - lastEventAt > STALE_MS) return ["bad", "연결 프로그램 응답 없음"];
    if (!state.link_open) return ["bad", "보드 연결 안 됨"];
    if (state.board_age_ms === null) return ["wait", "보드 응답 기다리는 중"];
    if (state.board_age_ms > STALE_MS) return ["warn", `보드 응답 없음 (${Math.round(state.board_age_ms / 1000)}초)`];
    if (!state.profile_ok || !state.fw_ok) return ["warn", "회로·코드 확인 필요"];
    return ["ok", state.demo ? "데모 보드 연결됨" : state.sim ? "시뮬레이터 연결됨" : "보드 연결됨"];
  }
  function controllable() {
    return connStatus()[0] === "ok";
  }

  function renderConn() {
    const [kind, text] = connStatus();
    const el = $("conn");
    el.className = "pill pill-" + kind;
    $("conn-text").textContent = text;
    if (lastConn && lastConn !== kind + text && kind !== "ok") announce(text);
    if (lastConn && lastConn.startsWith("bad") && kind === "ok") announce("연결되었습니다.");
    lastConn = kind + text;
  }

  // ---------- 화면 그리기 ----------
  function render() {
    renderConn();
    if (!state) return;
    const s = state;

    const banner = $("banner");
    if (s.demo) {
      banner.className = "banner"; banner.hidden = false;
      banner.innerHTML = "<b>브라우저 데모</b>입니다. 실제 보드 없이 화면 키패드로 체험합니다. 처음 PIN은 1234이며, 바꾼 PIN은 새로고침하면 사라집니다. 실제 보드 연결 방법은 <a href=\"https://github.com/pakuyama/arduino-pin-code#readme\">GitHub README</a>를 보세요.";
    } else if (s.profile && !s.profile_ok) {
      banner.className = "banner bad"; banner.hidden = false;
      banner.textContent = `보드에 ${s.profile} 회로 코드가 올라가 있습니다. P03 배선과 P03_PIN 코드가 필요합니다.`;
    } else if (s.profile_ok && !s.fw_ok) {
      banner.className = "banner"; banner.hidden = false;
      banner.textContent = "강의 기준 P03 코드가 올라가 있습니다. firmware/P03_PIN 을 업로드해야 PIN 입력을 받습니다.";
    } else if (s.default_pin) {
      banner.className = "banner"; banner.hidden = false;
      banner.textContent = "처음 설정된 PIN(1234)을 쓰는 중입니다. 아래 'PIN 바꾸기'에서 바꾸세요.";
    } else banner.hidden = true;
    $("board-msg").textContent = s.message || "";

    // 입력 진행: 숫자 자체는 받지 않고 자리 수만 받습니다.
    const n = s.entry_len;
    const slots = Math.min(s.pin_max, Math.max(s.pin_min, n));
    $("dots").innerHTML = Array.from({ length: slots }, (_, i) => `<span class="${i < n ? "on" : ""}"></span>`).join("");
    $("entry-text").textContent = n ? `${n}자리 입력됨` : "입력 없음";
    if (n !== lastEntryLen) {
      if (n > 0) announce(`${n}자리 입력됨`);
      lastEntryLen = n;
    }

    // 잠금
    lockEndsAt = s.locked_ms === null ? null : lastStateAt + s.locked_ms;
    const locked = lockEndsAt !== null;
    if (locked && !wasLocked) alertNow(`${s.max_fails}회 연속 실패로 ${s.lock_seconds}초 동안 잠겼습니다.`);
    if (!locked && wasLocked) announce("잠금이 풀렸습니다. 다시 입력할 수 있습니다.");
    wasLocked = locked;
    $("fails-text").textContent = `연속 실패 ${s.fails} / ${s.max_fails}회`;
    renderLock();

    // 신호등: 보드가 보고한 값 기준
    const sig = s.signal;
    document.querySelectorAll(".lamp").forEach((l) => l.classList.toggle("on", sig !== null && +l.dataset.v === sig));
    const known = sig !== null && sig !== undefined && connStatus()[0] === "ok";
    $("signal-now").textContent = known ? `보드 상태: ${SIGNAL_NAMES[sig]}` : "보드 상태 모름 (꺼졌다고 가정하지 않음)";
    const can = controllable();
    document.querySelectorAll(".btn.sig").forEach((b) => {
      b.setAttribute("aria-pressed", String(known && +b.dataset.v === sig));
      b.disabled = !can || locked;
    });
    $("stop").disabled = !(s.link_open);

    // 화면 키패드: 보드가 응답할 때만 입력을 받습니다(결과 LED를 켤 수 있어야 하므로).
    const padOk = can && s.fw_ok;
    document.querySelectorAll("#keypad button").forEach((b) => { b.setAttribute("aria-disabled", String(!padOk)); });
    $("keypad").classList.toggle("off", !padOk);
  }

  function renderLock() {
    if (!state) return;
    const total = state.lock_seconds * 1000;
    if (lockEndsAt === null) {
      $("lock-text").textContent = "잠기지 않음";
      $("lock-bar").style.width = "0";
      return;
    }
    const left = Math.max(0, lockEndsAt - Date.now());
    $("lock-text").textContent = `잠김 · ${Math.ceil(left / 1000)}초 남음`;
    $("lock-bar").style.width = `${(left / total) * 100}%`;
  }

  function renderResult(r) {
    const el = $("result");
    el.className = "result " + r.result;
    $("result-text").textContent = `${RESULT_TEXT[r.result]} · ${r.time}`;
    $("result-detail").textContent = `${r.reason} · ${r.length}자리 입력`;
  }

  function addLog(r) {
    const tbody = $("log");
    const empty = tbody.querySelector(".empty");
    if (empty) empty.remove();
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="t"></td><td><span class="result ${r.result}"><span class="shape" aria-hidden="true"></span><span></span></span></td><td></td><td></td><td></td>`;
    tr.children[0].textContent = r.time;
    tr.children[1].querySelector("span span:last-child").textContent = RESULT_TEXT[r.result];
    tr.children[2].textContent = r.source === "screen" ? "화면 키패드" : "보드 키패드";
    tr.children[3].textContent = `${r.length}자리`;
    tr.children[4].textContent = r.reason;
    tbody.prepend(tr);
    while (tbody.rows.length > 200) tbody.lastElementChild.remove();
  }

  // ---------- 이벤트 ----------
  function connect() {
    const es = DEMO ? demo : new EventSource("/api/events");
    es.addEventListener("state", (e) => { lastEventAt = lastStateAt = Date.now(); state = JSON.parse(e.data); render(); });
    es.addEventListener("attempt", (e) => {
      lastEventAt = Date.now();
      const r = JSON.parse(e.data);
      renderResult(r);
      addLog(r);
      if (r.result !== "locked") announce(`${RESULT_TEXT[r.result]}. ${r.reason}.`);
    });
    es.addEventListener("command", (e) => {
      lastEventAt = Date.now();
      const c = JSON.parse(e.data);
      if (c.origin === "connect") return;
      const who = ORIGIN_TEXT[c.origin] || c.origin;
      const text = {
        sent: `${who}: ${c.command} 전송 중…`,
        applied: `${who}: ${c.command} 적용됨 (보드 응답)`,
        error: `${who}: ${c.command} 거절됨 (${c.code}). 명령과 회로를 확인하세요.`,
        timeout: `${who}: ${c.command} 응답 없음. 적용 여부를 알 수 없고, 자동으로 다시 보내지 않았습니다.`,
      }[c.status];
      if (text) $("cmd").textContent = text;
    });
    es.addEventListener("notice", (e) => {
      lastEventAt = Date.now();
      const n = JSON.parse(e.data);
      $("notice").textContent = `${n.time} ${n.text}`;
      announce(n.text);
    });
  }

  async function post(url, data) {
    if (DEMO) return demo.post(url, data);
    try {
      const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
      return await res.json();
    } catch {
      return { ok: false, message: "연결 프로그램에 요청을 보내지 못했습니다. 프로그램이 실행 중인지 확인하세요." };
    }
  }

  // ---------- 조작 ----------
  document.querySelectorAll(".btn.sig").forEach((b) => b.addEventListener("click", async () => {
    const r = await post("/api/signal", { value: +b.dataset.v });
    if (!r.ok) $("cmd").textContent = r.message;
  }));
  $("stop").addEventListener("click", async () => {
    const r = await post("/api/stop");
    if (!r.ok) $("cmd").textContent = r.message;
  });

  const KEYS = ["1", "2", "3", "A", "4", "5", "6", "B", "7", "8", "9", "C", "*", "0", "#", "D"];
  const KEY_LABEL = { "*": "* 지우기", "#": "# 확인" };
  $("keypad").innerHTML = KEYS.map((k) => `<button type="button" class="btn" data-k="${k}" aria-label="${KEY_LABEL[k] || k}">${k}</button>`).join("");
  // 버튼은 disabled 대신 aria-disabled를 써서 포커스를 잃지 않게 하고, 막힌 이유를 알려 줍니다.
  async function pressKey(k) {
    $("pad-msg").textContent = "";
    if ($("keypad").classList.contains("off")) {
      $("pad-msg").textContent = "보드가 응답할 때만 입력할 수 있습니다. 위쪽 연결 상태를 확인하세요.";
      return;
    }
    const r = await post("/api/key", { key: k });
    if (!r.ok) $("pad-msg").textContent = r.message;
  }
  $("keypad").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-k]");
    if (b) pressKey(b.dataset.k);
  });
  const KEYBOARD = { Enter: "#", Backspace: "*", Escape: "*" };
  $("keypad").addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    let k = null;
    if (/^[0-9]$/.test(e.key)) k = e.key;
    else if (KEYBOARD[e.key]) k = KEYBOARD[e.key];
    else if (e.key === "*" || e.key === "#") k = e.key;
    if (k === null) return;
    e.preventDefault(); // Enter가 포커스된 버튼을 한 번 더 누르지 않게 합니다.
    pressKey(k);
    const target = $("keypad").querySelector(`button[data-k="${CSS.escape(k)}"]`);
    if (target) target.focus();
  });

  // ---------- PIN 바꾸기 ----------
  const form = $("pin-form");
  const fields = { current: $("pin-current"), new: $("pin-new"), confirm: $("pin-confirm") };
  const LABEL = { current: "현재 PIN", new: "새 PIN", confirm: "새 PIN 다시 입력" };

  function setError(name, msg) {
    fields[name].setAttribute("aria-invalid", msg ? "true" : "false");
    $(`pin-${name}-err`).textContent = msg || "";
  }
  function showSummary(errors) {
    const box = $("pin-summary");
    if (!errors.length) { box.hidden = true; box.textContent = ""; return; }
    box.hidden = false;
    box.innerHTML = `<p><b>고칠 곳 ${errors.length}개</b></p><ul>${errors.map(([n, m]) => `<li><a href="#pin-${n}">${LABEL[n]}</a>: ${m}</li>`).join("")}</ul>`;
    box.focus();
  }

  $("pin-show").addEventListener("change", (e) => {
    Object.values(fields).forEach((f) => { f.type = e.target.checked ? "text" : "password"; });
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    $("pin-status").textContent = "";
    const v = Object.fromEntries(Object.entries(fields).map(([k, f]) => [k, f.value.trim()]));
    const errors = [];
    const digits = /^\d{4,8}$/;
    if (!v.current) errors.push(["current", "현재 PIN을 입력하세요."]);
    if (!digits.test(v.new)) errors.push(["new", "숫자 4~8자리로 입력하세요. 예: 2580"]);
    else if (v.new === v.current) errors.push(["new", "현재 PIN과 다른 숫자를 입력하세요."]);
    if (digits.test(v.new) && v.confirm !== v.new) errors.push(["confirm", "새 PIN과 똑같이 입력하세요."]);
    Object.keys(fields).forEach((n) => setError(n, (errors.find(([k]) => k === n) || [])[1]));
    if (errors.length) { showSummary(errors); return; }
    showSummary([]);

    const r = await post("/api/pin", { current: v.current, new: v.new });
    if (r.ok) {
      Object.values(fields).forEach((f) => { f.value = ""; });
      $("pin-status").textContent = "PIN을 바꿨습니다. 다음 입력부터 새 PIN을 씁니다.";
    } else if (r.field && fields[r.field]) {
      setError(r.field, r.message);
      showSummary([[r.field, r.message]]);
    } else {
      $("pin-status").textContent = r.message;
    }
  });

  // ---------- 시작 ----------
  connect();
  setInterval(() => { renderConn(); renderLock(); if (state) render(); }, 500);
})();
