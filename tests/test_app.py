"""TUI tests with a fake CloudManager: no network calls."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from textual.widgets import DataTable

from ostack9s.cloud import Context, Project, Target
from ostack9s.ui.app import OstdApp
from ostack9s.ui.modals import ConfirmScreen, FormScreen, TextScreen

CTX = Context("test", "p1", "proj-one", "r1")


def server(name: str, status: str = "ACTIVE") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{name}",
        name=name,
        status=status,
        task_state=None,
        addresses={},
        flavor={"original_name": "m1"},
        key_name=None,
        availability_zone="nova",
        is_locked=False,
        created_at="2026-01-01",
        to_dict=lambda: {"id": f"id-{name}", "name": name, "status": status},
    )


def fake_conn() -> MagicMock:
    conn = MagicMock()
    conn.compute.servers.return_value = [server("alpha"), server("beta", "SHUTOFF")]
    conn.compute.get_limits.return_value.absolute = SimpleNamespace(
        instances_used=2,
        instances=10,
        total_cores_used=4,
        total_cores=20,
        total_ram_used=4096,
        total_ram=40960,
    )
    conn.block_storage.get_quota_set.return_value = SimpleNamespace(
        usage={"volumes": 1, "gigabytes": 10}, volumes=10, gigabytes=100
    )
    quota = {"used": 1, "limit": 5}
    conn.network.get_quota.return_value = SimpleNamespace(
        floating_ips=quota, networks=quota, security_groups=quota
    )
    conn.object_store.get_account_metadata.return_value = SimpleNamespace(
        account_bytes_used=2 * 2**30, metadata={"quota-bytes": str(10 * 2**30)}
    )
    return conn


class FakeManager:
    def __init__(self) -> None:
        self.conn = fake_conn()
        self.locked = True
        self.password_required = False
        self.password: str | None = None
        self.switched: list[tuple] = []

    def cloud_names(self) -> list[str]:
        return ["test"]

    def context(self, cloud, project_id=None, region=None) -> Context:
        self.switched.append((cloud, project_id, region))
        project = next((p for p in self.projects(cloud) if p.id == project_id), None)
        project = project or Project("p1", "proj-one")
        return Context(cloud, project.id, project.name, region or "r1")

    def regions(self, cloud, project_id) -> list[str]:
        return ["r1", "r2"]

    def connection(self, ctx: Context) -> MagicMock:
        return self.conn

    def is_project_locked(self, cloud: str) -> bool:
        return self.locked

    def user_name(self, cloud: str) -> str:
        return "tester"

    def auth_type(self, cloud: str) -> str:
        return "password"

    def projects(self, cloud: str) -> list[Project]:
        if self.locked:
            return [Project("p1", "proj-one")]
        return [Project("p1", "proj-one"), Project("p2", "proj-two")]

    def targets(self, cloud: str) -> list[Target]:
        return [Target(cloud, p) for p in self.projects(cloud)]

    def needs_password(self, cloud: str) -> bool:
        return self.password_required and self.password is None

    def set_password(self, cloud: str, password: str) -> None:
        self.password = password

    def all_contexts(self) -> list[Context]:
        return [CTX, replace(CTX, region="r2")]


async def wait_rows(pilot, app, n: int) -> DataTable:
    table = app.query_one("#table", DataTable)
    for _ in range(50):
        await pilot.pause(0.05)
        if table.row_count == n:
            break
    return table


def make_app(manager: FakeManager) -> OstdApp:
    return OstdApp(manager, refresh=0)  # type: ignore[arg-type]


async def test_lists_servers_and_overview():
    manager = FakeManager()
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        table = await wait_rows(pilot, app, 2)
        assert table.row_count == 2
        assert app.ctx == CTX
        await pilot.pause(0.2)
        summary = app.summaries[CTX]
        assert summary.usage["instances"].text() == "2/10"
        assert summary.usage["ram"].text() == "4/40"


async def test_filter_rows():
    app = make_app(FakeManager())
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("slash", "b", "e", "t")
        table = await wait_rows(pilot, app, 1)
        assert table.row_count == 1
        await pilot.press("escape")
        table = await wait_rows(pilot, app, 2)
        assert table.row_count == 2


async def test_stop_requires_confirmation():
    manager = FakeManager()
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("S")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause(0.1)
        manager.conn.compute.stop_server.assert_not_called()
        await pilot.press("S")
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.pause(0.3)
        manager.conn.compute.stop_server.assert_called_once()
        assert manager.conn.compute.stop_server.call_args.args[0].name == "alpha"


async def test_start_requires_confirmation():
    manager = FakeManager()
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("down", "s")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        manager.conn.compute.start_server.assert_not_called()
        await pilot.press("y")
        await pilot.pause(0.3)
        manager.conn.compute.start_server.assert_called_once()
        assert manager.conn.compute.start_server.call_args.args[0].name == "beta"


async def test_rename_form_prefilled():
    manager = FakeManager()
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("n")
        await pilot.pause(0.3)
        assert isinstance(app.screen, FormScreen)
        await pilot.press("end", "2", "ctrl+s")
        await pilot.pause(0.3)
        manager.conn.compute.update_server.assert_called_once()
        assert manager.conn.compute.update_server.call_args.kwargs == {"name": "alpha2"}


async def test_action_error_is_notified():
    manager = FakeManager()
    manager.conn.compute.start_server.side_effect = RuntimeError("boom")
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("s")
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.pause(0.3)
        assert any("boom" in str(n.message) for n in app._notifications)


async def test_yaml_and_resource_switch():
    manager = FakeManager()
    manager.conn.network.networks.return_value = []
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("y")
        await pilot.pause(0.1)
        assert isinstance(app.screen, TextScreen)
        await pilot.press("escape")
        await pilot.press("colon", *"networks", "enter")
        await pilot.pause(0.3)
        assert app.view.kind.key == "network.network"


async def test_region_cycle():
    app = make_app(FakeManager())
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("right_square_bracket")
        await pilot.pause(0.3)
        assert app.ctx is not None and app.ctx.region == "r2"


async def test_list_error_shown():
    manager = FakeManager()
    manager.conn.compute.servers.side_effect = RuntimeError("forbidden!")
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0.5)
        assert app.view.error is not None and "forbidden!" in app.view.error


async def test_command_region_and_completion():
    app = make_app(FakeManager())
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("colon", *"region r", "2", "enter")
        await pilot.pause(0.3)
        assert app.ctx is not None and app.ctx.region == "r2"
        # Tab accepts the suggestion: "vol" -> "volumes"
        app.manager.conn.block_storage.volumes.return_value = []
        await pilot.press("colon", "v", "o", "l", "tab", "enter")
        await pilot.pause(0.3)
        assert app.view.kind.key == "block_storage.volume"


async def test_command_project():
    manager = FakeManager()
    manager.locked = False
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("colon", *"project proj-two", "enter")
        await pilot.pause(0.3)
        assert app.ctx is not None and app.ctx.project_name == "proj-two"


async def test_command_project_locked():
    app = make_app(FakeManager())
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("colon", *"project proj-two", "enter")
        await pilot.pause(0.3)
        assert app.ctx is not None and app.ctx.project_name == "proj-one"
        assert any("application credential" in str(n.message) for n in app._notifications)


async def test_unknown_command_and_child_navigation():
    manager = FakeManager()
    manager.conn.network.ports.return_value = []
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("colon", *"boh", "enter")
        await pilot.pause(0.1)
        assert any("Unknown" in str(n.message) for n in app._notifications)
        await pilot.press("w")
        await pilot.pause(0.3)
        assert app.view.kind.key == "network.port"
        assert app.view.scope == "alpha"
        manager.conn.network.ports.assert_called_with(device_id="id-alpha")
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.view.kind.key == "compute.server"


async def test_describe_pane_follows_cursor():
    app = make_app(FakeManager())
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("d")
        await pilot.pause(0.3)
        pane = app.query_one("#describe")
        assert pane.display
        assert pane.border_title == "alpha"
        await pilot.press("down")
        await pilot.pause(0.4)
        assert pane.border_title == "beta"
        await pilot.press("tab")
        assert pane.has_focus
        await pilot.press("d")
        await pilot.pause(0.1)
        assert not pane.display


async def test_resource_menu_hotkey():
    manager = FakeManager()
    manager.conn.network.routers.return_value = []
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("m")
        await pilot.pause(0.2)
        await pilot.press("r")
        await pilot.pause(0.3)
        assert app.view.kind.key == "network.router"


async def test_topology_screen():
    from ostack9s.ui.screens import TopologyScreen

    manager = FakeManager()
    net = SimpleNamespace(id="n1", name="net1", is_router_external=False, is_shared=False)
    manager.conn.network.networks.return_value = [net]
    manager.conn.network.subnets.return_value = [
        SimpleNamespace(id="s1", network_id="n1", cidr="10.0.0.0/24")
    ]
    manager.conn.network.routers.return_value = []
    manager.conn.network.ports.return_value = [
        SimpleNamespace(
            id="p1",
            network_id="n1",
            device_owner="compute:nova",
            device_id="id-alpha",
            fixed_ips=[{"ip_address": "10.0.0.5", "subnet_id": "s1"}],
        )
    ]
    manager.conn.network.ips.return_value = []
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("colon", *"topology", "enter")
        await pilot.pause(0.5)
        assert isinstance(app.screen, TopologyScreen)
        topo = app.screen.topo
        assert topo is not None
        assert topo.networks["n1"].servers[0].server == "alpha"


async def test_language_switch_in_app():
    app = make_app(FakeManager())
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("colon", *"lang it", "enter")
        await pilot.pause(0.2)
        table = app.query_one("#table", DataTable)
        assert str(table.columns[next(iter(table.columns))].label) == "NOME"
        await pilot.press("colon", *"lang en", "enter")
        await pilot.pause(0.2)
        assert str(table.columns[next(iter(table.columns))].label) == "NAME"


async def test_password_prompt():
    from ostack9s.ui.screens import PasswordScreen

    manager = FakeManager()
    manager.password_required = True
    app = make_app(manager)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, PasswordScreen)
        await pilot.press(*"s3cret", "enter")
        await wait_rows(pilot, app, 2)
        assert manager.password == "s3cret"
        assert app.ctx is not None


def test_summarize_reports_progress():
    from ostack9s.overview import summarize

    conn = fake_conn()
    updates = []
    summary = summarize(conn, CTX, updates.append)
    assert len(updates) == 5
    assert summary.usage["object_gigabytes"].text() == "2/10"
    assert not updates[-1].pending
    assert summary.servers is not None and summary.servers["ACTIVE"] == 1


def test_volume_type_quotas():
    from ostack9s.overview import summarize
    from ostack9s.ui.widgets import quota_rows

    conn = fake_conn()
    conn.block_storage.get_quota_set.return_value = SimpleNamespace(
        usage={
            "volumes": 3,
            "gigabytes": 300,
            "backup_gigabytes": 0,
            "gigabytes_foo-ssd": 250,
            "volumes_foo-ssd": 2,
            "gigabytes_bar_hdd": 50,
            "volumes___DEFAULT__": 1,
        },
        volumes=-1,
        gigabytes=-1,
        backup_gigabytes=1000,
        **{
            "gigabytes_foo-ssd": 1000,
            "volumes_foo-ssd": 10,
            "gigabytes_bar_hdd": 500,
            "volumes___DEFAULT__": -1,
        },
    )
    usage = summarize(conn, CTX, include_servers=False).usage
    assert usage["gigabytes:foo-ssd"].text() == "250/1000"
    assert usage["gigabytes:bar_hdd"].text() == "50/500"
    assert usage["volumes:foo-ssd"].text() == "2/10"
    # Unlimited types and other Cinder quotas are not listed.
    assert "volumes:__DEFAULT__" not in usage
    assert not any(k.startswith("backup") for k in usage)

    keys = [row[0] for row in quota_rows(usage)]
    assert keys[keys.index("volumes") + 1] == "volumes:foo-ssd"
    at = keys.index("gigabytes")
    assert keys[at + 1 : at + 3] == ["gigabytes:bar_hdd", "gigabytes:foo-ssd"]


async def test_privacy_toggle_masks_table():
    from ostack9s import privacy

    manager = FakeManager()
    manager.conn.compute.servers.return_value[0].addresses = {
        "net": [{"addr": "9.9.9.9", "OS-EXT-IPS:type": "floating"}]
    }
    app = make_app(manager)
    try:
        async with app.run_test(size=(160, 40)) as pilot:
            table = await wait_rows(pilot, app, 2)

            def cells():
                return " ".join(str(c) for c in table.get_row_at(0))

            assert "9.9.9.9" in cells()
            await pilot.press("ctrl+p")
            await pilot.pause(0.2)
            assert "9.9.9.9" not in cells()
            assert "203.0.113." in cells()
            await pilot.press("ctrl+p")
            await pilot.pause(0.2)
            assert "9.9.9.9" in cells()
    finally:
        privacy.reset()


async def test_overview_sort_and_hide_empty():
    from ostack9s.ui.overview_screen import OverviewScreen

    manager = FakeManager()
    busy = fake_conn()
    idle = fake_conn()
    idle.compute.servers.return_value = []
    idle.compute.get_limits.return_value.absolute = SimpleNamespace(
        instances_used=0,
        instances=0,
        total_cores_used=0,
        total_cores=0,
        total_ram_used=0,
        total_ram=0,
    )
    idle.block_storage.get_quota_set.return_value = SimpleNamespace(
        usage={"volumes": 0, "gigabytes": 0}, volumes=0, gigabytes=0
    )
    idle.network.get_quota.return_value = SimpleNamespace(
        floating_ips={"used": 0, "limit": 0},
        networks={"used": 0, "limit": 0},
        security_groups={"used": 1, "limit": 10},
    )
    idle.object_store.get_account_metadata.return_value = SimpleNamespace(
        account_bytes_used=0, metadata={}
    )
    manager.connection = lambda ctx: idle if ctx.region == "r2" else busy
    app = make_app(manager)
    async with app.run_test(size=(200, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        await pilot.press("f1")
        await pilot.pause(0.6)
        screen = app.screen
        assert isinstance(screen, OverviewScreen)
        table = screen.query_one(DataTable)
        assert table.row_count == 2
        # Entry names differ from project names here, so the cloud column is shown.
        assert "cloud" in [c.value for c in table.columns]
        screen.sort = ("cores", True)
        screen._redraw()
        assert str(table.coordinate_to_cell_key((0, 0)).row_key.value).endswith("|r1")
        await pilot.press("e")
        await pilot.pause(0.1)
        assert table.row_count == 1


async def test_search_jumps_to_result_context():
    from ostack9s.ui.search_screen import SearchScreen

    manager = FakeManager()
    app = make_app(manager)
    async with app.run_test(size=(200, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        app.run_command("search beta")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, SearchScreen)
        table = screen.query_one("#search-table", DataTable)
        for _ in range(50):
            await pilot.pause(0.05)
            if table.row_count == 2:
                break
        # One server per region: pick the one in r2.
        row = next(i for i, hit in enumerate(screen.hits) if hit.ctx.region == "r2")
        table.move_cursor(row=row)
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause(0.05)
            if app.ctx is not None and app.ctx.region == "r2":
                break
        assert app.ctx == replace(CTX, region="r2")
        assert app.view.kind.key == "compute.server"
        assert app.view.filter == "beta"
        await wait_rows(pilot, app, 1)
        assert app.query_one("#table", DataTable).row_count == 1


async def test_checks_views_open_from_command_bar():
    manager = FakeManager()
    conn = manager.conn
    conn.network.ips.return_value = [
        SimpleNamespace(
            id="fip1",
            name=None,
            floating_ip_address="192.0.2.10",
            port_id=None,
            updated_at="2020-01-01T00:00:00Z",
        )
    ]
    for lister in (
        conn.block_storage.volumes,
        conn.block_storage.snapshots,
        conn.network.ports,
        conn.network.routers,
        conn.network.security_groups,
    ):
        lister.return_value = []
    app = make_app(manager)
    async with app.run_test(size=(200, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        app.run_command("unused")
        table = await wait_rows(pilot, app, 1)
        assert app.view.kind.key == "checks.unused"
        assert "192.0.2.10" in " ".join(str(c) for c in table.get_row_at(0))
        app.run_command("audit")
        await pilot.pause(0.3)
        assert app.view.kind.key == "checks.security"
        assert app.view.error is None


async def test_swift_folders_navigation():
    from test_swift import Resp

    listings = {
        "": [{"subdir": "docs/"}, {"name": "top.txt", "bytes": 1}],
        "docs/": [{"name": "docs/a.txt", "bytes": 2}],
    }

    def request(path, method, **kwargs):
        if method == "HEAD":
            return Resp(headers={"X-Storage-Policy": "gold"})
        params = kwargs["params"]
        return Resp(body=[] if params["marker"] else listings[params["prefix"]])

    manager = FakeManager()
    manager.conn.object_store.containers.return_value = [
        SimpleNamespace(name="foo", count=2, bytes=3)
    ]
    manager.conn.object_store.request.side_effect = request
    app = make_app(manager)
    async with app.run_test(size=(200, 40)) as pilot:
        await wait_rows(pilot, app, 2)
        app.run_command("containers")
        table = await wait_rows(pilot, app, 1)
        assert "gold" in [str(c) for c in table.get_row_at(0)]
        await pilot.press("enter")
        table = await wait_rows(pilot, app, 2)
        assert [str(table.get_row_at(i)[0]) for i in range(2)] == ["docs/", "top.txt"]
        await pilot.press("enter")  # the folder comes first
        table = await wait_rows(pilot, app, 1)
        assert app.view.query == {"container": "foo", "prefix": "docs/"}
        assert str(table.get_row_at(0)[0]) == "a.txt"
        await pilot.press("enter")  # a file: YAML details, no new view
        await pilot.pause(0.1)
        assert isinstance(app.screen, TextScreen)
        assert app.view.query["prefix"] == "docs/"
