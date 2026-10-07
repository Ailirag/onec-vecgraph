"""The real MCP dispatcher must never infer a data request's workspace."""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest

from onec_vecgraph.lite import admin, server
from test_lite_workspaces import _repo


@pytest.fixture()
def strict_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEC_LITE_PROFILE", "full")
    importlib.reload(server)
    monkeypatch.setenv("ONEC_LITE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("ONEC_LITE_FTS_DIR", str(tmp_path / "fts"))
    monkeypatch.setenv("ONEC_LITE_FTS_AUTOBUILD", "off")
    monkeypatch.setenv("ONEC_LITE_WORKSPACE", "a")
    monkeypatch.setenv("ONEC_LITE_ROOT", str(tmp_path / "a"))
    monkeypatch.setattr(server, "_WORKSPACES", {})
    monkeypatch.setattr(server, "_HELPS", {})
    a = _repo(tmp_path / "a", "OnlyA", "MethodA", "aa")
    admin.upsert_workspace(admin.state_file(), "a", str(a), [])
    yield server
    monkeypatch.setenv("ONEC_LITE_PROFILE", "lean")
    importlib.reload(server)


def call(s, name, arguments):
    return asyncio.run(s.mcp.call_tool(name, arguments))


def test_dispatcher_rejects_omitted_workspace_before_loading_singleton(strict_server):
    s = strict_server
    with pytest.raises(Exception, match="workspace"):
        call(s, "overview", {})
    assert not s._WORKSPACES


def required_arguments(tool):
    return {name: "test" for name in tool.inputSchema.get("required", [])
            if name != "workspace"}


@pytest.mark.parametrize("workspace", [None, 7, True, [], {}, "", " \t\n", "unknown"])
def test_every_tool_rejects_invalid_workspace_without_effects(strict_server, tmp_path, workspace):
    s = strict_server
    before = {p.relative_to(tmp_path): p.read_bytes()
              for p in tmp_path.rglob("*") if p.is_file()}
    for tool in asyncio.run(s.mcp.list_tools()):
        if tool.name == "list_workspaces":
            continue
        args = required_arguments(tool)
        args["workspace"] = workspace
        with pytest.raises(Exception, match="workspace|Воркспейс"):
            call(s, tool.name, args)
        assert s._WORKSPACES == {}, tool.name
        assert s._HELPS == {}, tool.name
        assert s._UPDATE_RESULTS == {}, tool.name
    after = {p.relative_to(tmp_path): p.read_bytes()
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_all_tools_require_workspace_in_schema(strict_server):
    for tool in asyncio.run(strict_server.mcp.list_tools()):
        if tool.name != "list_workspaces":
            assert "workspace" in tool.inputSchema["required"], tool.name
            field = tool.inputSchema["properties"]["workspace"]
            assert field["type"] == "string"
            assert field["minLength"] == 1
            assert field["pattern"] == r"\S"


def decode(result):
    return json.loads(result[0].text)


def test_discovery_has_no_mcp_default(strict_server):
    result = decode(call(strict_server, "list_workspaces", {}))
    assert result["default_workspace"] == ""
    assert "a" in {w["name"] for w in result["workspaces"]}


@pytest.mark.parametrize("profile,count", [("lean", 23), ("full", 32), ("review", 11)])
def test_profiles_publish_same_tools_with_required_selection(strict_server, monkeypatch, profile, count):
    monkeypatch.setenv("ONEC_LITE_PROFILE", profile)
    importlib.reload(strict_server)
    tools = asyncio.run(strict_server.mcp.list_tools())
    assert len(tools) == count
    assert "list_workspaces" in {t.name for t in tools}
    for tool in tools:
        if tool.name != "list_workspaces":
            assert "workspace" in tool.inputSchema["required"]


def http_rpc(client, method, params):
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    })
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        return json.loads(next(line[6:] for line in response.text.splitlines()
                               if line.startswith("data: ")))
    return response.json()


def test_real_http_dispatcher_rejects_every_tool_before_effects(strict_server, monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    s = strict_server
    monkeypatch.setenv("ONEC_LITE_WORKSPACE_HEADER", "X-Proj")
    # Reject before any lazy workspace, git update, help catalog or FTS access.
    effects = []

    def forbidden(*args, **kwargs):
        effects.append((args, kwargs))
        raise AssertionError("data access before workspace rejection")

    monkeypatch.setattr(s, "configure", forbidden)
    monkeypatch.setattr(s, "_maybe_update_on_start", forbidden)
    monkeypatch.setattr(s.fts, "index_for", forbidden)
    monkeypatch.setattr(s.platform_help.HelpCatalog, "configure", forbidden)
    monkeypatch.setattr(s.platform_help.HelpCatalog, "index", forbidden)
    before = {p.relative_to(tmp_path): p.read_bytes()
              for p in tmp_path.rglob("*") if p.is_file()}
    with TestClient(s.mcp.streamable_http_app(), base_url="http://127.0.0.1:8010",
                    headers={"Accept": "application/json, text/event-stream",
                             "X-Workspace": "a", "X-Tenant-Id": "a", "X-Proj": "a"}) as client:
        init = http_rpc(client, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "strict-workspace-test", "version": "1"},
        })
        assert init["result"]["serverInfo"]["name"] == "onec-lite"
        tools = http_rpc(client, "tools/list", {})["result"]["tools"]
        assert len(tools) == 32
        discovery = http_rpc(client, "tools/call", {"name": "list_workspaces", "arguments": {}})
        assert not discovery["result"].get("isError", False)
        for tool in tools:
            if tool["name"] == "list_workspaces":
                continue
            base = {name: "test" for name in tool["inputSchema"].get("required", [])
                    if name != "workspace"}
            for selection in [{}, *({"workspace": value} for value in
                                   [None, 7, True, [], {}, "", " \t\n", "unknown"])]:
                result = http_rpc(client, "tools/call", {
                    "name": tool["name"], "arguments": base | selection,
                })["result"]
                assert result["isError"], (tool["name"], selection, result)
                assert "workspace" in str(result), result
        assert not effects
        assert s._WORKSPACES == {} and s._HELPS == {}
    after = {p.relative_to(tmp_path): p.read_bytes()
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_http_explicit_workspace_overrides_headers_env_and_active(strict_server, tmp_path):
    from starlette.testclient import TestClient

    s = strict_server
    b = _repo(tmp_path / "b", "OnlyB", "MethodB", "bb")
    admin.upsert_workspace(admin.state_file(), "b", str(b), [])
    with TestClient(s.mcp.streamable_http_app(), base_url="http://127.0.0.1:8010",
                    headers={"Accept": "application/json, text/event-stream",
                             "X-Workspace": "a", "X-Tenant-Id": "a"}) as client:
        for name, expected in [("b", "OnlyB"), ("a", "OnlyA"), ("b", "OnlyB")]:
            result = http_rpc(client, "tools/call", {"name": "list_objects", "arguments": {
                "workspace": name, "kind": "Catalog",
            }})["result"]
            assert not result.get("isError", False), result
            data = json.loads(result["content"][0]["text"])
            assert [o["name"] for o in data["objects"]] == [expected]


def test_help_workspaces_are_isolated_without_loading_code(strict_server, monkeypatch, tmp_path):
    from test_lite_help import _RESOLVED, _fake_named_elements

    s = strict_server
    monkeypatch.setattr(s.platform_help, "_resolve_files", lambda e: _RESOLVED[str(e.get("path"))])
    monkeypatch.setattr(s.platform_help.hbk_container, "named_elements", _fake_named_elements)
    for name, help_path in [("new", "v27"), ("old", "v18")]:
        admin.upsert_workspace(admin.state_file(), name, str(tmp_path / name), [],
                               platform_help=[{"version": "", "path": help_path}])
    for name, version in [("old", "8.3.18.1289"), ("new", "8.3.27.2130")]:
        data = decode(call(s, "platform_versions", {"workspace": name}))
        assert [v["platform_version"] for v in data["versions"]] == [version]
    assert s._WORKSPACES == {}
    with pytest.raises(Exception, match="workspace"):
        call(s, "platform_docinfo", {"name": "Массив.Найти", "workspace": "unknown"})
    assert set(s._HELPS) == {"old", "new"}


def test_python_admin_defaults_remain_separate(strict_server):
    assert strict_server.admin_default_workspace() == "a"
    assert strict_server.overview()["workspace"] == "a"
    with pytest.raises(Exception, match="workspace"):
        call(strict_server, "overview", {})
