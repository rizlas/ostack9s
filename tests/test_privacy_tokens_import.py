"""Privacy masking, token cache and Horizon credential import."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from ostack9s import privacy
from ostack9s.importer import entry_name, merge
from ostack9s.tokens import TokenCache, cache_key, is_fresh


@pytest.fixture(autouse=True)
def _reset_privacy():
    privacy.reset()
    yield
    privacy.reset()


def test_mask_disabled_is_identity():
    text = "9.9.9.9 jane.doe@example.com"
    assert privacy.mask(text) == text


def test_mask_public_ip_keeps_private_and_is_consistent():
    privacy.set_enabled(True)
    out = privacy.mask("net=10.0.42.63,9.9.9.9* gw 9.9.9.9 dns 8.8.8.8")
    assert "10.0.42.63" in out
    assert "9.9.9.9" not in out and "8.8.8.8" not in out
    first, second, third = out.split("*")[0].split(",")[1], out.split("gw ")[1].split()[0], out[-7:]
    assert first == second  # same value, same placeholder
    assert first != third.strip()


def test_mask_ids_emails_keys_secrets():
    privacy.set_enabled(True)
    text = (
        "user jane.doe@example.com project 0123456789abcdef0123456789abcdef "
        "port 123e4567-e89b-12d3-a456-426614174000 mac fa:16:3e:12:34:56 "
        "https://x/spice_auto.html?token=9de7 application_credential_secret: abc "
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample123456789 v6 2001:760:2c00::10"
    )
    out = privacy.mask(text)
    for secret in (
        "jane.doe@example.com",
        "0123456789abcdef0123456789abcdef",
        "123e4567-e89b-12d3-a456-426614174000",
        "fa:16:3e:12:34:56",
        "token=9de7",
        ": abc",
        "AAAAC3NzaC1lZDI1NTE5",
        "2001:760:2c00::10",
    ):
        assert secret not in out, secret


def test_mask_registered_names_and_words():
    privacy.set_enabled(True)
    privacy.register_names("project", ["janedoe-sandbox"])
    privacy.add_words(["janedoe"])
    out = privacy.mask("janedoe-sandbox key tf-janedoe")
    assert out.startswith("project-1 ")
    assert "janedoe" not in out


def _state(minutes: int) -> str:
    expires = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
    return json.dumps({"auth_token": "tok", "body": {"token": {"expires_at": expires}}})


def test_token_cache_roundtrip_and_permissions(tmp_path):
    cache = TokenCache(tmp_path / "tokens")
    key = cache_key("c", {"auth_url": "u", "application_credential_secret": "s"})
    assert cache.load(key) is None
    cache.save(key, _state(60))
    assert cache.load(key) is not None
    path = tmp_path / "tokens" / f"{key}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "tokens").stat().st_mode) == 0o700


def test_token_cache_ignores_expiring_and_disabled(tmp_path):
    assert not is_fresh(_state(5))
    cache = TokenCache(tmp_path, enabled=False)
    cache.save("k", _state(60))
    assert cache.load("k") is None


def test_cache_key_ignores_secrets():
    a = cache_key("c", {"auth_url": "u", "password": "one"})
    b = cache_key("c", {"auth_url": "u", "password": "two"})
    assert a == b
    assert a != cache_key("c", {"auth_url": "u"}, project_id="p")


def _horizon_file(path, cred_id):
    path.write_text(
        yaml.safe_dump(
            {
                "clouds": {
                    "openstack": {
                        "auth": {
                            "auth_url": "https://keystone.example:5000/v3",
                            "application_credential_id": cred_id,
                            "application_credential_secret": "s",
                        },
                        "auth_type": "v3applicationcredential",
                    }
                }
            }
        )
    )
    return path


def test_import_merges_with_backup(tmp_path):
    target = tmp_path / "clouds.yaml"
    target.write_text(yaml.safe_dump({"clouds": {"existing": {"auth": {}}}}))
    a = _horizon_file(tmp_path / "a.yaml", "id-a")
    b = _horizon_file(tmp_path / "b.yaml", "id-b")
    projects = {"id-a": "Foo", "id-b": "bar-project"}

    def resolve(path, entry):
        data = yaml.safe_load(path.read_text())
        return projects[data["clouds"][entry]["auth"]["application_credential_id"]]

    report, backup = merge([a, b], target, prefix="acme-", resolve_project=resolve)
    assert [r.status for r in report] == ["added", "added"]
    clouds = yaml.safe_load(target.read_text())["clouds"]
    assert set(clouds) == {"existing", "acme-foo", "acme-bar-project"}
    assert backup is not None and backup.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # A second run does not duplicate or overwrite.
    report, _ = merge([a], target, prefix="acme-", resolve_project=resolve)
    assert report[0].status == "skipped"


def test_import_dry_run_writes_nothing(tmp_path):
    target = tmp_path / "clouds.yaml"
    a = _horizon_file(tmp_path / "a.yaml", "id-a")
    report, backup = merge([a], target, dry_run=True, resolve_project=lambda p, e: "p1")
    assert report[0].status == "added"
    assert not target.exists() and backup is None


def test_entry_name():
    assert entry_name("Foo Bar") == "foo-bar"
    assert entry_name("foo", "acme-") == "acme-foo"
