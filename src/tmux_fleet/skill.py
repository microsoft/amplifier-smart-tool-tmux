"""The tool's skill: what ``tmux-fleet --help`` prints.

An Agent Skill as a host delivers one to a model: where the tool's files are,
the manifest body under the tool's name, one generated line per verb pointing at
that verb's own ``--help``, and the shipped files the body refers to. The CLI
prints this and adds nothing.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

from tmux_fleet.manifest import ManifestError, manifest
from tmux_fleet.verbs import VERBS

NAME = "tmux-fleet"

#: Files the skill body refers to, relative to the skill directory. Each ships
#: inside the package, so every path resolves after installation.
SKILL_RESOURCES: tuple[str, ...] = ("SMART_TOOL.md",)


def skill_directory() -> Path:
    """The installed package root, resolved at runtime."""
    return Path(__file__).resolve().parent


def repository() -> str | None:
    """The canonical source URL from the package metadata, or None when it declares none."""
    try:
        urls = metadata(NAME).get_all("Project-URL") or []
    except PackageNotFoundError:
        return None
    for entry in urls:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "repository":
            return url.strip()
    return None


def skill_resources() -> list[str]:
    """The files the skill body refers to, relative to the skill directory."""
    return list(SKILL_RESOURCES)


def skill_body() -> str:
    """The manifest body, or a note saying why it could not be read."""
    try:
        return manifest().body
    except ManifestError as exc:
        return f"(manifest unavailable: {exc})"


def skill() -> str:
    """The skill, rendered."""
    lines = [f'<skill_content name="{NAME}">', f"Skill directory: {skill_directory()}"]
    source = repository()
    if source:
        lines.append(f"Repository: {source}")
    lines += [
        "Relative paths in this skill are relative to the skill directory.",
        "",
        f"# {NAME}",
        "",
        skill_body(),
        "",
        "## Capabilities",
        "",
    ]
    for verb in VERBS:
        kind = "model-backed" if verb["model_backed"] else "deterministic"
        lines.append(
            f"- `{verb['name']}` [{kind}] -- {verb['summary']}. "
            f"Arguments, result, and exit codes: `{NAME} {verb['name']} --help`."
        )
    lines += ["", "<skill_resources>"]
    lines += [f"  <file>{path}</file>" for path in skill_resources()]
    lines += ["</skill_resources>", "</skill_content>"]
    return "\n".join(lines) + "\n"
