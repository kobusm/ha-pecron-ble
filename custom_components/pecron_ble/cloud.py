"""One-time Pecron cloud calls used only during setup to obtain the BLE auth key.

Only the read-only ``getAuthKey`` endpoint is used. ``regenerateAuthKey`` is
deliberately NOT called: it would rotate the key the device and the official
app rely on.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
from dataclasses import dataclass

import aiohttp
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import REGIONS

_DOMAIN_RETRY_CODES = {5015, 5031, 5420}


class CloudError(Exception):
    """Cloud request failed."""


class CloudAuthError(CloudError):
    """Bad credentials."""


@dataclass(slots=True)
class CloudDevice:
    name: str
    product_key: str
    device_key: str


def login_params(email: str, password: str, domain: str, secret: str, rand: str | None = None) -> dict[str, str]:
    """Build the app's encrypted emailPwdLogin form."""
    rand = rand or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    md5 = hashlib.md5(rand.encode()).hexdigest().upper()  # noqa: S324 - protocol requirement
    key = md5[8:24]
    iv = key[8:16] + key[0:8]
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(password.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    pwd = base64.b64encode(enc.update(padded) + enc.finalize()).decode()
    signature = hashlib.sha256((email + pwd + rand + secret).encode()).hexdigest()
    return {"email": email, "pwd": pwd, "random": rand, "userDomain": domain, "signature": signature}


class PecronCloud:
    def __init__(self, session: aiohttp.ClientSession, region: str) -> None:
        self._session = session
        self._region = REGIONS[region]
        self._token: str | None = None

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        url = self._region["base_url"] + path
        headers = {"Authorization": self._token} if self._token else {}
        try:
            async with self._session.request(
                method, url, headers=headers, timeout=aiohttp.ClientTimeout(total=20), **kwargs
            ) as resp:
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as err:
            raise CloudError(f"{path}: {err}") from err
        if not isinstance(body, dict):
            raise CloudError(f"{path}: unexpected response")
        return body

    async def login(self, email: str, password: str) -> None:
        r = self._region
        attempts = [(r["user_domain"], r["user_domain_secret"])]
        if "fallback_domain" in r:
            attempts.append((r["fallback_domain"], r["fallback_secret"]))
        last: dict = {}
        for domain, secret in attempts:
            last = await self._json(
                "POST", "/v2/enduser/enduserapi/emailPwdLogin",
                data=login_params(email, password, domain, secret),
            )
            if last.get("code") == 200:
                self._token = last["data"]["accessToken"]["token"]
                return
            if last.get("code") not in _DOMAIN_RETRY_CODES:
                break
        raise CloudAuthError(f"login failed: {last.get('code')} {last.get('msg', '')}")

    async def devices(self) -> list[CloudDevice]:
        body = await self._json("GET", "/v2/binding/enduserapi/userDeviceList")
        if body.get("code") != 200:
            raise CloudError(f"device list failed: {body.get('code')} {body.get('msg', '')}")
        data = body.get("data") or {}
        items = data.get("list", []) if isinstance(data, dict) else data
        return [
            CloudDevice(
                name=d.get("deviceName") or d.get("productName") or d["deviceKey"],
                product_key=d["productKey"],
                device_key=d["deviceKey"],
            )
            for d in items
            if d.get("productKey") and d.get("deviceKey")
        ]

    async def auth_key(self, device: CloudDevice) -> str:
        body = await self._json(
            "POST", "/v2/binding/enduserapi/getAuthKey",
            data={"pk": device.product_key, "dk": device.device_key},
        )
        if body.get("code") != 200:
            raise CloudError(f"getAuthKey failed: {body.get('code')} {body.get('msg', '')}")
        return body["data"]["authKey"]


def match_device(devices: list[CloudDevice], ble_name: str | None, address: str) -> CloudDevice | None:
    """Pick the cloud device that corresponds to a BLE advertisement.

    The advertised name is ``QUEC_BLE_XXXX`` and the device key is the Wi-Fi
    MAC, which sits next to the BLE MAC. Try the name suffix, then the MAC.
    """
    if len(devices) == 1:
        return devices[0]
    suffix = (ble_name or "").rsplit("_", 1)[-1].upper()
    mac = address.replace(":", "").upper()
    for d in devices:
        dk = d.device_key.upper()
        if suffix and len(suffix) == 4 and dk.endswith(suffix):
            return d
        try:
            if len(dk) == 12 and len(mac) == 12 and abs(int(dk, 16) - int(mac, 16)) <= 2:
                return d
        except ValueError:
            continue
    return None
