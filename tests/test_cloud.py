"""CloudManager: discovering clouds must not crash without a usable clouds.yaml."""

from __future__ import annotations

from ostack9s.cloud import CloudManager
from ostack9s.tokens import TokenCache


def manager_for(path) -> CloudManager:
    return CloudManager(config_file=str(path), token_cache=TokenCache(enabled=False))


CLOUDS = """\
clouds:
  foo:
    auth:
      auth_url: https://keystone.example.org/v3
      username: foo
      password: bar
      project_name: foo
"""


def test_cloud_names_empty_without_config(tmp_path):
    assert manager_for(tmp_path / "missing.yaml").cloud_names() == []


def test_cloud_names_lists_configured_clouds(tmp_path):
    (tmp_path / "clouds.yaml").write_text(CLOUDS)
    assert manager_for(tmp_path / "clouds.yaml").cloud_names() == ["foo"]


def test_cloud_names_keeps_user_named_defaults(tmp_path):
    (tmp_path / "clouds.yaml").write_text(
        "clouds:\n"
        "  defaults:\n"
        "    auth:\n"
        "      auth_url: https://keystone.example.org/v3\n"
        "      username: foo\n"
        "      password: bar\n"
        "      project_name: bar\n"
    )
    assert manager_for(tmp_path / "clouds.yaml").cloud_names() == ["defaults"]


async def test_app_shows_message_when_no_clouds(tmp_path):
    from textual.widgets import Static

    from ostack9s.ui.app import OstdApp

    app = OstdApp(manager_for(tmp_path / "missing.yaml"), refresh=0)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.ctx is None
        message = app.query_one("#message", Static)
        assert message.display
        assert "No cloud found" in str(message.render())
