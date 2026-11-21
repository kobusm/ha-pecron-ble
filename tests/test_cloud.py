"""Cloud helper: login signing and device matching."""
import base64
import hashlib

from custom_components.pecron_ble.cloud import CloudDevice, login_params, match_device


def test_login_params_signature():
    params = login_params("a@b.c", "pw", "C.DM.1", "SECRET", rand="0123456789abcdef")
    assert params["userDomain"] == "C.DM.1"
    assert len(base64.b64decode(params["pwd"])) == 16
    expected = hashlib.sha256(("a@b.c" + params["pwd"] + "0123456789abcdef" + "SECRET").encode()).hexdigest()
    assert params["signature"] == expected


def test_match_single_device():
    d = CloudDevice("x", "pk", "112233445566")
    assert match_device([d], None, "00:00:00:00:00:00") is d


def test_match_by_name_suffix_and_mac():
    a = CloudDevice("a", "pk", "BC2A33E2B4BB")
    b = CloudDevice("b", "pk", "682499E40D61")
    assert match_device([a, b], "QUEC_BLE_0D61", "11:22:33:44:55:66") is b
    assert match_device([a, b], "QUEC_BLE_ZZZZ", "BC:2A:33:E2:B4:BC") is a
    assert match_device([a, b], None, "11:22:33:44:55:66") is None


async def test_auth_key_ok_without_key_raises():
    import pytest
    from unittest.mock import AsyncMock

    from custom_components.pecron_ble.cloud import CloudError, PecronCloud

    cloud = PecronCloud(None, "na")
    cloud._json = AsyncMock(return_value={"code": 200, "data": {"authKey": None, "dk": "x"}})
    with pytest.raises(CloudError, match="no authKey"):
        await cloud.auth_key(CloudDevice("x", "pk", "dk"))
    cloud._json = AsyncMock(return_value={"code": 5032, "msg": "No permission"})
    with pytest.raises(CloudError, match="code 5032: No permission"):
        await cloud.auth_key(CloudDevice("x", "pk", "dk"))
