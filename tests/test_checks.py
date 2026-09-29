"""Checks, search and helpers added beyond Horizon: fake data only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ostack9s.cloud import Context, parse_time, time_left
from ostack9s.resources import checks
from ostack9s.resources.compute import server_fault, server_group_placement
from ostack9s.resources.network import (
    address_pairs,
    set_address_pairs,
    set_port_security,
    trunk_info,
)
from ostack9s.search import match_item, search_context

NOW = datetime(2026, 1, 31, tzinfo=UTC)
CTX = Context("test", "p1", "proj-one", "r1")


def rule(**kw):
    base = dict(
        direction="ingress",
        ethertype="IPv4",
        protocol="tcp",
        port_range_min=None,
        port_range_max=None,
        remote_ip_prefix="0.0.0.0/0",
        remote_group_id=None,
    )
    return {**base, **kw}


@pytest.mark.parametrize(
    ("kw", "severity"),
    [
        ({"protocol": None}, "HIGH"),
        ({}, "HIGH"),
        ({"port_range_min": 22, "port_range_max": 22}, "MEDIUM"),
        ({"port_range_min": 3306, "port_range_max": 3306}, "HIGH"),
        ({"port_range_min": 30000, "port_range_max": 31000}, "MEDIUM"),
        ({"port_range_min": 443, "port_range_max": 443}, None),
        ({"port_range_min": 22, "port_range_max": 22, "remote_ip_prefix": "192.0.2.0/24"}, None),
        ({"protocol": None, "remote_group_id": "sg-foo"}, None),
        ({"protocol": None, "direction": "egress"}, None),
        ({"protocol": "icmp"}, None),
    ],
)
def test_rule_risk(kw, severity):
    risk = checks.rule_risk(rule(**kw))
    assert (risk[0] if risk else None) == severity


def test_security_findings_count_ports_and_downgrade_unused():
    used = SimpleNamespace(id="sg1", name="foo", security_group_rules=[rule(protocol=None)])
    unused = SimpleNamespace(id="sg2", name="bar", security_group_rules=[rule(protocol=None)])
    ports = [SimpleNamespace(security_group_ids=["sg1"])]
    findings = checks.security_findings([unused, used], ports)
    assert [(f.name, f.severity) for f in findings] == [("foo", "HIGH"), ("bar", "MEDIUM")]
    assert "1" in findings[0].detail


def res(id_, **kw):
    return SimpleNamespace(id=id_, name=kw.pop("name", id_), **kw)


def test_unused_findings():
    old = (NOW - timedelta(days=40)).isoformat()
    recent = (NOW - timedelta(days=2)).isoformat()
    lists = {
        "floating IPs": [
            res("fip1", name=None, floating_ip_address="192.0.2.10", port_id=None, updated_at=old),
            res("fip2", name=None, floating_ip_address="192.0.2.11", port_id="p", updated_at=old),
        ],
        "volumes": [
            res("v1", status="available", attachments=[], size=10, updated_at=old),
            res("v2", status="in-use", attachments=[{"server_id": "s"}], size=10),
        ],
        "snapshots": [res("sn1", created_at=old, size=5), res("sn2", created_at=recent, size=5)],
        "servers": [
            res("s1", status="SHUTOFF", updated_at=old),
            res("s2", status="SHUTOFF", updated_at=recent),
            res("s3", status="ACTIVE", updated_at=old),
        ],
        "ports": [
            res("p1", device_owner="", device_id="", fixed_ips=[{"ip_address": "10.0.0.9"}]),
            res("p2", device_owner="compute:nova", device_id="s3", security_group_ids=["sg1"]),
            res("p3", device_owner="network:router_interface", device_id="r2"),
        ],
        "routers": [
            res("r1", external_gateway_info={"network_id": "ext"}),
            res("r2", external_gateway_info={"network_id": "ext"}),
        ],
        "security groups": [res("sg0", name="default"), res("sg1"), res("sg2")],
    }
    found = {(f.type, f.id) for f in checks.unused_findings(lists, NOW)}
    assert found == {
        ("floating IP", "fip1"),
        ("volume", "v1"),
        ("volume snapshot", "sn1"),
        ("server", "s1"),
        ("port", "p1"),
        ("router", "r1"),
        ("security group", "sg2"),
    }
    # MEDIUM first
    assert checks.unused_findings(lists, NOW)[0].severity == "MEDIUM"


def test_fetch_lists_turns_errors_into_findings():
    def boom(_conn):
        raise RuntimeError("nope")

    lists, errors = checks.fetch_lists(MagicMock(), {"volumes": boom, "ports": lambda c: [1]})
    assert lists == {"volumes": [], "ports": [1]}
    assert [(e.severity, e.type) for e in errors] == [("ERROR", "volumes")]


def test_delete_finding():
    conn = MagicMock()
    fip = res("fip1")
    item = checks.Finding("MEDIUM", "floating IP", "192.0.2.10", "fip1", "x", resource=fip)
    checks.delete_finding(conn, item, {})
    conn.network.delete_ip.assert_called_once_with(fip)
    server = checks.Finding("LOW", "server", "foo", "s1", "x", resource=res("s1"))
    with pytest.raises(ValueError):
        checks.delete_finding(conn, server, {})
    conn.compute.delete_server.assert_not_called()


def test_finding_to_dict_is_plain():
    item = checks.Finding("LOW", "port", "foo", "p1", "x", resource={"id": "p1"})
    assert item.to_dict()["resource"] == {"id": "p1"}


@pytest.mark.parametrize(
    ("policy", "hosts", "expected"),
    [
        ("anti-affinity", ["h1", "h2"], "OK"),
        ("anti-affinity", ["h1", "h1"], "VIOLATED"),
        ("soft-anti-affinity", ["h1", "h1", "h2"], "SHARED_HOST"),
        ("affinity", ["h1", "h2"], "VIOLATED"),
        ("soft-affinity", ["h1", "h2"], "SPREAD"),
        ("affinity", ["h1", "h1"], "OK"),
        ("anti-affinity", ["h1"], "OK"),
    ],
)
def test_server_group_placement(policy, hosts, expected):
    assert server_group_placement(SimpleNamespace(policy=policy), hosts) == expected


def test_server_fault():
    assert server_fault(SimpleNamespace(fault={"message": "No valid host"})) == "No valid host"
    assert server_fault(SimpleNamespace(fault=None)) == ""
    assert len(server_fault(SimpleNamespace(fault={"message": "x" * 200}))) == 80


def test_port_columns():
    port = SimpleNamespace(
        allowed_address_pairs=[{"ip_address": "10.0.0.100"}, {"ip_address": "10.0.1.0/24"}],
        trunk_details={"sub_ports": [{"segmentation_id": 200}, {"segmentation_id": 100}]},
    )
    assert address_pairs(port) == "10.0.0.100, 10.0.1.0/24"
    assert trunk_info(port) == "parent, VLAN 100,200"
    assert trunk_info(SimpleNamespace(trunk_details=None)) == ""


def test_set_address_pairs_parses_lines():
    conn = MagicMock()
    set_address_pairs(conn, "port", {"pairs": "10.0.0.100\n10.0.1.0/24 fa:16:3e:00:00:01\n"})
    conn.network.update_port.assert_called_once_with(
        "port",
        allowed_address_pairs=[
            {"ip_address": "10.0.0.100"},
            {"ip_address": "10.0.1.0/24", "mac_address": "fa:16:3e:00:00:01"},
        ],
    )


def test_disabling_port_security_clears_groups():
    conn = MagicMock()
    set_port_security(conn, "port", {"enabled": False})
    conn.network.update_port.assert_called_once_with(
        "port", is_port_security_enabled=False, security_group_ids=[]
    )


def test_time_helpers():
    assert parse_time("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_time("2026-01-01T00:00:00").tzinfo is UTC
    assert parse_time(None) is None
    assert time_left(NOW + timedelta(days=3, hours=2), NOW) == "3d"
    assert time_left(NOW + timedelta(hours=5, minutes=12), NOW) == "5h 12m"
    assert time_left(NOW - timedelta(minutes=1), NOW) == "0m"
    assert time_left(None) == ""


def test_match_item():
    assert match_item("192.0.2.1", ["foo", "192.0.2.10"]) == ("192.0.2.10", False)
    assert match_item("192.0.2.1", ["192.0.2.10", "192.0.2.1"]) == ("192.0.2.1", True)
    assert match_item("FOO", ["foo-bar"]) == ("foo-bar", False)
    assert match_item("zzz", ["foo", None]) is None


def test_search_context_skips_failing_services():
    conn = MagicMock()
    conn.compute.servers.return_value = [
        SimpleNamespace(id="id-foo", name="foo", addresses={"net": [{"addr": "192.0.2.5"}]})
    ]
    conn.network.ips.return_value = [
        SimpleNamespace(id="fip1", floating_ip_address="192.0.2.5", fixed_ip_address="10.0.0.5")
    ]
    for lister in (
        conn.network.ports,
        conn.block_storage.volumes,
        conn.network.networks,
        conn.network.subnets,
        conn.network.routers,
        conn.network.security_groups,
    ):
        lister.return_value = []
    conn.load_balancer.load_balancers.side_effect = RuntimeError("forbidden")
    hits, failed = search_context(conn, CTX, "192.0.2.5")
    assert {(h.kind, h.id, h.exact) for h in hits} == {
        ("compute.server", "id-foo", True),
        ("network.floating_ip", "fip1", True),
    }
    assert failed == ["load_balancer.loadbalancer"]
    server = next(h for h in hits if h.kind == "compute.server")
    assert server.filter == "foo"
