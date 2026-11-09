pytest_plugins = "pytest_homeassistant_custom_component"

import pytest

from custom_components.btr5 import gaia


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


# Tests never reach a real BTR5: sdptool finds no device unless a test fakes it.
# subprocess.run is shared by the whole process, so other commands still run.
@pytest.fixture(autouse=True)
def no_real_bluetooth(monkeypatch):
    real_run = gaia.subprocess.run

    def fake_run(args, *rest, **kwargs):
        if args[0] != "sdptool":
            return real_run(args, *rest, **kwargs)
        return gaia.subprocess.CompletedProcess(args, 1, stdout="", stderr="no device in tests")

    monkeypatch.setattr(gaia.subprocess, "run", fake_run)
