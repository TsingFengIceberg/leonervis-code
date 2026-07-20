from __future__ import annotations

import json
import os
from uuid import UUID

import pytest

from leonervis_code.providers.definitions import WireProtocol
from leonervis_code.providers.profile import (
    NamedProviderProfile,
    ProviderProfileError,
    ProviderProfileSpec,
    legacy_profile_id,
    profile_fingerprint,
)
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
    assert configured.to_spec().to_dict() == {
        "name": "local-dev",
        "provider_id": "custom",
        "protocol": "openai_chat_completions",
        "model": "Qwen/Qwen3.5",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": None,
        "max_output_tokens": 1024,
        "context_window_tokens": None,
        "temperature": None,
    }
    assert "api_key" not in configured.to_dict()
    assert str(UUID(configured.profile_id)) == configured.profile_id
    assert configured.revision == 1


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


def test_store_creates_v2_identity_and_supports_revisioned_no_op_replace(tmp_path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    created = store.add_profile(profile().to_spec())
    original_bytes = store.user_path.read_bytes()

    unchanged = store.replace_profile(
        created.profile_id, created.to_spec(), expected_revision=created.revision
    )
    assert unchanged == created
    assert store.user_path.read_bytes() == original_bytes

    changed = store.replace_profile(
        created.profile_id,
        ProviderProfileSpec(
            name=created.name,
            provider_id=created.provider_id,
            protocol=created.protocol,
            model="new-model",
            base_url=created.base_url,
        ),
        expected_revision=created.revision,
    )
    assert changed.profile_id == created.profile_id
    assert changed.revision == 2
    with pytest.raises(ProviderProfileError, match="revision conflict"):
        store.rename_profile(changed.profile_id, "renamed", expected_revision=1)


def test_store_rename_preserves_id_and_remove_readd_gets_new_id(tmp_path) -> None:
    store = ProviderProfileStore(tmp_path / "user.json", tmp_path / "project.json")
    created = store.add_profile(profile().to_spec())
    renamed = store.rename_profile(
        created.profile_id, "renamed", expected_revision=created.revision
    )

    assert renamed.name == "renamed"
    assert renamed.profile_id == created.profile_id
    assert renamed.revision == 2
    assert store.get_profile_by_id(created.profile_id) == renamed

    store.remove_profile_by_id(renamed.profile_id, expected_revision=2)
    readded = store.add_profile(profile("renamed").to_spec())
    assert readded.profile_id != renamed.profile_id
    assert readded.revision == 1


def test_v1_reads_are_deterministic_and_do_not_write(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    project_path = tmp_path / "project.json"
    write_v1_user(user_path, "Legacy", active="Legacy")
    write_v1_project(project_path, "Legacy")
    before_user = user_path.read_bytes()
    before_project = project_path.read_bytes()
    store = ProviderProfileStore(user_path, project_path)

    loaded = store.get_profile("Legacy")
    selection = store.active_selection()

    assert loaded.profile_id == legacy_profile_id("Legacy")
    assert loaded.profile_id != legacy_profile_id("legacy")
    assert loaded.revision == 1
    assert selection.profile_id == loaded.profile_id
    assert selection.revision == 1
    assert user_path.read_bytes() == before_user
    assert project_path.read_bytes() == before_project


def test_explicit_migration_rewrites_both_v1_files_as_v3(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    project_path = tmp_path / "project.json"
    write_v1_user(user_path, "Legacy", active="Legacy")
    write_v1_project(project_path, "Legacy")
    store = ProviderProfileStore(user_path, project_path)

    store.migrate()

    user = json.loads(user_path.read_text(encoding="utf-8"))
    project = json.loads(project_path.read_text(encoding="utf-8"))
    profile_id = legacy_profile_id("Legacy")
    assert user["schema_version"] == 3
    assert user["active_profile_id"] == profile_id
    assert user["profiles"][profile_id]["revision"] == 1
    assert project == {"schema_version": 3, "active_profile_id": profile_id}


@pytest.mark.parametrize(("user_version", "project_version"), [(1, 1), (1, 2), (2, 1), (2, 2)])
def test_store_resolves_all_mixed_schema_combinations(
    tmp_path, user_version: int, project_version: int
) -> None:
    user_path = tmp_path / "user.json"
    project_path = tmp_path / "project.json"
    profile_id = legacy_profile_id("Legacy")
    if user_version == 1:
        write_v1_user(user_path, "Legacy", active=None)
    else:
        write_v2_user(user_path, profile("Legacy"), profile_id=profile_id)
    if project_version == 1:
        write_v1_project(project_path, "Legacy")
    else:
        project_path.write_text(
            json.dumps({"schema_version": 2, "active_profile_id": profile_id}),
            encoding="utf-8",
        )

    selection = ProviderProfileStore(user_path, project_path).active_selection()

    assert selection.name == "Legacy"
    assert selection.profile_id == profile_id
    assert selection.source == "project"


def test_v2_rename_remains_addressable_from_dormant_v1_project_selection(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    project_path = tmp_path / "project.json"
    profile_id = legacy_profile_id("Legacy")
    write_v2_user(user_path, profile("Legacy"), profile_id=profile_id)
    write_v1_project(project_path, "Legacy")
    store = ProviderProfileStore(user_path, project_path)

    renamed = store.rename_profile(profile_id, "Renamed", expected_revision=1)
    selection = store.active_selection()

    assert renamed.profile_id == profile_id
    assert selection.name == "Renamed"
    assert selection.profile_id == profile_id
    assert json.loads(project_path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_writes_upgrade_only_the_written_file(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    project_path = tmp_path / "project.json"
    write_v1_user(user_path, "Legacy", active=None)
    write_v1_project(project_path, None)
    store = ProviderProfileStore(user_path, project_path)

    store.set_active("Legacy", scope="project")

    assert json.loads(user_path.read_text(encoding="utf-8"))["schema_version"] == 1
    project = json.loads(project_path.read_text(encoding="utf-8"))
    assert project["schema_version"] == 3
    assert project["active_profile_id"] == legacy_profile_id("Legacy")


def test_future_schema_versions_fail_closed_for_each_layer(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    project_path = tmp_path / "project.json"
    user_path.write_text(json.dumps({"schema_version": 4}), encoding="utf-8")
    store = ProviderProfileStore(user_path, project_path)
    with pytest.raises(ProviderProfileError, match="unsupported user"):
        store.list_profiles()

    write_v1_user(user_path, "Legacy", active=None)
    project_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(ProviderProfileError, match="unsupported project"):
        store.active_selection()


def test_v2_profiles_require_store_identity_fields(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    configured = profile().to_spec().to_dict()
    user_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "active_profile_id": None,
                "profiles": {"00000000-0000-4000-8000-000000000001": configured},
            }
        ),
        encoding="utf-8",
    )
    store = ProviderProfileStore(user_path, tmp_path / "project.json")

    with pytest.raises(ProviderProfileError, match="missing required field"):
        store.list_profiles()


def test_profile_fingerprint_is_canonical_and_excludes_identity_and_name() -> None:
    first = profile("one")
    second = NamedProviderProfile(
        name="two",
        provider_id=first.provider_id,
        protocol=first.protocol,
        model=first.model,
        base_url=first.base_url,
        profile_id="00000000-0000-4000-8000-000000000001",
        revision=9,
    )

    assert profile_fingerprint(first) == profile_fingerprint(second)
    assert len(profile_fingerprint(first)) == 64
    assert profile_fingerprint(first) != profile_fingerprint(
        ProviderProfileSpec(
            name="one",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="different",
            base_url="http://127.0.0.1:11434/v1",
        )
    )


def test_profile_context_window_override_is_validated_and_fingerprinted() -> None:
    configured = ProviderProfileSpec(
        name="local",
        provider_id="custom",
        protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
        model="model",
        base_url="http://127.0.0.1:11434/v1",
        context_window_tokens=131_072,
    )

    assert configured.to_dict()["context_window_tokens"] == 131_072
    assert (
        configured.fingerprint()
        != ProviderProfileSpec(
            name="local",
            provider_id="custom",
            protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
            model="model",
            base_url="http://127.0.0.1:11434/v1",
        ).fingerprint()
    )
    for invalid in (0, -1, True, 100_000_001):
        with pytest.raises(ProviderProfileError, match="context window"):
            ProviderProfileSpec(
                name="local",
                provider_id="custom",
                protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
                model="model",
                base_url="http://127.0.0.1:11434/v1",
                context_window_tokens=invalid,
            )


def test_schema_v2_rejects_context_override_and_v3_accepts_it(tmp_path) -> None:
    user_path = tmp_path / "user.json"
    profile_id = "00000000-0000-4000-8000-000000000001"
    configured = NamedProviderProfile(
        name="local",
        provider_id="custom",
        protocol=WireProtocol.OPENAI_CHAT_COMPLETIONS,
        model="model",
        base_url="http://127.0.0.1:11434/v1",
        context_window_tokens=131_072,
        profile_id=profile_id,
    )
    user_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "active_profile_id": None,
                "profiles": {profile_id: configured.to_dict()},
            }
        ),
        encoding="utf-8",
    )
    store = ProviderProfileStore(user_path, tmp_path / "project.json")
    with pytest.raises(ProviderProfileError, match="unknown field"):
        store.list_profiles()

    data = json.loads(user_path.read_text(encoding="utf-8"))
    data["schema_version"] = 3
    user_path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get_profile("local").context_window_tokens == 131_072


def write_v1_user(path, name: str, *, active: str | None) -> None:
    configured = profile(name).to_spec().to_dict(include_context_window=False)
    path.write_text(
        json.dumps({"schema_version": 1, "active_profile": active, "profiles": {name: configured}}),
        encoding="utf-8",
    )


def write_v1_project(path, active: str | None) -> None:
    path.write_text(json.dumps({"schema_version": 1, "active_profile": active}), encoding="utf-8")


def write_v2_user(path, configured: NamedProviderProfile, *, profile_id: str) -> None:
    owned = NamedProviderProfile(
        name=configured.name,
        provider_id=configured.provider_id,
        protocol=configured.protocol,
        model=configured.model,
        base_url=configured.base_url,
        api_key_env=configured.api_key_env,
        max_output_tokens=configured.max_output_tokens,
        temperature=configured.temperature,
        profile_id=profile_id,
        revision=1,
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "active_profile_id": None,
                "profiles": {profile_id: owned.to_dict(include_context_window=False)},
            }
        ),
        encoding="utf-8",
    )


def stat_mode(path) -> int:
    return os.stat(path).st_mode & 0o777
