import os
from dataclasses import dataclass


@dataclass
class Config:
    mac_address: str
    device_id: int = 255
    temperature_unit: str = "C"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    ecoflow_access_key: str = ""
    ecoflow_secret_key: str = ""
    ecoflow_device_sn: str = ""
    ecoflow_base_url: str = "https://api-e.ecoflow.com"

    @property
    def has_ecoflow(self) -> bool:
        return bool(self.ecoflow_access_key and self.ecoflow_secret_key and self.ecoflow_device_sn)

    @classmethod
    def from_env(cls) -> "Config":
        mac_address = os.environ.get("RENOGY_MAC_ADDRESS", "")
        if not mac_address:
            raise RuntimeError(
                "RENOGY_MAC_ADDRESS is not set. Run `python -m solar_control scan` to "
                "find your BT-1/BT-2 module's MAC address, then set it in .env "
                "(see .env.example)."
            )
        return cls(
            mac_address=mac_address,
            device_id=int(os.environ.get("RENOGY_DEVICE_ID", 255)),
            temperature_unit=os.environ.get("RENOGY_TEMP_UNIT", "C"),
            api_host=os.environ.get("API_HOST", "0.0.0.0"),
            api_port=int(os.environ.get("API_PORT", 8000)),
            ecoflow_access_key=os.environ.get("ECOFLOW_ACCESS_KEY", ""),
            ecoflow_secret_key=os.environ.get("ECOFLOW_SECRET_KEY", ""),
            ecoflow_device_sn=os.environ.get("ECOFLOW_DEVICE_SN", ""),
            ecoflow_base_url=os.environ.get("ECOFLOW_BASE_URL", "https://api-e.ecoflow.com"),
        )
