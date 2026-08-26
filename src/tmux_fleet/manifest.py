"""The SMART_TOOL.md manifest, reachable from the library as structured data.

The spec (manifest.md) requires the manifest to be reachable through the
library "read from the copy built into the tool", not by locating a filesystem
path -- install layouts differ by ecosystem and no path is portable. So the
accessor parses a copy shipped INSIDE the package (``_smart_tool.md``), and the
root ``SMART_TOOL.md`` (what a registry or a human browsing the repo reads) is
byte-identical to it (asserted by a test) -- "the same manifest reached two
ways", so the file and the accessor can never diverge.

The closed field set is enforced here: ``smart_tool_format``, ``name``,
``version``, ``description``, ``use_cases``, ``platforms``, ``requires``.
Anything else is refused, because "fields not listed here are not part of the
manifest".
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_PACKAGED_COPY = "_smart_tool.md"

_ALLOWED_FIELDS = {
    "smart_tool_format",
    "name",
    "version",
    "description",
    "use_cases",
    "platforms",
    "requires",
}
_REQUIRED_FIELDS = set(_ALLOWED_FIELDS)

_ALLOWED_REQUIRE_FIELDS = {"name", "purpose", "install", "optional"}


class ManifestError(RuntimeError):
    """The manifest is missing, malformed, or violates the closed field set."""


@dataclass(frozen=True)
class Requirement:
    name: str
    purpose: str
    install: str
    optional: bool = False


@dataclass(frozen=True)
class Manifest:
    smart_tool_format: int
    name: str
    version: str
    description: str
    use_cases: list[str]
    platforms: list[str]
    requires: list[Requirement]
    body: str  # the free-form markdown guidance after the frontmatter


def _read_manifest_text() -> str:
    """The raw SMART_TOOL.md text, from the copy built into the tool.

    Prefers the packaged copy (works after installation, any layout); falls back
    to the repo-root ``SMART_TOOL.md`` for a source checkout that has not been
    built.
    """
    try:
        resource = importlib.resources.files("tmux_fleet").joinpath(_PACKAGED_COPY)
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        pass

    root = Path(__file__).resolve().parents[2] / "SMART_TOOL.md"
    if root.is_file():
        return root.read_text(encoding="utf-8")

    raise ManifestError(
        "SMART_TOOL.md is not reachable -- neither the packaged copy "
        f"(tmux_fleet/{_PACKAGED_COPY}) nor the repo-root SMART_TOOL.md was "
        "found. The manifest must ship with the tool."
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(yaml_frontmatter, markdown_body)`` from a ``---``-fenced file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ManifestError(
            "SMART_TOOL.md must begin with a YAML frontmatter block fenced by "
            "'---' on its own line."
        )
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            frontmatter = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :]).strip()
            return frontmatter, body
    raise ManifestError(
        "SMART_TOOL.md frontmatter is not closed by a second '---' line."
    )


def _parse_requirement(entry: Any, index: int) -> Requirement:
    if not isinstance(entry, dict):
        raise ManifestError(
            f"requires[{index}] must be a mapping, got {type(entry).__name__}."
        )
    extra = set(entry) - _ALLOWED_REQUIRE_FIELDS
    if extra:
        raise ManifestError(
            f"requires[{index}] has fields not in the manifest schema: "
            f"{sorted(extra)}. Allowed: {sorted(_ALLOWED_REQUIRE_FIELDS)}."
        )
    for required in ("name", "purpose", "install"):
        if required not in entry:
            raise ManifestError(
                f"requires[{index}] is missing required field {required!r}."
            )
        if not isinstance(entry[required], str) or not entry[required].strip():
            raise ManifestError(
                f"requires[{index}].{required} must be a non-empty string."
            )
    install = entry["install"]
    # The spec: `install` is a reference to documentation (a relative path or a
    # URL), never a command. Guard the most obvious violation.
    if any(tok in install for tok in ("&&", "|", ";")) or install.split()[0] in (
        "pip",
        "uv",
        "brew",
        "apt",
        "npm",
        "cargo",
        "curl",
        "sudo",
    ):
        raise ManifestError(
            f"requires[{index}].install ({install!r}) looks like a command. The "
            "manifest is inert: install must reference documentation (a relative "
            "path or a URL), never a command."
        )
    optional = entry.get("optional", False)
    if not isinstance(optional, bool):
        raise ManifestError(
            f"requires[{index}].optional must be a boolean if present."
        )
    return Requirement(
        name=entry["name"],
        purpose=entry["purpose"],
        install=install,
        optional=optional,
    )


def manifest() -> Manifest:
    """The parsed manifest as a structured ``Manifest`` -- the library accessor.

    Enforces the closed field set: unknown top-level fields are refused.
    """
    text = _read_manifest_text()
    frontmatter, body = _split_frontmatter(text)
    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise ManifestError(f"SMART_TOOL.md frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError(
            f"SMART_TOOL.md frontmatter must be a mapping, got {type(data).__name__}."
        )

    extra = set(data) - _ALLOWED_FIELDS
    if extra:
        raise ManifestError(
            f"SMART_TOOL.md declares fields not in the manifest schema: "
            f"{sorted(extra)}. Fields not listed in the spec are not part of the "
            "manifest."
        )
    missing = _REQUIRED_FIELDS - set(data)
    if missing:
        raise ManifestError(
            f"SMART_TOOL.md is missing required manifest fields: {sorted(missing)}."
        )

    if data["smart_tool_format"] != 1:
        raise ManifestError(
            f"smart_tool_format must be 1 (the only schema version), got "
            f"{data['smart_tool_format']!r}."
        )
    for key in ("name", "version", "description"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ManifestError(f"{key} must be a non-empty string.")
    for key in ("use_cases", "platforms"):
        if not isinstance(data[key], list) or not all(
            isinstance(x, str) for x in data[key]
        ):
            raise ManifestError(f"{key} must be a list of strings.")
    if not isinstance(data["requires"], list):
        raise ManifestError("requires must be a list.")

    requires = [_parse_requirement(e, i) for i, e in enumerate(data["requires"])]

    return Manifest(
        smart_tool_format=data["smart_tool_format"],
        name=data["name"],
        version=data["version"],
        description=data["description"],
        use_cases=list(data["use_cases"]),
        platforms=list(data["platforms"]),
        requires=requires,
        body=body,
    )


def manifest_dict() -> dict[str, Any]:
    """The manifest frontmatter as a plain dict (the wire-friendly form)."""
    m = manifest()
    return {
        "smart_tool_format": m.smart_tool_format,
        "name": m.name,
        "version": m.version,
        "description": m.description,
        "use_cases": list(m.use_cases),
        "platforms": list(m.platforms),
        "requires": [
            {
                "name": r.name,
                "purpose": r.purpose,
                "install": r.install,
                **({"optional": True} if r.optional else {}),
            }
            for r in m.requires
        ],
    }
