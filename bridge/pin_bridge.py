"""P03_PIN 연결 프로그램.

COM 포트는 이 프로그램 하나만 엽니다. 웹 화면은 이 프로그램의 API만 씁니다.
보드는 키 입력만 보내고, PIN 비교·잠금·LED 시간 관리는 여기서 합니다.
입력한 PIN 숫자는 웹 화면에도 기록에도 남기지 않습니다.

실행 예:
  python bridge/pin_bridge.py                 (기본 COM3)
  python bridge/pin_bridge.py --serial COM4   (다른 포트)
  python bridge/pin_bridge.py --simulate      (보드 없이 화면 시험)
"""
import argparse
import hashlib
import hmac
import json
import queue
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "web"
PIN_FILE = ROOT / "pin.json"

PROFILE = "P03"
FIRMWARE = "pin1"
PIN_MIN, PIN_MAX = 4, 8
DEFAULT_PIN = "1234"
RESULT_SECONDS = 3      # 허용·거부 LED를 켜 두는 시간
LOCK_SECONDS = 30       # 연속 실패 뒤 잠금 시간
MAX_FAILS = 3
ACK_TIMEOUT = 2.0       # 이 시간 안에 응답이 없으면 '적용 여부 모름'으로 알림. 자동 재전송은 하지 않음
SIGNAL_NAMES = {0: "꺼짐", 1: "빨강", 2: "노랑", 3: "초록"}


def now_text():
    return datetime.now().strftime("%H:%M:%S")


# ---------- PIN 저장 ----------
class PinStore:
    """PIN은 솔트를 붙인 해시로만 저장합니다."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.salt = bytes.fromhex(data["salt"])
            self.digest = bytes.fromhex(data["hash"])
            self.is_default = bool(data.get("default", False))
        except (OSError, ValueError, KeyError):
            self._write(DEFAULT_PIN, default=True)

    @staticmethod
    def _hash(pin, salt):
        return hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, 100_000)

    def _write(self, pin, default):
        self.salt = secrets.token_bytes(16)
        self.digest = self._hash(pin, self.salt)
        self.is_default = default
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"salt": self.salt.hex(), "hash": self.digest.hex(), "default": default}), encoding="utf-8")
        tmp.replace(self.path)

    def check(self, pin):
        with self.lock:
            return hmac.compare_digest(self._hash(pin, self.salt), self.digest)

    def set(self, pin):
        with self.lock:
            self._write(pin, default=False)


def valid_pin_format(pin):
    return isinstance(pin, str) and pin.isascii() and pin.isdigit() and PIN_MIN <= len(pin) <= PIN_MAX


# ---------- 보드 연결 ----------
class SerialLink:
    def __init__(self, port):
        import serial  # pyserial==3.5 (강의 pc/requirements.txt)
        self.port = serial.Serial(port, 115200, timeout=0.25)
        time.sleep(2)  # 포트를 열면 UNO가 재시작할 수 있습니다.
        self.port.reset_input_buffer()

    def readline(self):
        return self.port.readline()

    def write(self, data):
        self.port.write(data)

    def close(self):
        self.port.close()


class SimLink:
    """P03_PIN 펌웨어와 같은 형식으로 응답하는 가짜 보드."""

    def __init__(self):
        self.out = queue.Queue()
        self.signal = 0
        self.seq = 0
        self.last_key = "-"
        self.start = time.monotonic()
        self.next_state = 0.0

    def _ms(self):
        return int((time.monotonic() - self.start) * 1000)

    def _state(self, request):
        return json.dumps({"v": 1, "type": "state", "profile": PROFILE, "request": request, "ms": self._ms(),
                           "fw": FIRMWARE, "last_key": self.last_key, "key_seq": self.seq, "signal": self.signal})

    def _put(self, text):
        self.out.put((text + "\n").encode("ascii"))

    def write(self, data):
        for line in data.decode("ascii").splitlines():
            parts = line.split(" ")
            if len(parts) < 2 or not parts[0].isdigit():
                self._put('{"v":1,"type":"error","request":0,"code":"format"}')
                continue
            rid, name, args = int(parts[0]), parts[1], parts[2:]
            if name == "STATUS" and not args:
                self._put(self._state(rid))
            elif name == "PING" and not args:
                self._put(json.dumps({"v": 1, "type": "ack", "request": rid, "applied": True}))
            elif name == "STOP" and not args:
                self.signal = 0
                self._put(json.dumps({"v": 1, "type": "ack", "request": rid, "applied": True}))
                self._put(self._state(rid))
            elif name == "SIGNAL" and len(args) == 1 and args[0] in ("0", "1", "2", "3"):
                self.signal = int(args[0])
                self._put(json.dumps({"v": 1, "type": "ack", "request": rid, "applied": True}))
            else:
                self._put(json.dumps({"v": 1, "type": "error", "request": rid, "code": "unsupported_or_range"}))

    def readline(self):
        now = time.monotonic()
        if now >= self.next_state:
            self.next_state = now + 1
            return (self._state(0) + "\n").encode("ascii")
        try:
            return self.out.get(timeout=min(0.25, self.next_state - now))
        except queue.Empty:
            return b""

    def close(self):
        pass


# ---------- 핵심 상태 ----------
class Bridge:
    def __init__(self, link_factory, simulate, store):
        self.link_factory = link_factory
        self.simulate = simulate
        self.store = store
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.subscribers = []
        self.link = None
        self.board = {"profile": None, "fw": None, "signal": None}
        self.last_seen = None
        self.status_request = None
        self.last_seq = None
        self.entry = []
        self.fails = 0
        self.locked_until = None
        self.result_off_at = None
        self.last_result = None
        self.message = "보드에 연결하는 중입니다."
        self.next_request = 100
        self.pending = {}
        self.change_fails = 0
        self.change_block_until = None

    # --- 웹으로 보내기 ---
    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, event, data):
        text = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(text)
                except queue.Full:
                    self.subscribers.remove(q)

    def notice(self, text, level="info"):
        self.publish("notice", {"text": text, "level": level, "time": now_text()})

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            return {
                "sim": self.simulate,
                "link_open": self.link is not None,
                "board_age_ms": None if self.last_seen is None else int((now - self.last_seen) * 1000),
                "profile_ok": self.board["profile"] == PROFILE,
                "fw_ok": self.board["fw"] == FIRMWARE,
                "profile": self.board["profile"],
                "signal": self.board["signal"],
                "entry_len": len(self.entry),
                "pin_min": PIN_MIN,
                "pin_max": PIN_MAX,
                "fails": self.fails,
                "max_fails": MAX_FAILS,
                "locked_ms": None if self.locked_until is None else max(0, int((self.locked_until - now) * 1000)),
                "lock_seconds": LOCK_SECONDS,
                "last_result": self.last_result,
                "default_pin": self.store.is_default,
                "message": self.message,
            }

    def publish_state(self):
        self.publish("state", self.snapshot())

    # --- 보드로 보내기 ---
    def send(self, command, origin):
        with self.lock:
            if self.link is None:
                raise RuntimeError("보드가 연결되어 있지 않습니다.")
            rid = self.next_request
            self.next_request = 100 if self.next_request >= 65535 else self.next_request + 1
            self.pending[rid] = {"command": command, "origin": origin, "sent": time.monotonic()}
            link = self.link
        with self.write_lock:
            link.write(f"{rid} {command}\n".encode("ascii"))
        self.publish("command", {"request": rid, "command": command, "origin": origin, "status": "sent"})
        return rid

    def controllable(self):
        s = self.snapshot()
        return s["link_open"] and s["profile_ok"] and s["board_age_ms"] is not None and s["board_age_ms"] < 3000

    # --- 보드에서 받기 ---
    def on_line(self, raw):
        try:
            msg = json.loads(raw.decode("ascii"))
        except (UnicodeError, ValueError):
            return
        if not isinstance(msg, dict) or msg.get("v") != 1:
            return
        kind = msg.get("type")
        with self.lock:
            if kind == "state":
                self.last_seen = time.monotonic()
                first = self.board["profile"] is None
                self.board.update(profile=msg.get("profile"), fw=msg.get("fw"), signal=msg.get("signal"))
                if msg.get("request") == self.status_request or first:
                    self.status_request = None
                    self._check_profile()
            elif kind == "key":
                self.last_seen = time.monotonic()
                seq = msg.get("seq")
                if seq == self.last_seq:
                    return
                self.last_seq = seq
                key = msg.get("key")
                if isinstance(key, str) and len(key) == 1:
                    self.handle_key(key, "keypad")
            elif kind in ("ack", "error"):
                rid = msg.get("request")
                info = self.pending.pop(rid, None)
                if info:
                    ok = kind == "ack"
                    # 보드 프로그램이 적용한 값을 바로 반영합니다(실제 LED 발광 관찰은 아님).
                    if ok and info["command"].startswith("SIGNAL "):
                        self.board["signal"] = int(info["command"].split()[1])
                    elif ok and info["command"] == "STOP":
                        self.board["signal"] = 0
                    self.publish("command", {"request": rid, "command": info["command"], "origin": info["origin"],
                                             "status": "applied" if ok else "error", "code": msg.get("code")})
        self.publish_state()

    def _check_profile(self):
        p, fw = self.board["profile"], self.board["fw"]
        if p != PROFILE:
            self.message = f"이 보드는 {p} 회로입니다. P03 배선과 P03_PIN 코드가 필요합니다. 명령을 보내지 않습니다."
        elif fw != FIRMWARE:
            self.message = "강의 기준 P03 코드가 올라가 있습니다. firmware/P03_PIN을 업로드해야 PIN 입력을 받을 수 있습니다."
        else:
            self.message = "P03_PIN 보드와 연결되었습니다."

    # --- PIN 로직 ---
    def handle_key(self, key, source):
        """source: 'keypad'(보드의 실제 키패드) 또는 'screen'(웹 화면 키패드)."""
        if self.board["fw"] != FIRMWARE:
            return
        if self.locked_until is not None:
            self.notice("잠금 중이라 키 입력을 무시했습니다.")
            return
        if key.isdigit():
            if len(self.entry) >= PIN_MAX:
                self.notice(f"PIN은 최대 {PIN_MAX}자리입니다. # 로 확인하거나 * 로 지우세요.", "warn")
            else:
                self.entry.append(key)
        elif key == "*":
            if self.entry:
                self.entry.clear()
                self.notice("입력을 지웠습니다.")
        elif key == "#":
            self.submit(source)
        else:
            self.notice(f"{key} 키는 쓰지 않습니다. 숫자, * (지우기), # (확인)만 씁니다.")

    def submit(self, source):
        if not self.entry:
            self.notice("입력한 숫자가 없습니다. 숫자를 누른 뒤 # 를 누르세요.")
            return
        pin = "".join(self.entry)
        length = len(pin)
        self.entry.clear()
        ok = length >= PIN_MIN and self.store.check(pin)
        del pin
        if ok:
            self.fails = 0
            result, reason = "allowed", "PIN 일치"
            self._signal_for_result(3)
        else:
            self.fails += 1
            reason = f"{PIN_MIN}자리보다 짧음" if length < PIN_MIN else "PIN 불일치"
            if self.fails >= MAX_FAILS:
                result = "locked"
                reason += f" · {MAX_FAILS}회 연속 실패로 {LOCK_SECONDS}초 잠금"
                self.locked_until = time.monotonic() + LOCK_SECONDS
                self.result_off_at = None
                self._try_send("SIGNAL 2", "lock")
            else:
                result = "denied"
                self._signal_for_result(1)
        self.last_result = {"result": result, "reason": reason, "time": now_text(), "length": length, "fails": self.fails,
                            "source": source}
        self.publish("attempt", self.last_result)

    def _signal_for_result(self, value):
        if self._try_send(f"SIGNAL {value}", "result"):
            self.result_off_at = time.monotonic() + RESULT_SECONDS

    def _try_send(self, command, origin):
        try:
            self.send(command, origin)
            return True
        except (RuntimeError, OSError) as e:
            self.notice(f"{command} 전송 실패: {e}", "error")
            return False

    # --- 주기 처리 ---
    def tick(self):
        with self.lock:
            now = time.monotonic()
            if self.locked_until is not None and now >= self.locked_until:
                self.locked_until = None
                self.fails = 0
                self._try_send("SIGNAL 0", "unlock")
                self.notice("잠금이 풀렸습니다. 다시 입력할 수 있습니다.")
            if self.result_off_at is not None and now >= self.result_off_at:
                self.result_off_at = None
                self._try_send("SIGNAL 0", "result_end")
            for rid, info in list(self.pending.items()):
                if now - info["sent"] > ACK_TIMEOUT:
                    del self.pending[rid]
                    self.publish("command", {"request": rid, "command": info["command"], "origin": info["origin"],
                                             "status": "timeout"})

    # --- 웹에서 온 조작 ---
    def manual_signal(self, value):
        with self.lock:
            if self.locked_until is not None:
                raise ValueError("잠금 중에는 신호등을 직접 바꿀 수 없습니다. 잠금이 풀린 뒤 다시 시도하세요.")
            if not self.controllable():
                raise ValueError("보드 응답이 없어 명령을 보내지 않았습니다. 연결 상태를 확인하세요.")
            self.result_off_at = None
            return self.send(f"SIGNAL {value}", "manual")

    def stop(self):
        with self.lock:
            if self.link is None:
                raise ValueError("보드가 연결되어 있지 않습니다.")
            self.result_off_at = None
            return self.send("STOP", "manual")

    def change_pin(self, current, new):
        with self.lock:
            now = time.monotonic()
            if self.change_block_until and now < self.change_block_until:
                left = int(self.change_block_until - now) + 1
                raise ValueError(f"현재 PIN을 여러 번 틀려 {left}초 뒤에 다시 시도할 수 있습니다.", "current")
            if not valid_pin_format(new):
                raise ValueError(f"새 PIN은 숫자 {PIN_MIN}~{PIN_MAX}자리여야 합니다.", "new")
            if not isinstance(current, str) or not self.store.check(current):
                self.change_fails += 1
                if self.change_fails >= MAX_FAILS:
                    self.change_fails = 0
                    self.change_block_until = now + LOCK_SECONDS
                    raise ValueError(f"현재 PIN이 맞지 않습니다. {MAX_FAILS}회 틀려 {LOCK_SECONDS}초 동안 변경을 막습니다.", "current")
                raise ValueError("현재 PIN이 맞지 않습니다.", "current")
            if current == new:
                raise ValueError("새 PIN이 현재 PIN과 같습니다. 다른 숫자를 입력하세요.", "new")
            self.change_fails = 0
            self.store.set(new)
            self.entry.clear()
        self.notice("PIN을 바꿨습니다. 다음 입력부터 새 PIN을 씁니다.")
        self.publish_state()

    def screen_press(self, key):
        """웹 화면 키패드 입력. 실제 키패드와 같은 PIN 검사·잠금 규칙을 거칩니다."""
        if not (isinstance(key, str) and len(key) == 1 and key in "0123456789ABCD*#"):
            raise ValueError("키패드에 없는 키입니다.")
        with self.lock:
            if not self.controllable():
                raise ValueError("보드 응답이 없어 입력을 받지 않았습니다. 결과 LED를 켤 수 없기 때문입니다.")
            if self.board["fw"] != FIRMWARE:
                raise ValueError("보드에 P03_PIN 코드가 필요합니다.")
            self.handle_key(key, "screen")
        self.publish_state()

    # --- 스레드 ---
    def reader_loop(self):
        while True:
            try:
                link = self.link_factory()
                with self.lock:
                    self.link = link
                    self.last_seq = None
                    self.board = {"profile": None, "fw": None, "signal": None}
                    self.message = "보드 응답을 기다리는 중입니다."
                self.status_request = self.send("STATUS", "connect")
                while True:
                    raw = link.readline()
                    if raw:
                        self.on_line(raw)
            except Exception as e:  # 포트 없음·케이블 분리 등
                with self.lock:
                    if self.link is not None:
                        try:
                            self.link.close()
                        except Exception:
                            pass
                    self.link = None
                    self.last_seen = None
                    self.entry.clear()
                    self.message = f"보드 연결 실패: {e}. 3초 뒤 다시 시도합니다. 시리얼 모니터가 같은 포트를 열고 있지 않은지 확인하세요."
                self.publish_state()
                time.sleep(3)

    def ticker_loop(self):
        n = 0
        while True:
            time.sleep(0.25)
            self.tick()
            n += 1
            if n % 4 == 0:  # 보드 상태가 끊겨도 화면이 '응답 없음'을 알 수 있게 1초마다 보냄
                self.publish_state()


# ---------- HTTP ----------
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/demo.js": ("demo.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


def make_handler(bridge, http_port):
    allowed_hosts = {f"127.0.0.1:{http_port}", f"localhost:{http_port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "P03PinBridge"

        def log_message(self, fmt, *args):
            pass

        def _host_ok(self):
            return self.headers.get("Host") in allowed_hosts

        def _json(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "message": "허용되지 않은 주소입니다."})
            path = self.path.split("?")[0]
            if path in STATIC:
                name, ctype = STATIC[path]
                body = (WEB / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._json(200, bridge.snapshot())
            elif path == "/api/events":
                self._events()
            else:
                self._json(404, {"ok": False, "message": "없는 주소입니다."})

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = bridge.subscribe()
            try:
                self.wfile.write(f"event: state\ndata: {json.dumps(bridge.snapshot(), ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        text = q.get(timeout=15)
                    except queue.Empty:
                        text = ": keepalive\n\n"
                    self.wfile.write(text.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                bridge.unsubscribe(q)

        def do_POST(self):
            # 다른 사이트가 이 API를 부르지 못하게 주소·Origin·JSON 형식을 확인합니다.
            origin = self.headers.get("Origin")
            if not self._host_ok() or (origin and origin.split("://", 1)[-1] not in allowed_hosts):
                return self._json(403, {"ok": False, "message": "허용되지 않은 요청입니다."})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"ok": False, "message": "JSON 요청만 받습니다."})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1024:
                return self._json(413, {"ok": False, "message": "요청이 너무 깁니다."})
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "message": "요청 형식이 잘못되었습니다."})
            try:
                if self.path == "/api/signal":
                    value = data.get("value")
                    if type(value) is not int or value not in SIGNAL_NAMES:
                        raise ValueError("신호 값은 0~3입니다.")
                    rid = bridge.manual_signal(value)
                    return self._json(200, {"ok": True, "request": rid, "message": f"{SIGNAL_NAMES[value]} 명령을 보냈습니다."})
                if self.path == "/api/stop":
                    rid = bridge.stop()
                    return self._json(200, {"ok": True, "request": rid, "message": "STOP 명령을 보냈습니다."})
                if self.path == "/api/pin":
                    bridge.change_pin(data.get("current"), data.get("new"))
                    return self._json(200, {"ok": True, "message": "PIN을 바꿨습니다."})
                if self.path == "/api/key":
                    bridge.screen_press(data.get("key"))
                    return self._json(200, {"ok": True})
                return self._json(404, {"ok": False, "message": "없는 주소입니다."})
            except ValueError as e:
                field = e.args[1] if len(e.args) > 1 else None
                return self._json(400, {"ok": False, "message": str(e.args[0]), "field": field})
            except (RuntimeError, OSError) as e:
                return self._json(503, {"ok": False, "message": str(e)})

    return Handler


def main():
    p = argparse.ArgumentParser(description="P03_PIN 대시보드 연결 프로그램. 실행 전에 Arduino 시리얼 모니터를 닫으세요.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--serial", default="COM3", help="실제 COM 포트 (기본: COM3). 포트를 자동으로 찾지 않습니다.")
    g.add_argument("--simulate", action="store_true", help="보드 없이 가짜 보드로 실행")
    p.add_argument("--http-port", type=int, default=8000)
    args = p.parse_args()

    store = PinStore(PIN_FILE)
    factory = SimLink if args.simulate else (lambda: SerialLink(args.serial))
    bridge = Bridge(factory, args.simulate, store)
    threading.Thread(target=bridge.reader_loop, daemon=True).start()
    threading.Thread(target=bridge.ticker_loop, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), make_handler(bridge, args.http_port))
    server.daemon_threads = True
    print(f"대시보드: http://127.0.0.1:{args.http_port}/  (종료: Ctrl+C)")
    if store.is_default:
        print(f"처음 PIN은 {DEFAULT_PIN} 입니다. 화면의 'PIN 바꾸기'에서 바꾸세요.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
