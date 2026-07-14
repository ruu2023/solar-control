import hashlib
import hmac

from solar_control.ecoflow_client import EcoFlowClient, _flatten, parse_status


def test_flatten_nests_dicts_with_dot_notation():
    assert _flatten({"a": {"b": 1, "c": {"d": 2}}}) == {"a.b": "1", "a.c.d": "2"}


def test_flatten_indexes_lists():
    assert _flatten({"a": [{"b": 1}, {"b": 2}]}) == {"a[0].b": "1", "a[1].b": "2"}


def test_sign_matches_hmac_sha256_of_sorted_params():
    client = EcoFlowClient("AK", "SECRET")
    headers, flat = client._sign({"sn": "ABC123"})

    assert flat == {"sn": "ABC123"}
    sign_str = f"sn=ABC123&accessKey=AK&nonce={headers['nonce']}&timestamp={headers['timestamp']}"
    expected = hmac.new(b"SECRET", sign_str.encode(), hashlib.sha256).hexdigest()
    assert headers["sign"] == expected
    assert headers["accessKey"] == "AK"


def test_sign_with_no_params_still_includes_auth_suffix():
    client = EcoFlowClient("AK", "SECRET")
    headers, flat = client._sign({})

    assert flat == {}
    sign_str = f"accessKey=AK&nonce={headers['nonce']}&timestamp={headers['timestamp']}"
    expected = hmac.new(b"SECRET", sign_str.encode(), hashlib.sha256).hexdigest()
    assert headers["sign"] == expected


def test_parse_status_extracts_known_fields():
    quota = {
        "data": {
            "bms_bmsStatus.f32ShowSoc": 87,
            "bms_bmsStatus.inputWatts": 120,
            "bms_bmsStatus.outputWatts": 45,
            "bms_bmsStatus.temp": 29,
        }
    }
    assert parse_status(quota) == {
        "battery_percent": 87,
        "input_watts": 120,
        "output_watts": 45,
        "temperature": 29,
    }
