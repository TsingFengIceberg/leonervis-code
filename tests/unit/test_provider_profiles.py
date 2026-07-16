from __future__ import annotations

import json
import os

import pytest

from leonervis_code.providers.definitions import WireProtocol
from leonervis_code.providers.profile import NamedProviderProfile, ProviderProfileError
from leonervis_code.providers.profile_store import ProviderProfileStore


def profile(name: str = "local-dev") -> NamedProviderProfile:
    return NamedProviderProfile(
        name=name,
        provider_id="custom",
        protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
        model="Qwen/Qwen3.5",
        base_url="http://127.0.0.1:11434",
    )


def test_profiles_validate_and_normalize_non_secret_configuration() -> None:
    configured = profile()

    assert configured.base_url == "http://127.0.0.1:11434/v1"
    assert configured.to_dict() == {
        "name": "local-dev",
        "provider_id": "custom",
        "protocol": "openai_chat_completions",
        "model": "Qwen/Qwen3.5",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": None,
        "max_output_tokens": 1024,
        "temperature": None,
    }
    assert "api_key" not in configured.to_dict()


@pytest.mark.parametrize(
    "data",
    [
        {
            "name": "bad name",
            "provider_id": "openai",
            "protocol": "openai_chat_completions",
            "model": "gpt-5",
        },
        {
            "name": "openai",
            "provider_id": "openai",
            "protocol": "anthropic_messages",
            "model": "gpt-5",
        },
        {
            "name": "custom",
            "provider_id": "custom",
            "protocol": "openai_chat_completions",
            "model": "vendor/model",
        },
        {
            "name": "openai",
            "provider_id": "openai",
            "protocol": "openai_chat_completions",
            "model": "gpt-5",
            "api_key": "secret",
        },
    ],
)
def test_profiles_fail_closed_on_invalid_or_secret_bearing_data(data) -> None:
    with pytest.raises(ProviderProfileError):
        NamedProviderProfile.from_mapping(data)


def test_store_round_trips_profiles_and_resolves_project_over_user_active(tmp_path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "workspace" / "project.json")
    store.add_profile(profile("one"))
    store.add_profile(profile("two"))
    store.set_active("one", scope="user")

    assert store.active_selection().name == "one"
    assert store.active_selection().source == "user"

    store.set_active("two", scope="project")
    assert store.active_selection().name == "two"
    assert store.active_selection().source == "project"
    assert [item.name for item in store.list_profiles()] == ["one", "two"]
    assert stat_mode(store.user_path) == 0o600
    assert stat_mode(store.project_path) == 0o600

    store.clear_active(scope="project")
    assert store.active_selection().name == "one"


def test_store_rejects_corruption_unknown_fields_and_symlinks(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    store = ProviderProfileStore(user_path, tmp_path / "project.json")
    user_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ProviderProfileError, match="unreadable or invalid"):
        store.list_profiles()

    user_path.write_text(
        json.dumps({"schema_version": 1, "active_profile": None, "profiles": {}, "token": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(ProviderProfileError, match="unknown field"):
        store.list_profiles()

    user_path.unlink()
    target = tmp_path / "actual.json"
    target.write_text("{}", encoding="utf-8")
    user_path.symlink_to(target)
    with pytest.raises(ProviderProfileError, match="symlink"):
        store.list_profiles()


def test_store_rejects_dangling_symlinks_and_oversized_configuration(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    store = ProviderProfileStore(user_path, tmp_path / "project.json")
    user_path.symlink_to(tmp_path / "missing.json")
    with pytest.raises(ProviderProfileError, match="symlink"):
        store.list_profiles()

    user_path.unlink()
    user_path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ProviderProfileError, match="exceeds"):
        store.list_profiles()


def test_store_protects_active_profiles_and_requires_explicit_replace(tmp_path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    store.add_profile(profile())
    with pytest.raises(ProviderProfileError, match="already exists"):
        store.add_profile(profile())
    store.set_active("local-dev", scope="project")
    with pytest.raises(ProviderProfileError, match="is active"):
        store.remove_profile("local-dev")

    replacement = NamedProviderProfile(
        name="local-dev",
        provider_id="custom",
        protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
        model="other-model",
        base_url="http://127.0.0.1:11434/v1",
    )
    store.add_profile(replacement, replace=True)
    assert store.get_profile("local-dev").model == "other-model"


def stat_mode(path) -> int:
    return os.stat(path).st_mode & 0o777
