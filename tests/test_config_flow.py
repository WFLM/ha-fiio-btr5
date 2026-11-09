import sys
import types
from types import SimpleNamespace

from homeassistant import config_entries
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.btr5 import gaia
from custom_components.btr5.const import DOMAIN

_NEARBY = "00:00:5E:00:53:02"


def _advert(address, name="FiiO BTR5"):
    return SimpleNamespace(address=address, name=name)


def _nearby(hass, monkeypatch, *adverts):
    """Have Home Assistant's Bluetooth integration report these adverts."""
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    bluetooth.async_discovered_service_info = lambda hass, connectable=True: list(adverts)
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bluetooth)
    hass.config.components.add("bluetooth")


async def test_a_discovered_btr5_is_set_up_once_confirmed(hass, monkeypatch):
    probes = []
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: probes.append(mac))
    other = "00:00:5E:00:53:01"
    MockConfigEntry(domain=DOMAIN, data={"mac": other}, unique_id=other).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_advert(_NEARBY)
    )
    assert result["type"] == "form"
    assert result["step_id"] == "bluetooth_confirm"
    assert probes == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"] == {"mac": _NEARBY}
    assert probes == [_NEARBY]


async def test_a_discovered_btr5_that_doesnt_answer_shows_an_error(hass, monkeypatch):
    def failing_read_battery(mac, timeout):
        raise gaia.GaiaConnectError("nope")

    monkeypatch.setattr(gaia, "read_battery", failing_read_battery)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_advert(_NEARBY)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_a_discovered_btr5_already_set_up_is_ignored(hass):
    MockConfigEntry(domain=DOMAIN, data={"mac": _NEARBY}, unique_id=_NEARBY).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_advert(_NEARBY)
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_the_user_picks_a_btr5_found_nearby(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: None)
    _nearby(hass, monkeypatch, _advert(_NEARBY), _advert("00:00:5E:00:53:99", name="Desk Lamp"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    assert set(result["data_schema"].schema["mac"].container) == {_NEARBY, "manual"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": _NEARBY})
    assert result["type"] == "create_entry"
    assert result["data"] == {"mac": _NEARBY}


async def test_a_btr5_with_a_discovery_card_can_be_picked_too(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: None)
    _nearby(hass, monkeypatch, _advert(_NEARBY))
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_advert(_NEARBY)
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": _NEARBY})
    assert result["type"] == "create_entry"


async def test_the_user_can_enter_a_mac_although_a_btr5_was_found(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: None)
    _nearby(hass, monkeypatch, _advert(_NEARBY))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": "manual"})
    assert result["step_id"] == "manual"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "00:00:5E:00:53:01"}
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {"mac": "00:00:5E:00:53:01"}


async def test_without_bluetooth_the_mac_is_asked_for(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "manual"


async def test_a_btr5_already_set_up_is_not_offered(hass, monkeypatch):
    MockConfigEntry(domain=DOMAIN, data={"mac": _NEARBY}, unique_id=_NEARBY).add_to_hass(hass)
    _nearby(hass, monkeypatch, _advert(_NEARBY))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "manual"


async def test_user_flow_creates_entry_on_successful_probe(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "00:00:5E:00:53:01"}
    )
    await hass.async_block_till_done()
    assert result2["type"] == "create_entry"
    assert result2["data"]["mac"] == "00:00:5E:00:53:01"


async def test_user_flow_shows_error_on_invalid_mac(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "not-a-mac"}
    )
    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "invalid_mac"}


async def test_user_flow_shows_cannot_connect_on_gaia_error(hass, monkeypatch):
    def failing_read_battery(mac, timeout):
        raise gaia.GaiaConnectError("nope")

    monkeypatch.setattr(gaia, "read_battery", failing_read_battery)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "00:00:5E:00:53:01"}
    )
    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_a_failed_probe_is_reported_although_the_btr5_is_no_longer_found(
    hass, monkeypatch
):
    _nearby(hass, monkeypatch, _advert(_NEARBY))

    def gone_meanwhile(mac, timeout):
        _nearby(hass, monkeypatch)
        raise gaia.GaiaConnectError("nope")

    monkeypatch.setattr(gaia, "read_battery", gone_meanwhile)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": _NEARBY})

    assert result2["step_id"] == "manual"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_a_failed_probe_doesnt_keep_that_btr5_from_being_discovered(hass, monkeypatch):
    def failing_read_battery(mac, timeout):
        raise gaia.GaiaConnectError("nope")

    monkeypatch.setattr(gaia, "read_battery", failing_read_battery)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mac": _NEARBY})
    assert result["errors"] == {"base": "cannot_connect"}

    discovery = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=_advert(_NEARBY)
    )

    assert discovery["type"] == "form"
    assert discovery["step_id"] == "bluetooth_confirm"


async def test_a_mac_passed_to_the_user_step_is_normalized(hass, monkeypatch):
    probes = []
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: probes.append(mac))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}, data={"mac": " 00:00:5e:00:53:02 "}
    )

    assert result["type"] == "create_entry"
    assert result["data"] == {"mac": _NEARBY}
    assert probes == [_NEARBY]


async def test_user_flow_strips_whitespace_around_mac(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "  00:00:5e:00:53:01\n"}
    )
    await hass.async_block_till_done()
    assert result2["type"] == "create_entry"
    assert result2["data"]["mac"] == "00:00:5E:00:53:01"


async def test_user_flow_aborts_when_mac_is_already_configured(hass, monkeypatch):
    probes = []
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: probes.append(mac))
    entry = MockConfigEntry(
        domain=DOMAIN, data={"mac": "00:00:5E:00:53:01"}, unique_id="00:00:5E:00:53:01"
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "00:00:5E:00:53:01"}
    )
    assert result2["type"] == "abort"
    assert result2["reason"] == "already_configured"
    assert probes == []


