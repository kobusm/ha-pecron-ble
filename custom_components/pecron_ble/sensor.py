"""Sensors. Only data points the device actually reports get an entity."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const as c
from .coordinator import PecronConfigEntry, PecronCoordinator, value_at
from .entity import PecronEntity


@dataclass(frozen=True, kw_only=True)
class PecronSensorDescription(SensorEntityDescription):
    # Paths tried in order; the first one present wins (models differ: the
    # E300LFP reports battery % top-level, larger units nest it in id 35).
    paths: tuple[tuple[int, ...], ...]
    value_fn: Callable[[Any, dict[int, Any]], Any] = lambda v, _d: v


def _net(d: dict[int, Any]) -> float:
    """Sign says charging (+) or discharging (-).

    Total input/output power is 0/0 when nothing is plugged in, but the pack
    still drifts (self-consumption), so fall back to the battery current.
    """
    net = float(d.get(c.DP_TOTAL_IN) or 0) - float(d.get(c.DP_TOTAL_OUT) or 0)
    if net == 0:
        net = float(value_at(d, (c.DP_HOST_PACK, 3)) or 0)
    return net


W = dict(device_class=SensorDeviceClass.POWER, native_unit_of_measurement=UnitOfPower.WATT,
         state_class=SensorStateClass.MEASUREMENT)
TEMP = dict(device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS)

SENSORS: tuple[PecronSensorDescription, ...] = (
    PecronSensorDescription(
        key="battery", paths=((c.DP_HOST_PACK, 1), (c.DP_BATTERY_PCT,)),
        device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PecronSensorDescription(
        key="battery_voltage", translation_key="battery_voltage", paths=((c.DP_HOST_PACK, 2),),
        device_class=SensorDeviceClass.VOLTAGE, native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PecronSensorDescription(
        key="battery_current", translation_key="battery_current", paths=((c.DP_HOST_PACK, 3),),
        device_class=SensorDeviceClass.CURRENT, native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PecronSensorDescription(
        key="battery_temperature", translation_key="battery_temperature",
        paths=((c.DP_HOST_PACK, 4), (c.DP_BATTERY_TEMP,)), **TEMP,
    ),
    PecronSensorDescription(
        key="charger_temperature", translation_key="charger_temperature", paths=((c.DP_CHARGER_TEMP,),),
        entity_category=EntityCategory.DIAGNOSTIC, **TEMP,
    ),
    PecronSensorDescription(
        key="inverter_temperature", translation_key="inverter_temperature", paths=((c.DP_INVERTER_TEMP,),),
        entity_category=EntityCategory.DIAGNOSTIC, **TEMP,
    ),
    PecronSensorDescription(key="input_power", translation_key="input_power", paths=((c.DP_TOTAL_IN,),), **W),
    PecronSensorDescription(key="output_power", translation_key="output_power", paths=((c.DP_TOTAL_OUT,),), **W),
    PecronSensorDescription(key="ac_input_power", translation_key="ac_input_power", paths=((c.DP_AC_IN, 1),), **W),
    PecronSensorDescription(key="dc_input_power", translation_key="dc_input_power", paths=((c.DP_DC_IN, 1),), **W),
    PecronSensorDescription(key="ac_output_power", translation_key="ac_output_power", paths=((c.DP_AC_OUT, 4),), **W),
    PecronSensorDescription(key="dc_output_power", translation_key="dc_output_power", paths=((c.DP_DC_OUT, 1),), **W),
    PecronSensorDescription(
        key="ac_output_voltage", translation_key="ac_output_voltage", paths=((c.DP_AC_OUT, 2),),
        device_class=SensorDeviceClass.VOLTAGE, native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PecronSensorDescription(
        key="ac_output_frequency", translation_key="ac_output_frequency", paths=((c.DP_AC_OUT, 1),),
        device_class=SensorDeviceClass.FREQUENCY, native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PecronSensorDescription(
        key="ac_output_power_factor", translation_key="ac_output_power_factor", paths=((c.DP_AC_OUT, 3),),
        device_class=SensorDeviceClass.POWER_FACTOR, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Firmware reports the same number in remain_time and remain_charging_time
    # (see pecron-monitor known quirks), so pick by direction of net power.
    PecronSensorDescription(
        key="time_to_empty", translation_key="time_to_empty", paths=((c.DP_REMAIN_TIME,),),
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda v, d: v if _net(d) < 0 else None,
    ),
    PecronSensorDescription(
        key="time_to_full", translation_key="time_to_full", paths=((c.DP_REMAIN_CHARGE_TIME,),),
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda v, d: v if _net(d) > 0 else None,
    ),
    PecronSensorDescription(
        key="device_status", translation_key="device_status", paths=((c.DP_DEVICE_STATUS,),),
        device_class=SensorDeviceClass.ENUM, options=list(c.DEVICE_STATUS.values()),
        value_fn=lambda v, _d: c.DEVICE_STATUS.get(int(v)),
    ),
    PecronSensorDescription(
        key="charge_status", translation_key="charge_status", paths=((c.DP_HOST_PACK, 5),),
        device_class=SensorDeviceClass.ENUM, options=list(c.CHARGE_STATUS.values()),
        value_fn=lambda v, _d: c.CHARGE_STATUS.get(int(v)), entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PecronSensorDescription(
        key="fault_code", translation_key="fault_code", paths=((c.DP_FAULT,),),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PecronConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    added: set[str] = set()

    # The first poll may hold only part of the data (the E300LFP sends its
    # nested structs in a later report), so keep adding entities as new data
    # points show up.
    @callback
    def _add_new() -> None:
        new = [
            d for d in SENSORS
            if d.key not in added and any(value_at(coordinator.data, p) is not None for p in d.paths)
        ]
        if new:
            added.update(d.key for d in new)
            async_add_entities(PecronSensor(coordinator, d) for d in new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PecronSensor(PecronEntity, SensorEntity):
    entity_description: PecronSensorDescription

    def __init__(self, coordinator: PecronCoordinator, description: PecronSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        for path in self.entity_description.paths:
            if (raw := value_at(data, path)) is not None:
                return self.entity_description.value_fn(raw, data)
        return None
