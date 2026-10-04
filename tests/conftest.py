import copy
import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import yaml

from talos import cli


@pytest.fixture
def environment(tmp_path, monkeypatch):
    source = json.loads((Path(__file__).parent / "fixtures/proxmox.json").read_text())
    state = SimpleNamespace(
        source=source,
        records={
            "locations": [{"id": str(UUID(int=1)), "name": "Lab", "slug": "lab"}],
            "devices": [],
            "virtual-machines": [],
        },
        requests=[],
        values={name: secrets.token_hex(20) for name in cli.SECRET_NAMES},
        failure=None,
    )
    config = {
        "proxmox": {
            "url": "https://pve.example.internal:8006",
            "cluster": "demo",
            "tls_server_name": "pve.example.org",
        },
        "registry": {"url": "https://registry.example.org", "location": "lab"},
    }
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(config))
    state.path, state.config = path, config

    def get_many(names):
        assert names == list(cli.SECRET_NAMES)
        return state.values

    state.provider = SimpleNamespace(get_many=get_many)
    monkeypatch.setattr(cli, "load_provider", lambda: state.provider)

    def handler(request):
        state.requests.append(request)
        if state.failure:
            result = state.failure(request)
            if result is not None:
                return result
        route = request.url.path
        if request.url.host == "pve.example.internal":
            assert (
                request.headers["Authorization"]
                == f"PVEAPIToken={state.values[cli.SECRET_NAMES[0]]}={state.values[cli.SECRET_NAMES[1]]}"
            )
            assert request.extensions["sni_hostname"] == "pve.example.org"
            assert request.method == "GET"
            if route == "/api2/json/access/permissions":
                return httpx.Response(
                    200, json={"data": {"/": {"Sys.Audit": 1, "VM.Audit": 1}}}
                )
            if route == "/api2/json/cluster/resources":
                return httpx.Response(200, json={"data": state.source["resources"]})
            kind, vmid = route.split("/")[-3:-1]
            assert request.url.params["current"] == "1"
            return httpx.Response(
                200, json={"data": state.source["configs"][f"{kind}:{vmid}"]}
            )
        assert request.url.host == "registry.example.org"
        assert (
            request.headers["CF-Access-Client-Id"] == state.values[cli.SECRET_NAMES[2]]
        )
        assert (
            request.headers["CF-Access-Client-Secret"]
            == state.values[cli.SECRET_NAMES[3]]
        )
        resource = route.split("/")[3]
        records = state.records[resource]
        if request.method == "GET":
            filtered = [
                record
                for record in records
                if all(
                    record.get(key) == value
                    for key, value in request.url.params.items()
                    if key not in {"limit", "offset"}
                )
            ]
            offset, limit = (
                int(request.url.params["offset"]),
                int(request.url.params["limit"]),
            )
            return httpx.Response(
                200,
                json={
                    "items": filtered[offset : offset + limit],
                    "total": len(filtered),
                },
            )
        fields = json.loads(request.content)
        if request.method == "POST":
            record = {
                "id": str(UUID(int=100 + sum(map(len, state.records.values())))),
                **fields,
            }
            records.append(record)
            return httpx.Response(201, json=record)
        assert request.method == "PATCH"
        record = next(item for item in records if item["id"] == route.split("/")[-1])
        record.update(fields)
        return httpx.Response(200, json=record)

    state.handler = handler

    def client(url, headers):
        return httpx.Client(
            base_url=url,
            headers=headers,
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )

    monkeypatch.setattr(cli, "client", client)

    def run(*arguments):
        return cli.main(["--config", str(path), *arguments])

    state.run = run
    state.snapshot = lambda: copy.deepcopy(state.records)
    return state
