import sys, time, datetime, pathlib

AUTH_ALLOWED = 3
LOG = pathlib.Path.home() / "Library/Logs/solar-control/preflight.log"

def _log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)

def wait_for_ble_auth(timeout=180.0):
    from Foundation import NSRunLoop, NSDate
    from CoreBluetooth import CBCentralManager
    mgr = CBCentralManager.alloc().initWithDelegate_queue_(None, None)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.5))
        try:
            auth = int(CBCentralManager.authorization())
        except Exception as e:
            try: auth = int(mgr.authorization())
            except Exception: auth = -1
        if auth != last:
            _log(f"CBManager authorization = {auth}")
            last = auth
        if auth == AUTH_ALLOWED:
            return auth
        if auth in (1, 2):
            return auth
    return last if last is not None else -1

def main():
    _log("serve_app start")
    auth = wait_for_ble_auth()
    _log(f"preflight done, auth={auth}")
    if auth != AUTH_ALLOWED:
        _log("Bluetooth NOT authorized; exiting 1")
        sys.exit(1)
    _log("Bluetooth OK -> starting uvicorn serve")
    from solar_control.cli import main as cli_main
    sys.argv = ["solar-control", "serve"]
    cli_main()

if __name__ == "__main__":
    main()
