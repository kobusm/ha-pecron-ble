"""Output switches (AC, DC, UPS)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import const as c
from .coordinator import PecronConfigEntry, PecronCoordinator
from .entity import PecronEntity


@dataclass(frozen=True, kw_only=True)
class PecronSwitchDescription(SwitchEntityDescription):
    dp: int


SWITCHES = (
    PecronSwitchDescription(key="ac_output", translation_key="ac_output", dp=c.DP_AC_SWITCH,
                            device_class=SwitchDeviceClass.OUTLET),
    PecronSwitchDescription(key="dc_output", translation_key="dc_output", dp=c.DP_DC_SWITCH,
                            device_class=SwitchDeviceClass.OUTLET),
    PecronSwitchDescription(key="ups_mode", translation_key="ups_mode", dp=c.DP_UPS,
                            entity_category=EntityCategory.CONFIG),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PecronConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        PecronSwitch(coordinator, d) for d in SWITCHES if (coordinator.data or {}).get(d.dp) is not None
    )


class PecronSwitch(PecronEntity, SwitchEntity):
    entity_description: PecronSwitchDescription

    def __init__(self, coordinator: PecronCoordinator, description: PecronSwitchDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        val = (self.coordinator.data or {}).get(self.entity_description.dp)
        return None if val is None else bool(val)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write_bool(self.entity_description.dp, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write_bool(self.entity_description.dp, False)
