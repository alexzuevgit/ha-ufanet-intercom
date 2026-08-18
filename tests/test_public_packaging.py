"""Validate public repository packaging without network access."""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _read_utf8(path: Path) -> str:
    data = path.read_bytes()
    text = data.decode("utf-8", errors="strict")
    assert "\x00" not in text
    return text


def test_hacs_metadata_uses_only_supported_public_keys() -> None:
    metadata = json.loads(_read_utf8(ROOT / "hacs.json"))
    assert metadata == {
        "name": "Ufanet Intercom",
        "homeassistant": "2026.7.4",
        "country": "RU",
    }


def test_workflows_have_safe_triggers_and_documented_actions() -> None:
    expected = {
        "test.yml": (
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e",
            'version: "0.11.13"',
            'python-version: "3.13.5"',
            'checksum: "f830ea3d38ae1492acf53cb7f2cd0f81d6ae22b42d2d7310a6c7d42c451e1a43"',
            "uv sync --locked --dev",
            "uv run ruff check .",
            "uv run ruff format --check .",
            "uv run pytest",
            "python -m compileall",
            "json.load",
        ),
        "hacs.yml": (
            "permissions: {}",
            "schedule:",
            "docker://ghcr.io/hacs/action@sha256:41f6310585d9fb72c7a0e183cce0594355715bc24112b62bc4279b83412edccb",
            "INPUT_CATEGORY: integration",
            "INPUT_GITHUB_TOKEN: ${{ github.token }}",
        ),
        "hassfest.yml": (
            "permissions: {}",
            "schedule:",
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            "docker://ghcr.io/home-assistant/hassfest@sha256:8cd7bdb8f82430c2c13703290b1fc38dcc99957dd76ad3f230035ecee70b672d",
        ),
    }

    for filename, markers in expected.items():
        text = _read_utf8(WORKFLOWS / filename)
        assert text.startswith("name: ")
        assert "\non:\n" in text
        assert "\njobs:\n" in text
        assert "pull_request_target:" not in text
        assert "uvx " not in text
        assert text.count("\npermissions:") == 1
        assert re.search(r"(?m)^\s+[A-Za-z_-]+:\s*write\s*$", text) is None
        assert "\t" not in text
        assert all(line == line.rstrip() for line in text.splitlines())
        for trigger in ("push:", "pull_request:", "workflow_dispatch:"):
            assert trigger in text
        for marker in markers:
            assert marker in text

        uses = {
            line.strip().removeprefix("uses: ").split(maxsplit=1)[0]
            for line in text.splitlines()
            if line.strip().startswith("uses: ")
        }
        expected_uses = {
            "test.yml": {
                "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
                "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e",
            },
            "hacs.yml": {
                "docker://ghcr.io/hacs/action@sha256:41f6310585d9fb72c7a0e183cce0594355715bc24112b62bc4279b83412edccb"
            },
            "hassfest.yml": {
                "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
                "docker://ghcr.io/home-assistant/hassfest@sha256:8cd7bdb8f82430c2c13703290b1fc38dcc99957dd76ad3f230035ecee70b672d",
            },
        }
        assert uses == expected_uses[filename]
        assert all(
            re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action)
            or re.fullmatch(r"docker://ghcr\.io/[^@\s]+@sha256:[0-9a-f]{64}", action)
            for action in uses
        )
        if filename == "test.yml":
            assert "permissions:\n  contents: read" in text
        else:
            assert text.count("permissions: {}") == 1


def test_project_does_not_constrain_home_assistant_internal_uv() -> None:
    metadata = _read_utf8(ROOT / "pyproject.toml")
    assert 'requires-python = ">=3.13"' in metadata
    assert "required-version" not in metadata


def test_readme_is_plain_utf8_markdown_without_nul() -> None:
    text = _read_utf8(ROOT / "README.md")
    assert text.startswith("# Ufanet Intercom")
    assert "## Русский" in text
    assert "## English" in text
    assert "резервные копии Home Assistant" in text
    assert "Home Assistant backups" in text
    assert "voice assistant" in text
    assert "2.0.0rc6" in text
    assert "release candidate" in text.casefold()
    assert text.count("python tests/ha_entity_registry_probe.py") == 2
    assert "docs/controlled-rollout-and-rollback.md" in text
    assert "uvx " not in text
    assert "uv run ruff check ." in text
    assert "uv run ruff format --check ." in text
    assert "Show beta versions" in text
    assert "Need a different version?" in text
    assert "v2.0.0rc6" in text
    assert "verify the installed version" in text.casefold()

    rollout = _read_utf8(ROOT / "docs" / "controlled-rollout-and-rollback.md")
    for required in (
        "complete Home Assistant backup",
        "Disable every automation",
        "do not retry",
        "at most one explicitly authorized manual",
        "Show beta versions",
        "v2.0.0rc6",
        "verify the installed version",
        "Preferred rollback",
        "restore the complete backup",
    ):
        assert required.casefold() in rollout.casefold()


def test_brand_icon_is_a_complete_256_pixel_png() -> None:
    root_icon = (ROOT / "brand" / "icon.png").read_bytes()
    component_icon = (
        ROOT / "custom_components" / "ufanet_intercom" / "brand" / "icon.png"
    ).read_bytes()
    assert root_icon == component_icon
    data = component_icon
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(data):
        assert offset + 12 <= len(data)
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        assert chunk_end <= len(data)
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        assert actual_crc == expected_crc
        chunks.append((chunk_type, payload))
        offset = chunk_end
        if chunk_type == b"IEND":
            break

    assert offset == len(data)
    assert chunks[0][0] == b"IHDR"
    assert len(chunks[0][1]) == 13
    assert struct.unpack(">II", chunks[0][1][:8]) == (256, 256)
    assert sum(chunk_type == b"IHDR" for chunk_type, _ in chunks) == 1
    assert any(chunk_type == b"IDAT" for chunk_type, _ in chunks)
    assert chunks[-1] == (b"IEND", b"")


def test_public_policy_and_issue_template_files_exist() -> None:
    license_text = _read_utf8(ROOT / "LICENSE")
    security_text = _read_utf8(ROOT / "SECURITY.md")
    issue_text = _read_utf8(ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml")
    dependabot_text = _read_utf8(ROOT / ".github" / "dependabot.yml")

    assert "MIT License" in license_text
    assert "2026 Ufanet Intercom contributors" in license_text
    assert "GitHub Security Advisories" in security_text
    assert "Home Assistant backups" in security_text
    assert "unofficial" in security_text.lower()
    assert "package-ecosystem: github-actions" in dependabot_text
    assert "interval: weekly" in dependabot_text
    for marker in (
        "Home Assistant version",
        "Integration version",
        "sanitized",
        "sensitive identifiers",
        "required: true",
    ):
        assert marker in issue_text
