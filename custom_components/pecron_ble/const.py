"""Constants and the Pecron TSL data-point map."""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "pecron_ble"

CONF_AUTH_KEY: Final = "auth_key"
CONF_DEVICE_KEY: Final = "device_key"
CONF_PRODUCT_KEY: Final = "product_key"
CONF_REGION: Final = "region"

DEFAULT_SCAN_INTERVAL: Final = timedelta(seconds=30)
BLE_NAME_PREFIX: Final = "QUEC_BLE_"
# Quectel modules put manufacturer id 0x5551 ("QU") in their advertisement.
# Matching on it too covers units that advertise a different name, and
# passive scanners that never receive the scan response carrying the name.
QUECTEL_MANUFACTURER_ID: Final = 0x5551

# ---------------------------------------------------------------------------
# TSL data points (numeric ids on the wire). Source: pecron-monitor TSL_TOP.
# A path is a tuple of ids: (top,) or (struct, member).
# ---------------------------------------------------------------------------

DP_BATTERY_PCT = 1
DP_REMAIN_TIME = 2
DP_REMAIN_CHARGE_TIME = 3
DP_TOTAL_IN = 4
DP_TOTAL_OUT = 5
DP_UPS = 27
DP_DC_IN = 28  # struct: 1 dc_input_power
DP_AC_IN = 29  # struct: 1 ac_power
DP_DC_OUT = 30  # struct: 1 dc_output_power
DP_AC_OUT = 31  # struct: 1 hz, 2 voltage, 3 pf, 4 power
DP_HOST_PACK = 35  # struct: 1 pct, 2 V, 3 A, 4 temp, 5 status
DP_EXP_PACKS = 36  # repeated struct
DP_DEVICE_STATUS = 37
DP_DC_SWITCH = 38
DP_AC_SWITCH = 40
DP_TOUCH_LOCK = 42
DP_ECO_MODE = 44
DP_BATTERY_TEMP = 47
DP_CHARGER_TEMP = 48
DP_INVERTER_TEMP = 49
DP_FAULT = 90

# Every id we know a name for, used to label raw values in diagnostics.
KNOWN_IDS: Final[dict[int, str]] = {
    1: "battery_percentage", 2: "remain_time", 3: "remain_charging_time",
    4: "total_input_power", 5: "total_output_power", 27: "ups_status_hm",
    28: "dc_data_input_hm", 29: "ac_data_input_hm", 30: "dc_data_output_hm",
    31: "ac_data_output_hm", 32: "ac_output_voltage_io", 33: "ac_output_frequency_io",
    34: "noastime_io", 35: "host_packet_data_jdb", 36: "charging_pack_data_jdb",
    37: "device_status_hm", 38: "dc_switch_hm", 39: "add_bat_status_hm",
    40: "ac_switch_hm", 41: "device_mode_info", 42: "device_touch_locking_as",
    43: "auto_light_flag_as", 44: "eco_quite_mode_as", 45: "machine_screen_light_as",
    46: "ups_start_charge_value_as", 47: "battery_temp", 48: "charging_plate_temp",
    49: "inverter_temp", 50: "ac_charging_power_ios", 51: "device_standy_times_as",
    52: "device_manual", 55: "dc_charging_power_enable", 56: "bypass_enable",
    86: "battery_coding_us", 87: "beep_voice_us", 89: "battery_indicator_us",
    90: "FAULT_ALARM_ENUM", 91: "battery_heating_mode", 92: "charging_limit_voltage",
    93: "discharge_limiting_voltage", 94: "charging_current_limit",
    95: "discharge_limiting_current", 100: "high_frequency_reporting",
}

DEVICE_STATUS: Final[dict[int, str]] = {
    0: "shut_down", 1: "charging", 2: "dc_discharge",
    3: "ac_discharge", 4: "standby", 5: "conservation",
}
CHARGE_STATUS: Final[dict[int, str]] = {
    0: "no_charge", 1: "cascade_charging", 2: "balance_no_charge",
    3: "balanced_charging", 4: "no_connection",
}

# Cloud regions, used once during setup to fetch the auth key.
# userDomain values are the Pecron app's public client identifiers.
REGIONS: Final[dict[str, dict[str, str]]] = {
    "na": {
        "name": "North America",
        "base_url": "https://iot-api.landecia.com",
        "user_domain": "C.DM.10351.1",
        "user_domain_secret": "FA5ZHXSka8y9GHvU91Hz1vWvaDSHE2mGW5B7bpn3fXTW",
        "fallback_domain": "U.DM.10351.1",
        "fallback_secret": "HARsQXfeex8vxyaPRAM8fyjqqVuH2uxAGQ3inJ8XxTiB",
    },
    "eu": {
        "name": "Europe",
        "base_url": "https://iot-api.acceleronix.io",
        "user_domain": "C.DM.10351.1",
        "user_domain_secret": "FA5ZHXSka8y9GHvU91Hz1vWvaDSHE2mGW5B7bpn3fXTW",
    },
    "cn": {
        "name": "China",
        "base_url": "https://iot-api.quectelcn.com",
        "user_domain": "C.DM.5903.1",
        "user_domain_secret": "EufftRJSuWuVY7c6txzGifV9bJcfXHAFa7hXY5doXSn7",
    },
}
