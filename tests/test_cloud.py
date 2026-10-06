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


def test_scoped_connection_targets_project_by_id(monkeypatch):
    """Switching project must scope the connection by id, not by name.

    openstacksdk's connect_as_project treats a bare string as a project *name*;
    ostack9s passes an id, so the connection must be scoped with connect_as
    (project_id=...) or it silently stays on the default project.
    """
    from types import SimpleNamespace

    import ostack9s.cloud as cloudmod
    from ostack9s.cloud import Context

    GARR, CSD = "garr-id", "csd-id"

    class FakeAuth:
        def get_access(self, session):
            return None

        def get_auth_state(self):
            return "{}"

    class FakeConn:
        def __init__(self, project=GARR, config=None, app_name=None):
            self.current_project_id = project
            self.config = SimpleNamespace(region_name="r1", config={})
            self.session = SimpleNamespace(auth=FakeAuth(), _discovery_cache={})
            self.name = "acme"

        def connect_as(self, **kwargs):
            return FakeConn(kwargs.get("project_id", self.current_project_id))

        def connect_as_project(self, project):
            # A bare string is a project *name*; an id used as a name does not
            # resolve, so it stays on the default project (the old bug).
            if isinstance(project, dict):
                return FakeConn(project.get("id", self.current_project_id))
            return FakeConn(self.current_project_id)

    monkeypatch.setattr(cloudmod, "Connection", FakeConn)
    manager = CloudManager(config_file=None, token_cache=TokenCache(enabled=False))
    manager._config = SimpleNamespace(
        get_one=lambda **kw: SimpleNamespace(
            auth={
                "auth_url": "https://keystone.example.org/v3",
                "username": "foo",
                "project_id": GARR,
            },
            config={"auth_type": "password"},
        )
    )

    conn = manager.connection(Context("acme", CSD, "csd-playground", "r1"))
    assert conn.current_project_id == CSD
