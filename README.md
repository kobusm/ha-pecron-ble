# Pecron (Bluetooth) for Home Assistant

[![Tests](https://github.com/kobusm/ha-pecron-ble/actions/workflows/tests.yml/badge.svg)](https://github.com/kobusm/ha-pecron-ble/actions/workflows/tests.yml)

Local Bluetooth LE integration for Pecron portable power stations, written with the **E300LFP** in mind. It should also work on other models that advertise as `QUEC_BLE_xxxx`. There is no cloud polling: after a one-time key fetch, everything goes over BLE, either directly from the HA host's adapter or through an ESPHome Bluetooth proxy.

## How it works

Pecron stations use a Quectel Wi-Fi/BLE module. The app talks to GATT characteristic `0x9C40`: it writes Write Requests and the device answers with indications. Frames look like `AA AA | len | sum8 | pid | cmd | TTLV payload`, with `AA→AA 55` byte stuffing. Each session:

1. `0x7032` → device returns a random string (`0x7033`)
2. `0x7034` login with `sha256(hex(auth_key) + ";" + random)` → `0x7035`
3. `0x0011` read status → encrypted `0x0012`/`0x0014` reports (AES-128-CBC, key = auth key, IV = random)
4. `0x0013` encrypted property writes → `0x7036` result

The **auth key** is per-device and only available from Pecron's cloud. During setup you either sign in once (read-only `getAuthKey`; your password isn't stored) or paste a key you already have. Setup won't call `regenerateAuthKey`, because that would rotate the key the device and the app depend on.

Protocol credit: [attractify-logan/pecron-monitor](https://github.com/attractify-logan/pecron-monitor) (MIT). The ENUM/number encoding fix follows its issue #112.

## Install

Copy `custom_components/pecron_ble` into your HA `config/custom_components/` folder (or add this repo to HACS as a custom repository), then restart HA. The station should be auto-discovered. If it isn't, go to **Settings → Devices & services → Add integration → Pecron (Bluetooth)**.

Close the Pecron app before setup. A BLE peripheral accepts only one central at a time.

## Entities

Entities are created only for data points your unit actually reports:

- Sensors: battery %, input/output power, AC and DC in/out power, AC voltage/frequency/PF, battery/charger/inverter temperature, time to empty/full, status, charge status, fault code
- Switches: AC output, DC output, UPS mode

## E300LFP caveats

- **AC output can turn off when a BLE session starts.** An E300LFP owner saw this in [pecron-monitor #3](https://github.com/attractify-logan/pecron-monitor/issues/3); it looks like a firmware side effect of the handshake. This integration keeps one connection open and only reconnects after a drop, which keeps handshakes rare. If AC was on before a drop and is off right after reconnecting, the integration logs a warning and increments `ac_flips_on_reconnect` in diagnostics. It does **not** turn AC back on by itself.
- **Time remaining.** The firmware reports the same number for time-to-empty and time-to-full, so the integration shows whichever one matches the current power flow. The unit is assumed to be minutes; this hasn't been checked on a real unit yet.
- **Data-point map.** The map is taken from larger models. If something your E300LFP reports isn't shown, download the device diagnostics: every raw data point is listed as `id:name`.

## Development

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```

`tests/fake_device.py` is a fake device that speaks the real protocol: handshake, AES, and indications split into 20-byte chunks. The client and integration tests run against it.

## License

MIT, see [LICENSE](LICENSE).
