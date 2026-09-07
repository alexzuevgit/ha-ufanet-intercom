"""Validate generic v2 Home Assistant artifacts without contacting a service."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

import custom_components.ufanet_intercom.const as const_module
from custom_components.ufanet_intercom.const import (
    AUTH_PATH,
    BASE_URL,
    DISCOVERY_PATH,
    DOOR_SELECTOR,
    PLATFORMS,
    REFRESH_PATH,
    USER_AGENT,
    DiscoveredDoor,
    contract_fingerprint,
    discovered_door_binding,
    discovered_door_key,
)

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "ufanet_intercom"
HMAC_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def load_json(path: Path) -> Any:
    """Load strict JSON and reject duplicate object keys and non-finite values."""

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise AssertionError(f"duplicate key in {path.name}")
            value[key] = item
        return value

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda _value: (_ for _ in ()).throw(AssertionError("NaN")),
    )


def _python_tree(filename: str) -> ast.Module:
    return ast.parse((COMPONENT / filename).read_text(encoding="utf-8"), filename)


def _function_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _assignment_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _class_integer_assignment(tree: ast.Module, class_name: str, name: str) -> int:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == name
                    for target in statement.targets
                )
                and isinstance(statement.value, ast.Constant)
                and type(statement.value.value) is int
            ):
                return statement.value.value
    raise AssertionError(f"missing {class_name}.{name}")


def _document_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _document_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_document_shape(item) for item in value]
    return type(value)


def _placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_placeholders(item) for item in value]
    if isinstance(value, str):
        return set(re.findall(r"\{([a-z0-9_]+)\}", value))
    return set()


def _assert_concepts(text: str, concepts: dict[str, tuple[str, ...]]) -> None:
    normalized = text.casefold()
    for concept, alternatives in concepts.items():
        assert any(
            alternative.casefold() in normalized for alternative in alternatives
        ), concept


def test_fixed_provider_origin_and_mobile_app_routes() -> None:
    assert BASE_URL == "https://dom.ufanet.ru"
    assert AUTH_PATH == "/api/v1/auth/auth_by_contract/"
    assert REFRESH_PATH == "/api/v1/auth/refresh/"
    assert DISCOVERY_PATH == "/api/v0/skud/shared/"
    assert USER_AGENT == "android/4.0.14"
    assert DOOR_SELECTOR == 0


def test_identities_are_full_keyed_hmacs_for_synthetic_values() -> None:
    identity_key = bytes(range(32))
    account = "SYNTHETIC-ACCOUNT"
    synthetic_shared_id = 40_000 + 321

    account_id = contract_fingerprint(identity_key, account)
    door_key = discovered_door_key(identity_key, synthetic_shared_id)
    binding = discovered_door_binding(
        identity_key,
        shared_id=synthetic_shared_id,
        door=DOOR_SELECTOR,
        model=42,
        house=50_001,
        contract=60_001,
        cctv_number="SYNTHETIC-CAMERA",
    )

    assert account_id.startswith("ufanet-")
    assert HMAC_HEX_RE.fullmatch(account_id.removeprefix("ufanet-"))
    assert HMAC_HEX_RE.fullmatch(door_key)
    assert HMAC_HEX_RE.fullmatch(binding)
    assert len({account_id.removeprefix("ufanet-"), door_key, binding}) == 3
    assert account.casefold() not in account_id.casefold()
    assert str(synthetic_shared_id) not in door_key
    assert account_id == contract_fingerprint(identity_key, account.casefold())


def test_dynamic_door_contract_has_no_static_table_or_url_field() -> None:
    assert PLATFORMS == ("button", "binary_sensor", "camera")
    assert not hasattr(const_module, "CAMERA_LINKS")
    assert not hasattr(const_module, "TARGETS")
    assert not hasattr(const_module, "TARGET_CONFIGS")
    assert not hasattr(const_module, "CONF_URL")
    assert not hasattr(const_module, "CONF_SHARED_ID")
    assert not hasattr(const_module, "CONF_DOOR_SELECTOR")

    door_fields = {item.name for item in fields(DiscoveredDoor)}
    assert door_fields == {
        "key",
        "shared_id",
        "door",
        "model",
        "display_name",
        "binding",
        "openable",
        "trusted",
        "cctv_number",
        "house",
    }
    assert door_fields.isdisjoint({"url", "base_url", "open_url", "open_path"})

    const_tree = _python_tree("const.py")
    assert _assignment_names(const_tree).isdisjoint(
        {"TARGETS", "TARGET_CONFIGS", "STATIC_TARGETS"}
    )


def test_config_flow_is_v2_and_has_only_generic_input_contracts() -> None:
    tree = _python_tree("config_flow.py")
    assert _class_integer_assignment(tree, "UfanetIntercomConfigFlow", "VERSION") == 2
    assert {
        "async_step_user",
        "async_step_acknowledge",
        "async_step_reauth",
        "async_step_reauth_confirm",
        "async_step_init",
        "async_step_adopt",
    } <= _function_names(tree)

    user_schema: ast.AST | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "_USER_SCHEMA"
            for target in node.targets
        ):
            user_schema = node.value
            break
    assert user_schema is not None
    schema_names = {
        node.id for node in ast.walk(user_schema) if isinstance(node, ast.Name)
    }
    assert {"CONF_CONTRACT", "CONF_PASSWORD"} <= schema_names
    assert schema_names.isdisjoint(
        {"CONF_URL", "CONF_SHARED_ID", "CONF_DOOR", "CONF_SELECTOR", "CONF_TARGET"}
    )
    assert {
        node.value
        for node in ast.walk(user_schema)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }.isdisjoint({"url", "shared_id", "door", "selector", "target"})


def test_sanitized_shared_inventory_fixture_is_fully_synthetic() -> None:
    path = ROOT / "tests" / "fixtures" / "shared_inventory_sanitized.json"
    inventory = load_json(path)

    assert isinstance(inventory, list)
    assert len(inventory) == 4

    integer_fields = {"id", "model", "house", "private_status", "timeout"}
    boolean_fields = {
        "ble_support",
        "disable_button",
        "frsi",
        "is_blocked",
        "is_fav",
        "no_sound",
        "supports_key_recording",
    }
    string_fields = {
        "cctv_number",
        "dtmf_code",
        "open_in_talk",
        "open_type",
        "scope",
        "string_view",
    }
    required_fields = (
        integer_fields
        | boolean_fields
        | string_fields
        | {
            "camera",
            "contract",
            "custom_name",
            "inactivity_reason",
            "relays",
            "role",
        }
    )

    ids: list[int] = []
    fixture_strings: list[str] = []
    for item in inventory:
        assert isinstance(item, dict)
        assert set(item) == required_fields
        assert all(type(item[field]) is int for field in integer_fields)
        assert all(type(item[field]) is bool for field in boolean_fields)
        assert all(type(item[field]) is str for field in string_fields)
        assert item["camera"] is None
        assert item["contract"] is None or type(item["contract"]) is int
        assert item["custom_name"] is None or type(item["custom_name"]) is str
        assert (
            item["inactivity_reason"] is None or type(item["inactivity_reason"]) is str
        )
        assert item["relays"] == []
        assert set(item["role"]) == {"id", "name"}
        assert type(item["role"]["id"]) is int
        assert item["role"]["id"] == 2
        assert type(item["role"]["name"]) is str
        ids.append(item["id"])
        fixture_strings.extend(item[field] for field in string_fields)
        fixture_strings.append(item["role"]["name"])
        if item["custom_name"] is not None:
            fixture_strings.append(item["custom_name"])

    assert len(set(ids)) == len(inventory)
    assert all(
        value == "http" or "synth" in value.casefold() for value in fixture_strings
    )
    assert any(
        not item["disable_button"] and not item["is_blocked"] for item in inventory
    )
    assert any(item["disable_button"] for item in inventory)
    assert any(item["is_blocked"] for item in inventory)
    assert any(item["custom_name"] for item in inventory)


def test_release_metadata_is_consistent_generic_v2() -> None:
    manifest = load_json(COMPONENT / "manifest.json")
    assert manifest == {
        "domain": "ufanet_intercom",
        "name": "Ufanet Intercom",
        "after_dependencies": ["ffmpeg", "frontend", "http"],
        "codeowners": ["@alexzuevgit"],
        "config_flow": True,
        "documentation": "https://github.com/alexzuevgit/ha-ufanet-intercom",
        "integration_type": "hub",
        "iot_class": "cloud_polling",
        "issue_tracker": "https://github.com/alexzuevgit/ha-ufanet-intercom/issues",
        "requirements": [
            "argon2-cffi==25.1.0",
            "httpx==0.28.1",
            "pymicro-vad==1.0.1",
        ],
        "version": "2.1.0b7",
    }

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == manifest["version"]
    assert project["project"]["dependencies"] == manifest["requirements"]
    assert project["project"]["description"] == (
        "Home Assistant custom integration for account-discovered Ufanet shared "
        "intercoms"
    )

    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    root_packages = [
        package
        for package in locked["package"]
        if package["name"] == project["project"]["name"]
    ]
    assert len(root_packages) == 1
    assert root_packages[0]["version"] == manifest["version"]

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "2.1.0b7" in readme
    assert "beta" in readme.casefold()


def test_strings_and_translations_cover_complete_v2_flows() -> None:
    strings = load_json(COMPONENT / "strings.json")
    english = load_json(COMPONENT / "translations" / "en.json")
    russian = load_json(COMPONENT / "translations" / "ru.json")
    assert strings == english
    assert _document_shape(russian) == _document_shape(strings)
    assert _placeholders(russian) == _placeholders(strings)

    for value in (strings, russian):
        assert isinstance(value, dict)
        config = value["config"]
        options = value["options"]
        assert set(config["step"]) == {"user", "acknowledge", "reauth_confirm"}
        assert set(config["error"]) == {
            "cannot_connect",
            "invalid_auth",
            "invalid_targets",
            "unknown",
        }
        assert set(config["abort"]) == {
            "already_configured",
            "already_in_progress",
            "reauth_successful",
            "unknown",
        }
        assert set(options["step"]) == {
            "init",
            "bindings",
            "adopt",
            "voice_service",
            "voice_model",
            "voice_device",
            "voice_phrases",
            "voice_phrases_legacy",
            "voice_reset",
        }
        assert set(options["error"]) == set(config["error"]) | {
            "invalid_stt",
            "invalid_model",
            "token_origin_changed",
            "models_unsupported",
            "models_unavailable",
            "models_empty",
            "voice_service_required",
            "stale_target",
            "too_many_targets",
            "invalid_phrases",
        }
        assert set(options["abort"]) == {
            "no_pending_targets",
            "adoption_successful",
            "no_voice_targets",
            "unknown",
        }
        assert "code_phrase" in value["entity"]["binary_sensor"]

    english_warning = strings["config"]["step"]["acknowledge"]["description"]
    russian_warning = russian["config"]["step"]["acknowledge"]["description"]
    assert isinstance(english_warning, str)
    assert isinstance(russian_warning, str)
    _assert_concepts(
        english_warning,
        {
            "explicit standard action": ("button.press",),
            "physical actuation": ("physical actuation", "physically open"),
            "remote access": ("remote ui", "remote user interface"),
            "automations": ("automation",),
            "voice assistants": ("voice assistant", "voice-assistant"),
            "provider display names": ("provider-supplied display names",),
            "address-like names": ("address-like", "address like"),
            "persistent metadata": ("persist",),
            "registry persistence": ("entity registry", "device registry"),
            "backup persistence": ("backup",),
            "history persistence": ("history",),
            "explicit trust": ("confirm to trust",),
        },
    )
    _assert_concepts(
        russian_warning,
        {
            "стандартное действие": ("button.press",),
            "физическое срабатывание": ("физическ",),
            "удалённый доступ": ("удалённ",),
            "автоматизации": ("автоматизац",),
            "голосовые ассистенты": ("голосов",),
            "имена провайдера": ("провайдер",),
            "адресоподобные имена": ("адрес",),
            "сохранение метаданных": ("сохраня",),
            "реестры": ("реестр", "реестре"),
            "резервные копии": ("резервн",),
            "история": ("истори",),
            "явное доверие": ("подтвердите доверие",),
        },
    )


def test_options_menu_names_and_phrase_editor_explain_everyday_actions() -> None:
    for language, expected_menu in (
        (
            "ru",
            {
                "voice_service": "Настройки распознавания речи",
                "voice_device": "Кодовые фразы по домофонам",
                "bindings": "Обновить список домофонов",
                "voice_reset": "Сбросить настройки распознавания",
            },
        ),
        (
            "en",
            {
                "voice_service": "Speech recognition settings",
                "voice_device": "Code phrases by intercom",
                "bindings": "Refresh intercom list",
                "voice_reset": "Reset recognition settings",
            },
        ),
    ):
        options = load_json(COMPONENT / "translations" / f"{language}.json")["options"]
        steps = options["step"]
        assert steps["init"]["menu_options"] == expected_menu
        for step, title in expected_menu.items():
            assert steps[step]["title"] == title
            assert steps[step]["description"]
        for step in ("voice_phrases", "voice_phrases_legacy"):
            assert set(steps[step]["data"]) == {"phrases", "clear_phrases"}
            assert "{target_name}" in steps[step]["description"]
            assert "{phrase_count}" in steps[step]["description"]
        assert (
            steps["voice_phrases"]["description"]
            != steps["voice_phrases_legacy"]["description"]
        )
    russian = load_json(COMPONENT / "translations" / "ru.json")["options"]["step"]
    assert (
        "показать его текст невозможно"
        in russian["voice_phrases_legacy"]["description"]
    )
    assert (
        "Пустое поле сохраняет старый список"
        in russian["voice_phrases_legacy"]["description"]
    )
    assert "администратором" in russian["voice_phrases"]["description"]


def test_component_surface_is_dynamic_button_and_call_sensor_importable() -> None:
    expected = {
        "__init__.py",
        "api.py",
        "config_flow.py",
        "const.py",
        "coordinator.py",
        "diagnostics.py",
        "button.py",
        "binary_sensor.py",
        "history.py",
        "camera.py",
        "media.py",
        "rtsp_proxy.py",
        "settings_panel.py",
        "frontend/ufanet-settings.js",
        "voice_audio.py",
        "voice_models.py",
        "voice_phrase.py",
        "voice_runtime.py",
        "voice_stt.py",
        "manifest.json",
        "brand/icon.png",
        "strings.json",
        "translations/en.json",
        "translations/ru.json",
    }
    existing = {
        str(path.relative_to(COMPONENT))
        for path in COMPONENT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert existing == expected

    button_tree = _python_tree("button.py")
    imports = {
        node.module
        for node in ast.walk(button_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "homeassistant.components.button" in imports
    assert "homeassistant.components.lock" not in imports
    assert "async_press" in _function_names(button_tree)

    all_python = [_python_tree(path.name) for path in COMPONENT.glob("*.py")]
    all_functions = set().union(*(_function_names(tree) for tree in all_python))
    assert all_functions.isdisjoint(
        {"async_register_service", "async_setup_services", "async_unlock"}
    )
    assert "services.yaml" not in existing
    assert "lock.py" not in existing

    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "ha_stub_import.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_runtime_artifact_declares_two_safe_transports_ack_gate_and_migration() -> None:
    runtime_tree = _python_tree("__init__.py")
    functions = _function_names(runtime_tree)
    assert {
        "_new_session",
        "_sessions_are_safe",
        "_async_build_voice_manager",
        "_async_reload_entry",
        "async_setup_entry",
        "async_migrate_entry",
    } <= functions

    runtime_data = next(
        node
        for node in runtime_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UfanetRuntimeData"
    )
    runtime_fields = {
        statement.target.id
        for statement in runtime_data.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    }
    assert runtime_fields == {
        "client",
        "coordinator",
        "read_session",
        "open_session",
        "history_manager",
        "history_poller",
        "rtsp_proxy",
        "voice_manager",
        "voice_session",
        "proxy_unsubscribe",
    }

    setup = next(
        node
        for node in runtime_tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )
    setup_calls = [
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_new_session"
    ]
    assert len(setup_calls) == 2
    setup_names = {node.id for node in ast.walk(setup) if isinstance(node, ast.Name)}
    assert {"CONF_REQUIRES_ACK", "CONF_TRUSTED_BINDINGS"} <= setup_names

    migration = next(
        node
        for node in runtime_tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_migrate_entry"
    )
    migration_names = {
        node.id for node in ast.walk(migration) if isinstance(node, ast.Name)
    }
    assert {
        "CONF_IDENTITY_KEY",
        "CONF_TRUSTED_BINDINGS",
        "CONF_REQUIRES_ACK",
    } <= migration_names


def test_readme_documents_acknowledgement_adoption_and_legacy_quarantine() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    russian, english = readme.split("## English", maxsplit=1)
    _assert_concepts(
        english,
        {
            "explicit setup acknowledgement": ("explicit acknowledgement",),
            "binding adoption": ("adopt",),
            "options flow": ("options",),
            "display-name persistence": ("display name", "display-name"),
            "legacy entries": ("legacy 1.x",),
            "quarantine": ("quarantin",),
            "no automatic trust": ("not automatically trusted", "never auto-trusted"),
        },
    )
    _assert_concepts(
        russian,
        {
            "явное подтверждение": ("явного подтверждения", "явное подтверждение"),
            "принятие привязок": ("принять", "принят", "довер"),
            "параметры": ("параметр",),
            "сохранение имён": (
                "отображаем",
                "сохраня",
            ),
            "старые записи 1.x": ("1.x",),
            "карантин": ("карантин",),
            "нет автоматического доверия": (
                "автоматического доверия",
                "автоматически довер",
                "автодовер",
            ),
        },
    )


def test_current_release_tree_has_no_obvious_private_installation_artifacts() -> None:
    excluded_parts = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
    text_suffixes = {".json", ".md", ".py", ".toml", ".yml", ".yaml", ".lock"}
    files = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in text_suffixes
        and not excluded_parts.intersection(path.relative_to(ROOT).parts)
        and path.relative_to(ROOT).parts[:2] != ("docs", "plans")
    ]
    assert files

    forbidden_patterns = {
        "private key material": re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
        "GitHub access token": re.compile(
            r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
        ),
        "OpenAI-style secret": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        "literal JWT": re.compile(
            r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"
        ),
        "credentials in URL": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
        "local Unix home path": re.compile(
            r"(?<![A-Za-z0-9_])/(?:home|Users)/[^\s\"']+"
        ),
        "local Windows home path": re.compile(
            r"\b[A-Za-z]:\\Users\\[^\s\"']+", re.IGNORECASE
        ),
        "fixed-installation wording": re.compile(
            r"\b(?:two|2)\s+(?:fixed|hard[- ]?coded)\s+"
            r"(?:doors?|entrances?|targets?)\b",
            re.IGNORECASE,
        ),
    }

    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="strict")
        assert "\x00" not in text
        for label, pattern in forbidden_patterns.items():
            if pattern.search(text):
                violations.append(f"{path.relative_to(ROOT)}: {label}")
    assert violations == []
