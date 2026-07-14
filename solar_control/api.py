"""REST API for reading Rover status and controlling the DC load output."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import Config
from .ecoflow_client import EcoFlowClient
from .rover_client import RoverClient

_client: Optional[RoverClient] = None
_ecoflow_client: Optional[EcoFlowClient] = None
_ecoflow_device_sn: str = ""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _client, _ecoflow_client, _ecoflow_device_sn
    config = Config.from_env()
    _client = RoverClient(config.mac_address, config.device_id, config.temperature_unit)
    await _client.connect()
    if config.has_ecoflow:
        _ecoflow_client = EcoFlowClient(config.ecoflow_access_key, config.ecoflow_secret_key, config.ecoflow_base_url)
        _ecoflow_device_sn = config.ecoflow_device_sn
    try:
        yield
    finally:
        await _client.disconnect()


app = FastAPI(title="solar-control", lifespan=lifespan)


class LoadRequest(BaseModel):
    on: bool


def _require_client() -> RoverClient:
    if not _client or not _client.is_connected:
        raise HTTPException(status_code=503, detail="Not connected to the Rover")
    return _client


@app.get("/health")
async def health():
    return {"connected": bool(_client and _client.is_connected)}


@app.get("/status")
async def status():
    client = _require_client()
    try:
        solar = await client.get_status()
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out reading from the Rover")

    if _ecoflow_client:
        try:
            home_battery = await asyncio.to_thread(_ecoflow_client.get_status, _ecoflow_device_sn)
        except requests.RequestException as e:
            home_battery = {"error": str(e)}
    else:
        home_battery = {"error": "EcoFlow not configured"}

    return {"solar": solar, "home_battery": home_battery}


@app.post("/load")
async def set_load(req: LoadRequest):
    client = _require_client()
    try:
        return await client.set_load(req.on)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timed out writing to the Rover")
