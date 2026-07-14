"""EcoFlow 公式REST API (DELTA 2 等) 用の署名付きクライアント。

署名仕様: リクエストパラメータをキー昇順でフラット化し
`key=value&...&accessKey=...&nonce=...&timestamp=...` を secretKey で HMAC-SHA256。
"""
from __future__ import annotations

import hashlib
import hmac
import random
import time
from typing import Any

import requests


def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in params.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, full_key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                item_key = f"{full_key}[{i}]"
                if isinstance(item, dict):
                    flat.update(_flatten(item, item_key))
                else:
                    flat[item_key] = _stringify(item)
        else:
            flat[full_key] = _stringify(value)
    return flat


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def parse_status(quota: dict) -> dict:
    """Pull the handful of fields we care about out of a `device/quota/all` response."""
    data = quota.get("data", quota)
    return {
        "battery_percent": data.get("bms_bmsStatus.f32ShowSoc"),
        "input_watts": data.get("bms_bmsStatus.inputWatts"),
        "output_watts": data.get("bms_bmsStatus.outputWatts"),
        "temperature": data.get("bms_bmsStatus.temp"),
    }


class EcoFlowClient:
    def __init__(self, access_key: str, secret_key: str, base_url: str = "https://api-e.ecoflow.com"):
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")

    def _sign(self, params: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
        nonce = str(random.randint(100000, 999999))
        timestamp = str(int(time.time() * 1000))

        flat = _flatten(params) if params else {}
        sign_pairs = {**flat}
        query_string = "&".join(f"{k}={sign_pairs[k]}" for k in sorted(sign_pairs))
        auth_suffix = f"accessKey={self.access_key}&nonce={nonce}&timestamp={timestamp}"
        sign_str = f"{query_string}&{auth_suffix}" if query_string else auth_suffix

        sign = hmac.new(self.secret_key.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

        headers = {
            "accessKey": self.access_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
        }
        return headers, flat

    def get_device_list(self) -> Any:
        headers, _ = self._sign({})
        resp = requests.get(f"{self.base_url}/iot-open/sign/device/list", headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_all_quota(self, serial_number: str) -> Any:
        params = {"sn": serial_number}
        headers, flat = self._sign(params)
        resp = requests.get(
            f"{self.base_url}/iot-open/sign/device/quota/all",
            headers=headers,
            params=flat,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_status(self, serial_number: str) -> dict:
        return parse_status(self.get_all_quota(serial_number))
