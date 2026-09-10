"""Rule-based automatic load control, backed by a small SQLite database.

Design
------
* One SQLite file (``solar_control.db`` next to the package). Plain stdlib
  ``sqlite3`` guarded by a lock; every call is cheap and infrequent, so all DB
  work is pushed onto a worker thread with ``asyncio.to_thread``.
* ``rules``      -- user-defined conditions. Each rule is ``metric op threshold``
                    (optionally AND a second clause) with an action of on/off.
* ``automation`` -- single settings row: master switch, eval interval,
                    anti-flap window, manual-override handling.
* ``events``     -- audit log of every switch (who/why + a metric snapshot).

Evaluation (every ``interval_seconds``):
    1. skip if automation disabled, still inside a manual override, or the
       last switch was < ``min_switch_seconds`` ago;
    2. read a fresh status snapshot -> flat metrics dict;
    3. first ENABLED rule (priority asc, then id) whose condition holds wins;
    4. if its action differs from the current load state, switch + log.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("solar_control.automation")

METRICS = (
    "battery_percent",
    "battery_voltage",
    "pv_power",
    "load_power",
    "home_battery_percent",
    "hour",  # local hour of day, 0-23
)
OPERATORS = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}
ACTIONS = ("on", "off")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 100,
    metric      TEXT NOT NULL,
    op          TEXT NOT NULL,
    threshold   REAL NOT NULL,
    metric2     TEXT,
    op2         TEXT,
    threshold2  REAL,
    action      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    enabled                  INTEGER NOT NULL DEFAULT 0,
    interval_seconds         INTEGER NOT NULL DEFAULT 30,
    min_switch_seconds       INTEGER NOT NULL DEFAULT 60,
    manual_override_minutes  INTEGER NOT NULL DEFAULT 30,
    override_until           TEXT,
    last_eval_at             TEXT,
    last_action_at           TEXT
);
INSERT OR IGNORE INTO automation (id) VALUES (1);
CREATE TABLE IF NOT EXISTS events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,
    action               TEXT NOT NULL,
    source               TEXT NOT NULL,
    detail               TEXT NOT NULL DEFAULT '',
    battery_percent      REAL,
    battery_voltage      REAL,
    pv_power             REAL,
    load_power           REAL,
    home_battery_percent REAL,
    ok                   INTEGER NOT NULL DEFAULT 1
);
"""

_EVENTS_KEEP = 500


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Database:
    """Tiny synchronous SQLite wrapper. Call the async helpers from the app."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- rules ---------------------------------------------------------------
    def list_rules(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rules ORDER BY priority ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def create_rule(self, data: dict) -> dict:
        _validate_rule(data, partial=False)
        now = _now_iso()
        fields = dict(
            name=data.get("name", ""),
            enabled=1 if data.get("enabled", True) else 0,
            priority=int(data.get("priority", 100)),
            metric=data["metric"],
            op=data["op"],
            threshold=float(data["threshold"]),
            metric2=data.get("metric2"),
            op2=data.get("op2"),
            threshold2=None if data.get("threshold2") is None else float(data["threshold2"]),
            action=data["action"],
            created_at=now,
            updated_at=now,
        )
        cols = ", ".join(fields)
        ph = ", ".join(["?"] * len(fields))
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO rules ({cols}) VALUES ({ph})", tuple(fields.values())
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM rules WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def update_rule(self, rule_id: int, data: dict) -> Optional[dict]:
        _validate_rule(data, partial=True)
        allowed = ("name", "enabled", "priority", "metric", "op", "threshold",
                   "metric2", "op2", "threshold2", "action")
        sets, vals = [], []
        for key in allowed:
            if key in data:
                v = data[key]
                if key == "enabled":
                    v = 1 if v else 0
                elif key in ("priority",):
                    v = int(v)
                elif key in ("threshold", "threshold2") and v is not None:
                    v = float(v)
                sets.append(f"{key} = ?")
                vals.append(v)
        if not sets:
            return self.get_rule(rule_id)
        sets.append("updated_at = ?")
        vals.append(_now_iso())
        vals.append(rule_id)
        with self._lock:
            self._conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", tuple(vals))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
        return dict(row) if row else None

    def get_rule(self, rule_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
        return dict(row) if row else None

    def delete_rule(self, rule_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # -- automation settings ----------------------------------------------------
    def get_automation(self) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM automation WHERE id = 1").fetchone()
        d = dict(row)
        until = _parse_iso(d.get("override_until"))
        d["override_active"] = bool(until and until > datetime.now())
        return d

    def update_automation(self, data: dict) -> dict:
        allowed = ("enabled", "interval_seconds", "min_switch_seconds", "manual_override_minutes")
        sets, vals = [], []
        for key in allowed:
            if key in data:
                v = data[key]
                v = (1 if v else 0) if key == "enabled" else int(v)
                if key != "enabled" and v < 1:
                    raise ValueError(f"{key} must be >= 1")
                sets.append(f"{key} = ?")
                vals.append(v)
        if sets:
            with self._lock:
                self._conn.execute(f"UPDATE automation SET {', '.join(sets)} WHERE id = 1", tuple(vals))
                self._conn.commit()
        return self.get_automation()

    def _set_automation_ts(self, **cols: Optional[str]) -> None:
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._lock:
            self._conn.execute(f"UPDATE automation SET {sets} WHERE id = 1", tuple(cols.values()))
            self._conn.commit()

    def touch_eval(self) -> None:
        self._set_automation_ts(last_eval_at=_now_iso())

    def mark_action(self) -> None:
        self._set_automation_ts(last_action_at=_now_iso())

    def clear_override(self) -> None:
        self._set_automation_ts(override_until=None)

    def start_manual_override(self) -> None:
        auto = self.get_automation()
        until = datetime.now() + timedelta(minutes=int(auto["manual_override_minutes"]))
        self._set_automation_ts(override_until=until.isoformat(timespec="seconds"))

    # -- events ---------------------------------------------------------------
    def add_event(self, action: str, source: str, detail: str, metrics: dict, ok: bool = True) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO events
                   (ts, action, source, detail, battery_percent, battery_voltage,
                    pv_power, load_power, home_battery_percent, ok)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now_iso(), action, source, detail,
                    metrics.get("battery_percent"), metrics.get("battery_voltage"),
                    metrics.get("pv_power"), metrics.get("load_power"),
                    metrics.get("home_battery_percent"), 1 if ok else 0,
                ),
            )
            self._conn.execute(
                "DELETE FROM events WHERE id <= (SELECT MAX(id) - ? FROM events)",
                (_EVENTS_KEEP,),
            )
            self._conn.commit()

    def list_events(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), _EVENTS_KEEP))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _validate_rule(data: dict, *, partial: bool) -> None:
    required = ("metric", "op", "threshold", "action")
    if not partial:
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"missing fields: {', '.join(missing)}")
    if data.get("metric") is not None and data["metric"] not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    if data.get("op") is not None and data["op"] not in OPERATORS:
        raise ValueError(f"op must be one of {tuple(OPERATORS)}")
    if data.get("action") is not None and data["action"] not in ACTIONS:
        raise ValueError("action must be 'on' or 'off'")
    if data.get("metric2"):
        if data["metric2"] not in METRICS:
            raise ValueError(f"metric2 must be one of {METRICS}")
        if data.get("op2") not in OPERATORS:
            raise ValueError(f"op2 must be one of {tuple(OPERATORS)}")
        if data.get("threshold2") is None:
            raise ValueError("threshold2 is required when metric2 is set")


def metrics_from_status(solar: Optional[dict], home: Optional[dict]) -> dict:
    """Flatten a /status payload into the values rules can test."""
    solar = solar or {}
    home = home or {}
    return {
        "battery_percent": solar.get("battery_percent"),
        "battery_voltage": solar.get("battery_voltage"),
        "pv_power": solar.get("pv_power"),
        "load_power": solar.get("load_power"),
        "home_battery_percent": home.get("battery_percent"),
        "hour": datetime.now().hour,
        # not a rule metric, but needed to decide whether a switch is required:
        "_load_on": str(solar.get("load_status", "")).lower() == "on",
    }


def _clause_true(metric: Optional[str], op: Optional[str], threshold: Optional[float], metrics: dict) -> bool:
    if not metric:
        return True
    value = metrics.get(metric)
    if value is None or threshold is None or op not in OPERATORS:
        return False
    try:
        return OPERATORS[op](float(value), float(threshold))
    except (TypeError, ValueError):
        return False


def _fmt_clause(metric: str, op: str, threshold: float, metrics: dict) -> str:
    sym = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "==", "ne": "!="}[op]
    return f"{metric}={metrics.get(metric)} {sym} {threshold}"


def evaluate(rules: list[dict], metrics: dict) -> Optional[tuple[str, dict, str]]:
    """Return (action, rule, human_detail) for the first matching enabled rule."""
    for rule in rules:
        if not rule.get("enabled"):
            continue
        if not _clause_true(rule["metric"], rule["op"], rule["threshold"], metrics):
            continue
        if not _clause_true(rule.get("metric2"), rule.get("op2"), rule.get("threshold2"), metrics):
            continue
        detail = _fmt_clause(rule["metric"], rule["op"], rule["threshold"], metrics)
        if rule.get("metric2"):
            detail += " AND " + _fmt_clause(rule["metric2"], rule["op2"], rule["threshold2"], metrics)
        return rule["action"], rule, detail
    return None


class AutomationEngine:
    """Background loop that applies the rules to the live load output."""

    def __init__(
        self,
        db: Database,
        collect_status: Callable[[], Awaitable[tuple[dict, dict]]],
        set_load: Callable[[bool], Awaitable[Any]],
    ):
        self.db = db
        self._collect_status = collect_status
        self._set_load = set_load
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="automation-engine")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        logger.info("automation engine started")
        while True:
            auto = await asyncio.to_thread(self.db.get_automation)
            interval = max(5, int(auto.get("interval_seconds") or 30))
            try:
                await self._tick(auto)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the loop die
                logger.exception("automation tick failed")
            await asyncio.sleep(interval)

    async def _tick(self, auto: dict) -> None:
        await asyncio.to_thread(self.db.touch_eval)
        if not auto.get("enabled"):
            return
        if auto.get("override_active"):
            return
        last_action = _parse_iso(auto.get("last_action_at"))
        min_gap = int(auto.get("min_switch_seconds") or 60)
        if last_action and (datetime.now() - last_action).total_seconds() < min_gap:
            return

        try:
            solar, home = await self._collect_status()
        except Exception as e:
            logger.warning("automation: status read failed: %s", e)
            return

        metrics = metrics_from_status(solar, home)
        rules = await asyncio.to_thread(self.db.list_rules)
        result = evaluate(rules, metrics)
        if result is None:
            return
        action, rule, detail = result
        want_on = action == "on"
        if want_on == metrics["_load_on"]:
            return  # already in the desired state

        source = f"rule:{rule['id']} {rule.get('name') or ''}".strip()
        logger.info("automation: %s -> load %s (%s)", source, action, detail)
        ok = True
        try:
            await self._set_load(want_on)
        except Exception as e:
            ok = False
            detail += f" [switch failed: {e}]"
            logger.error("automation: set_load(%s) failed: %s", want_on, e)
        await asyncio.to_thread(self.db.add_event, action, source, detail, metrics, ok)
        if ok:
            await asyncio.to_thread(self.db.mark_action)
