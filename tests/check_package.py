"""Fresh-wheel and tagged-SDK provenance guard, runnable without pytest."""
from __future__ import annotations

from importlib.metadata import distribution
import json
from pathlib import Path
import sys

COMMIT = "915edf9372312d4c8244df4b2b16b094cf455cff"
REFERENCE = "amplifier-agent @ git+https://github.com/microsoft/amplifier-agent@v0.22.0#subdirectory=packages/python"


def check_sdk_provenance():
    for package, subdir in (("amplifier-agent", "packages/python"), ("amplifier-agent-engine", "packages/engine")):
        dist = distribution(package)
        assert dist.version == "0.22.0", (package, dist.version)
        provenance = json.loads(dist.read_text("direct_url.json"))
        assert provenance["url"] == "https://github.com/microsoft/amplifier-agent"
        assert provenance["subdirectory"] == subdir
        assert provenance["vcs_info"]["commit_id"] == COMMIT
        assert provenance["vcs_info"]["requested_revision"] == "v0.22.0"
    import amplifier_agent
    assert amplifier_agent.__version__ == "0.22.0"
    assert all(hasattr(amplifier_agent, name) for name in (
        "create_agent", "AgentOptions", "SessionOptions", "TurnInput", "TextPart", "list_providers",
    ))


def check_wheel_install():
    check_sdk_provenance()
    dist = distribution("tmux-fleet")
    assert any(req == REFERENCE for req in dist.requires or [])
    assert not json.loads(dist.read_text("direct_url.json")).get("dir_info", {}).get("editable")
    import tmux_fleet
    location = Path(tmux_fleet.__file__).resolve()
    assert "site-packages" in location.parts, location
    assert "include-system-site-packages = true" not in (Path(sys.prefix) / "pyvenv.cfg").read_text()
    print("PASS: fresh wheel public imports, Requires-Dist, SDK/engine v0.22 tag/subdirectories/commit")


if __name__ == "__main__":
    check_wheel_install()