from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_STEP_SIZE,
    DOMAIN,
    MAX_DEBOUNCE_SECONDS,
    MAX_STEP_SIZE,
    MIN_DEBOUNCE_SECONDS,
    MIN_STEP_SIZE,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    mac = entry.data["mac"]
    async_add_entities(
        [
            Btr5StepSizeNumber(entry, mac),
            Btr5DebounceSecondsNumber(entry, mac),
        ]
    )


class _Btr5ConfigNumber(NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    _key: str
    _default: float

    def __init__(self, entry: ConfigEntry, mac: str, name: str) -> None:
        self._entry = entry
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name="FiiO BTR5",
            manufacturer="FiiO",
            model="BTR5 2021",
        )

    @property
    def native_value(self) -> float:
        return self._entry.options.get(self._key, self._default)

    def _coerce(self, value: float) -> float | int:
        return value

    async def async_set_native_value(self, value: float) -> None:
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, self._key: self._coerce(value)}
        )
        self.async_write_ha_state()


class Btr5StepSizeNumber(_Btr5ConfigNumber):
    _key = "step_size"
    _default = DEFAULT_STEP_SIZE
    _attr_native_min_value = MIN_STEP_SIZE
    _attr_native_max_value = MAX_STEP_SIZE
    _attr_native_step = 1
    _attr_icon = "mdi:stairs"

    def __init__(self, entry: ConfigEntry, mac: str) -> None:
        super().__init__(entry, mac, "Step Size")

    def _coerce(self, value: float) -> int:
        return int(value)


class Btr5DebounceSecondsNumber(_Btr5ConfigNumber):
    _key = "debounce_seconds"
    _default = DEFAULT_DEBOUNCE_SECONDS
    _attr_native_min_value = MIN_DEBOUNCE_SECONDS
    _attr_native_max_value = MAX_DEBOUNCE_SECONDS
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:timer-outline"

    def __init__(self, entry: ConfigEntry, mac: str) -> None:
        super().__init__(entry, mac, "Debounce Window")
