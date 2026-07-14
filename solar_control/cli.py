from __future__ import annotations

import argparse
import asyncio
import json
import os

import requests
from dotenv import load_dotenv

from . import ble_manager
from .config import Config


def _api_base_url() -> str:
    port = int(os.environ.get("API_PORT", 8000))
    return f"http://127.0.0.1:{port}"


async def cmd_scan(_args: argparse.Namespace) -> None:
    print("Scanning for Renogy BT-1/BT-2 modules (8s)...")
    devices = await ble_manager.scan()
    if not devices:
        print(
            "No Renogy BT module found. Make sure it's powered on and not already "
            "connected to another app (e.g. the official Renogy BT app)."
        )
        return
    for d in devices:
        print(f"{d.address}  {d.name}")


def _call_api(method: str, path: str, **kwargs) -> None:
    """status/load are thin HTTP clients: the `serve` daemon holds the one BLE
    connection the Rover allows, so anything that touches it goes through the API
    instead of opening a second, competing BLE connection."""
    base_url = _api_base_url()
    try:
        resp = requests.request(method, f"{base_url}{path}", timeout=10, **kwargs)
    except requests.ConnectionError:
        print(
            f"Could not reach the solar-control API server at {base_url}. "
            "Make sure `python -m solar_control serve` is running."
        )
        return

    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        print(f"API error ({resp.status_code}): {detail}")
        return

    print(json.dumps(resp.json(), indent=2, ensure_ascii=False))


def cmd_status(_args: argparse.Namespace) -> None:
    _call_api("GET", "/status")


def cmd_load(args: argparse.Namespace) -> None:
    _call_api("POST", "/load", json={"on": args.state == "on"})


def cmd_serve(_args: argparse.Namespace) -> None:
    import uvicorn

    config = Config.from_env()
    uvicorn.run("solar_control.api:app", host=config.api_host, port=config.api_port)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(prog="solar-control")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="Discover nearby Renogy BT-1/BT-2 modules")
    sub.add_parser("status", help="Read current status via the running API server")

    load_parser = sub.add_parser("load", help="Turn the DC load output on or off via the running API server")
    load_parser.add_argument("state", choices=["on", "off"])

    sub.add_parser("serve", help="Run the REST API server")

    args = parser.parse_args()

    if args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "load":
        cmd_load(args)
    elif args.command == "serve":
        cmd_serve(args)


if __name__ == "__main__":
    main()
