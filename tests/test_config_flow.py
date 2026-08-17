from homeassistant import config_entries
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.btr5 import gaia
from custom_components.btr5.const import DOMAIN


async def test_user_flow_creates_entry_on_successful_probe(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "40:ED:98:1A:A2:C9"}
    )
    await hass.async_block_till_done()
    assert result2["type"] == "create_entry"
    assert result2["data"]["mac"] == "40:ED:98:1A:A2:C9"


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
        result["flow_id"], {"mac": "40:ED:98:1A:A2:C9"}
    )
    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_user_flow_strips_whitespace_around_mac(hass, monkeypatch):
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "  40:ed:98:1a:a2:c9\n"}
    )
    await hass.async_block_till_done()
    assert result2["type"] == "create_entry"
    assert result2["data"]["mac"] == "40:ED:98:1A:A2:C9"


async def test_user_flow_aborts_when_mac_is_already_configured(hass, monkeypatch):
    probes = []
    monkeypatch.setattr(gaia, "read_battery", lambda mac, timeout: probes.append(mac))
    entry = MockConfigEntry(
        domain=DOMAIN, data={"mac": "40:ED:98:1A:A2:C9"}, unique_id="40:ED:98:1A:A2:C9"
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mac": "40:ED:98:1A:A2:C9"}
    )
    assert result2["type"] == "abort"
    assert result2["reason"] == "already_configured"
    assert probes == []


