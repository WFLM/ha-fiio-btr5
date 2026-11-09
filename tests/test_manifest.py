import json
from pathlib import Path

MANIFEST_PATH = Path(__file__).parent.parent / "custom_components" / "btr5" / "manifest.json"


def test_manifest_has_required_keys():
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["domain"] == "btr5"
    assert manifest["config_flow"] is True
    assert manifest["codeowners"]
    assert manifest["version"]


# hassfest fails a component imported but not declared.
def test_bluetooth_used_by_the_config_flow_is_declared():
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert "bluetooth" in manifest["after_dependencies"]
