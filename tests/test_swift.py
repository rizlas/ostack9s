"""Swift helpers and actions against a fake endpoint (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ostack9s.resources import swift
from ostack9s.resources.swift import Entry

ENDPOINT = "https://swift.example.org/v1/AUTH_foo"


class Resp:
    def __init__(self, status: int = 200, headers=None, body=None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = ""
        self.content = b""
        self.reason = ""
        self.url = ENDPOINT

    def json(self):
        return self._body


def fake_conn(*responses: Resp) -> MagicMock:
    conn = MagicMock()
    conn.object_store.request.side_effect = list(responses)
    conn.object_store.get_endpoint.return_value = ENDPOINT
    return conn


def calls(conn):
    return [(c.args[1], c.args[0], c.kwargs) for c in conn.object_store.request.call_args_list]


def test_entry_from_listing():
    assert swift.entry_from_listing("c", {"subdir": "a/"}).is_dir
    marker = swift.entry_from_listing("c", {"name": "a", "content_type": "application/directory"})
    assert marker.is_dir and marker.name == "a/"
    slo = swift.entry_from_listing("c", {"name": "big", "bytes": 5, "slo_etag": "x"})
    assert slo.large == "SLO" and slo.to_dict()["slo_etag"] == "x"
    assert swift.entry_type(slo) == "SLO"


def test_list_entries_pages_and_skips_own_marker():
    conn = fake_conn(
        Resp(
            body=[{"subdir": "docs/b/"}, {"name": "docs/", "content_type": "application/directory"}]
        ),
        Resp(body=[{"name": "docs/a.txt", "bytes": 2048, "content_type": "text/plain"}]),
        Resp(body=[]),
    )
    entries = swift.list_entries(conn, {"container": "foo", "prefix": "docs/"})
    assert [(e.name, e.is_dir) for e in entries] == [("docs/b/", True), ("docs/a.txt", False)]
    assert [swift.entry_label(e) for e in entries] == ["b/", "a.txt"]
    method, path, kwargs = calls(conn)[1]
    assert (method, path) == ("GET", "/foo")
    assert kwargs["params"]["delimiter"] == "/" and kwargs["params"]["marker"] == "docs/"


def test_container_info():
    info = swift.container_info(
        {
            "x-storage-policy": "ec",
            "x-container-read": ".r:*,.rlistings",
            "x-container-meta-quota-bytes": str(2 * 2**30),
            "x-history-location": "foo-old",
        }
    )
    assert (info.policy, info.public, info.quota_bytes) == ("ec", True, 2 * 2**30)
    assert info.versioning == "history → foo-old"
    assert not swift.container_info({}).public
    container = SimpleNamespace(info=info)
    assert swift.container_access(container) == "public"
    assert swift.container_quota(container) == "2.0 GiB"
    assert swift.container_access(SimpleNamespace()) == ""


def test_list_containers_fills_details():
    conn = fake_conn(Resp(headers={"X-Storage-Policy": "gold"}))
    conn.object_store.containers.return_value = [SimpleNamespace(name="foo")]
    (container,) = swift.list_containers(conn, {})
    assert container.info.policy == "gold"


def test_account_usage_reads_sdk_metadata():
    from openstack.object_store.v1.account import Account

    account = Account()
    resp = MagicMock(status_code=204)
    resp.headers = {"X-Account-Bytes-Used": "77853", "X-Account-Meta-Quota-Bytes": str(2**31)}
    account._translate_response(resp, has_body=False)
    conn = MagicMock()
    conn.object_store.get_account_metadata.return_value = account
    used, limit = swift.account_usage(conn)
    assert limit == 2.0 and used > 0


def test_account_usage():
    conn = MagicMock()
    conn.object_store.get_account_metadata.return_value = SimpleNamespace(
        account_bytes_used=2**30, metadata={"quota-bytes": str(4 * 2**30)}
    )
    assert swift.account_usage(conn) == (1.0, 4.0)
    conn.object_store.get_account_metadata.return_value = SimpleNamespace(
        account_bytes_used=0, metadata={}
    )
    assert swift.account_usage(conn) == (0.0, -1)


def test_target():
    assert swift.target(SimpleNamespace(name="foo")) == ("foo", "")
    assert swift.target(Entry("foo", "a/b/", is_dir=True)) == ("foo", "a/b/")
    assert swift.target(Entry("foo", "a/b/c.txt")) == ("foo", "a/b/")
    assert swift.target(Entry("foo", "c.txt")) == ("foo", "")


def test_upload_file_and_directory(tmp_path):
    (tmp_path / "one.txt").write_text("1")
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("a")
    (tree / "sub" / "b.txt").write_text("b")
    conn = MagicMock()
    folder = Entry("foo", "docs/", is_dir=True)
    swift.upload(conn, folder, {"path": str(tmp_path / "one.txt"), "name": ""})
    swift.upload(conn, folder, {"path": str(tree), "name": ""})
    names = sorted(c.args[1] for c in conn.object_store.create_object.call_args_list)
    assert names == ["docs/one.txt", "docs/tree/a.txt", "docs/tree/sub/b.txt"]
    with pytest.raises(FileNotFoundError):
        swift.upload(conn, folder, {"path": str(tmp_path / "missing")})


def test_create_folder():
    conn = fake_conn(Resp(201))
    swift.create_folder(conn, SimpleNamespace(name="foo"), {"name": "/new/"})
    method, path, kwargs = calls(conn)[0]
    assert (method, path) == ("PUT", "/foo/new/")
    assert kwargs["headers"]["Content-Type"] == "application/directory"


def test_edit_metadata_keeps_expiry():
    conn = fake_conn(
        Resp(headers={"X-Object-Meta-Old": "1", "X-Delete-At": "1900000000"}), Resp(202)
    )
    entry = Entry("foo", "a.txt")
    swift.edit_metadata(conn, entry, {"metadata": "Owner: bar\nbad line\n"})
    method, path, kwargs = calls(conn)[1]
    assert (method, path) == ("POST", "/foo/a.txt")
    assert kwargs["headers"] == {"x-object-meta-owner": "bar", "X-Delete-At": "1900000000"}


def test_set_expiry_keeps_metadata():
    conn = fake_conn(Resp(headers={"X-Object-Meta-Owner": "bar"}), Resp(202))
    swift.set_expiry(conn, Entry("foo", "a.txt"), {"days": 2})
    headers = calls(conn)[1][2]["headers"]
    assert headers == {"x-object-meta-owner": "bar", "X-Delete-After": str(2 * 86400)}


def test_metadata_text():
    conn = fake_conn(Resp(headers={"X-Object-Meta-B": "2", "X-Object-Meta-A": "1", "Etag": "x"}))
    assert swift.metadata_text(conn, Entry("foo", "a.txt")) == "a: 1\nb: 2"


def test_temp_url():
    conn = MagicMock()
    conn.object_store.get_endpoint.return_value = ENDPOINT
    conn.object_store.get_temp_url_key.return_value = None
    with pytest.raises(ValueError):
        swift.temp_url(conn, Entry("foo", "a b.txt"), {"minutes": 5})
    conn.object_store.set_account_temp_url_key.assert_not_called()
    conn.object_store.generate_temp_url.return_value = (
        "/v1/AUTH_foo/foo/a b.txt?temp_url_sig=abc&temp_url_expires=1"
    )
    url = swift.temp_url(conn, Entry("foo", "a b.txt"), {"minutes": 5, "create_key": True})
    conn.object_store.set_account_temp_url_key.assert_called_once()
    path, seconds, method = conn.object_store.generate_temp_url.call_args.args
    assert (path, seconds, method) == ("/v1/AUTH_foo/foo/a b.txt", 300, "GET")
    assert url == (
        "https://swift.example.org/v1/AUTH_foo/foo/a%20b.txt?temp_url_sig=abc&temp_url_expires=1"
    )


def test_copy_object():
    conn = fake_conn(Resp(201))
    swift.copy_object(conn, Entry("foo", "a.txt"), {"container": "bar", "name": "b.txt"})
    method, path, kwargs = calls(conn)[0]
    assert (method, path, kwargs["headers"]["X-Copy-From"]) == ("PUT", "/bar/b.txt", "/foo/a.txt")


def test_delete_folder_deletes_everything_under_it():
    conn = fake_conn(Resp(body=[{"name": "d/a"}, {"name": "d/b/c"}]), Resp(body=[]))
    out = swift.delete_entry(conn, Entry("foo", "d/", is_dir=True), {})
    deleted = sorted(c.args[0] for c in conn.object_store.delete_object.call_args_list)
    assert deleted == ["d/a", "d/b/c"]
    assert "2" in out


def test_actions_refuse_folders():
    with pytest.raises(ValueError):
        swift.object_details(MagicMock(), Entry("foo", "d/", is_dir=True), {})


def test_download_streams_and_never_overwrites(tmp_path):
    conn = MagicMock()
    conn.object_store.stream_object.return_value = iter([b"ab", b"c"])
    swift.download_object(conn, Entry("foo", "docs/a.txt"), {"path": str(tmp_path)})
    assert (tmp_path / "a.txt").read_bytes() == b"abc"
    with pytest.raises(FileExistsError):
        swift.download_object(conn, Entry("foo", "docs/a.txt"), {"path": str(tmp_path)})


def test_request_errors_keep_status_code():
    conn = fake_conn(Resp(403))
    with pytest.raises(Exception) as err:
        swift.list_entries(conn, {"container": "foo"})
    assert getattr(err.value, "status_code", None) == 403
