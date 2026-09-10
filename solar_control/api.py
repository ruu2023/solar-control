"""REST API: read Rover + EcoFlow status, control the DC load, and run
rule-based automatic load control backed by a small SQLite database.

Status is polled from the BT-1 ONCE by a background refresher (every
STATUS_REFRESH_SECONDS) into an in-memory snapshot. ``/status`` serves that
snapshot with no BLE round-trip, and ``/stream`` (Server-Sent Events) pushes it
to clients so the web UI / app never have to poll.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .automation import AutomationEngine, Database, metrics_from_status
from .config import Config
from .ecoflow_client import EcoFlowClient
from .rover_client import RoverClient

logger = logging.getLogger("solar_control.api")

_client: Optional[RoverClient] = None
_ecoflow_client: Optional[EcoFlowClient] = None
_ecoflow_device_sn: str = ""
_db: Optional[Database] = None
_engine: Optional[AutomationEngine] = None

DB_PATH = Path(__file__).resolve().parent.parent / "solar_control.db"
STATUS_REFRESH_SECONDS = float(os.environ.get("STATUS_REFRESH_SECONDS", "8"))
_META_PUSH_SECONDS = 15.0

# in-memory status snapshot + SSE fan-out
_snapshot: Optional[dict] = None
_subscribers: "set[asyncio.Queue]" = set()
_refresher_task: Optional[asyncio.Task] = None


# --------------------------------------------------------------------------- #
#  status collection + snapshot cache
# --------------------------------------------------------------------------- #
async def _collect_status_live() -> tuple[dict, dict]:
    """Actually talk to the hardware. (solar, home_battery); raises on Rover
    failure, home_battery degrades to ``{"error": ...}`` when EcoFlow is off."""
    client = _require_client()
    solar = await client.get_status()
    if _ecoflow_client:
        try:
            home = await asyncio.to_thread(_ecoflow_client.get_status, _ecoflow_device_sn)
        except requests.RequestException as e:
            home = {"error": str(e)}
    else:
        home = {"error": "EcoFlow not configured"}
    return solar, home


async def _snapshot_pair() -> tuple[dict, dict]:
    """(solar, home_battery) from the cached snapshot, or a live read if the
    refresher hasn't produced one yet. Used by /status, /metrics and the engine."""
    if _snapshot:
        return _snapshot["solar"], _snapshot["home_battery"]
    return await _collect_status_live()


async def _publish(event: str, data: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait((event, data))
        except asyncio.QueueFull:
            pass


async def _refresh_now() -> None:
    """Read the hardware immediately and publish; used after a manual switch."""
    global _snapshot
    try:
        solar, home = await _collect_status_live()
    except Exception as e:  # noqa: BLE001
        logger.warning("status refresh (now) failed: %s", e)
        return
    _snapshot = {"solar": solar, "home_battery": home, "ts": time.time()}
    await _publish("status", _snapshot)


async def _status_refresher() -> None:
    global _snapshot
    logger.info("status refresher started (every %ss)", STATUS_REFRESH_SECONDS)
    while True:
        try:
            solar, home = await _collect_status_live()
            _snapshot = {"solar": solar, "home_battery": home, "ts": time.time()}
            await _publish("status", _snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("status refresh failed: %s", e)
        await asyncio.sleep(STATUS_REFRESH_SECONDS)


async def _meta() -> dict:
    if _db is None:
        return {}
    return {
        "automation": await asyncio.to_thread(_db.get_automation),
        "rules": await asyncio.to_thread(_db.list_rules),
        "events": await asyncio.to_thread(_db.list_events, 8),
    }


async def _push_meta() -> None:
    """Send fresh automation/rules/events to every SSE client (after a change)."""
    await _publish("meta", await _meta())


async def _set_load(on: bool):
    return await _require_client().set_load(on)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _client, _ecoflow_client, _ecoflow_device_sn, _db, _engine, _refresher_task
    config = Config.from_env()
    _client = RoverClient(config.mac_address, config.device_id, config.temperature_unit)
    await _client.connect()
    if config.has_ecoflow:
        _ecoflow_client = EcoFlowClient(
            config.ecoflow_access_key, config.ecoflow_secret_key, config.ecoflow_base_url
        )
        _ecoflow_device_sn = config.ecoflow_device_sn

    _db = Database(DB_PATH)
    _refresher_task = asyncio.create_task(_status_refresher(), name="status-refresher")
    # the engine reads the cached snapshot, so only the refresher touches BLE for status
    _engine = AutomationEngine(_db, _snapshot_pair, _set_load)
    _engine.start()
    try:
        yield
    finally:
        if _engine:
            await _engine.stop()
        if _refresher_task:
            _refresher_task.cancel()
            with suppress(asyncio.CancelledError):
                await _refresher_task
        if _db:
            _db.close()
        await _client.disconnect()


app = FastAPI(title="solar-control", lifespan=lifespan)


# --------------------------------------------------------------------------- #
#  status / control
# --------------------------------------------------------------------------- #
class LoadRequest(BaseModel):
    on: bool


def _require_client() -> RoverClient:
    if not _client or not _client.is_connected:
        raise HTTPException(status_code=503, detail="Not connected to the Rover")
    return _client


def _require_db() -> Database:
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not ready")
    return _db


@app.get("/health")
async def health():
    return {"connected": bool(_client and _client.is_connected),
            "snapshot_age": None if not _snapshot else round(time.time() - _snapshot["ts"], 1)}


@app.get("/status")
async def status():
    try:
        solar, home = await _snapshot_pair()
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out reading from the Rover")
    return {"solar": solar, "home_battery": home}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/stream")
async def stream():
    """Server-Sent Events: `status` on every refresh, `meta` (automation/rules/
    events) on connect + when something changes + every ~15s as a fallback."""
    q: asyncio.Queue = asyncio.Queue(maxsize=20)
    _subscribers.add(q)

    async def gen():
        try:
            if _snapshot:
                yield _sse("status", _snapshot)
            yield _sse("meta", await _meta())
            last_meta = time.time()
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield _sse(event, data)
                    if event == "meta":
                        last_meta = time.time()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                if time.time() - last_meta > _META_PUSH_SECONDS:
                    yield _sse("meta", await _meta())
                    last_meta = time.time()
        finally:
            _subscribers.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/load")
async def set_load(req: LoadRequest):
    try:
        result = await _set_load(req.on)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out writing to the Rover")
    # A manual switch pauses automation for a while so rules don't fight the user.
    if _db is not None:
        action = "on" if req.on else "off"
        await asyncio.to_thread(_db.start_manual_override)
        await asyncio.to_thread(_db.add_event, action, "manual", "manual /load", {}, True)
        await _push_meta()
    await _refresh_now()
    return result


# --------------------------------------------------------------------------- #
#  automation: rules
# --------------------------------------------------------------------------- #
class RuleIn(BaseModel):
    name: str = ""
    enabled: bool = True
    priority: int = 100
    metric: str
    op: str
    threshold: float
    metric2: Optional[str] = None
    op2: Optional[str] = None
    threshold2: Optional[float] = None
    action: str


class RulePatch(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    metric: Optional[str] = None
    op: Optional[str] = None
    threshold: Optional[float] = None
    metric2: Optional[str] = None
    op2: Optional[str] = None
    threshold2: Optional[float] = None
    action: Optional[str] = None


class AutomationPatch(BaseModel):
    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = None
    min_switch_seconds: Optional[int] = None
    manual_override_minutes: Optional[int] = None


@app.get("/rules")
async def list_rules():
    return await asyncio.to_thread(_require_db().list_rules)


@app.post("/rules", status_code=201)
async def create_rule(rule: RuleIn):
    try:
        created = await asyncio.to_thread(_require_db().create_rule, rule.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _push_meta()
    return created


@app.put("/rules/{rule_id}")
async def update_rule(rule_id: int, patch: RulePatch):
    data = {k: v for k, v in patch.model_dump().items() if v is not None}
    try:
        updated = await asyncio.to_thread(_require_db().update_rule, rule_id, data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="rule not found")
    await _push_meta()
    return updated


@app.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int):
    ok = await asyncio.to_thread(_require_db().delete_rule, rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="rule not found")
    await _push_meta()


# --------------------------------------------------------------------------- #
#  automation: settings + history
# --------------------------------------------------------------------------- #
@app.get("/automation")
async def get_automation():
    return await asyncio.to_thread(_require_db().get_automation)


@app.post("/automation")
async def update_automation(patch: AutomationPatch):
    data = {k: v for k, v in patch.model_dump().items() if v is not None}
    try:
        result = await asyncio.to_thread(_require_db().update_automation, data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _push_meta()
    return result


@app.post("/automation/resume")
async def resume_automation():
    """Clear a manual-override pause so rules take effect again immediately."""
    db = _require_db()
    await asyncio.to_thread(db.clear_override)
    result = await asyncio.to_thread(db.get_automation)
    await _push_meta()
    return result


@app.get("/events")
async def list_events(limit: int = 50):
    return await asyncio.to_thread(_require_db().list_events, limit)


@app.get("/metrics")
async def current_metrics():
    """Live values of everything a rule can test (handy when writing rules)."""
    try:
        solar, home = await _snapshot_pair()
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out reading from the Rover")
    m = metrics_from_status(solar, home)
    m.pop("_load_on", None)
    return m


# --- static web UI ---------------------------------------------------------- #
_WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"
if _WEBUI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEBUI_DIR), html=True), name="webui")
