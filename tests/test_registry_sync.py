import json
from uuid import UUID

import httpx
import pytest
import yaml
from atlas_core.secrets import SecretResolutionError

from talos import cli
from talos.errors import SynchronizationError
from talos.http import client
from talos.proxmox import disk_mb, normalize_guest
from talos.registry import Registry
from talos.sync import plan


def writes(environment):
    return [request for request in environment.requests if request.method != "GET"]


def test_read_only_plan_normalizes_guests_and_excludes_templates(environment, capsys):
    assert environment.run() == 0
    output = capsys.readouterr().out
    assert "create=3 update=0 unchanged=0 missing=0" in output
    assert "qemu:900" not in output
    assert not writes(environment)
    qemu = normalize_guest(
        environment.source["resources"][1], environment.source["configs"]["qemu:101"]
    )
    lxc = normalize_guest(
        environment.source["resources"][2], environment.source["configs"]["lxc:102"]
    )
    assert (qemu.vcpu, qemu.memory_mb, qemu.disk_mb) == (4, 4096, 8705)
    assert (lxc.vcpu, lxc.memory_mb, lxc.disk_mb) == (1, 1024, 5120)


def test_apply_creates_devices_before_guests_and_converges(environment, capsys):
    assert environment.run("--apply") == 0
    created = writes(environment)
    assert [request.url.path for request in created] == [
        "/api/v1/devices",
        "/api/v1/virtual-machines",
        "/api/v1/virtual-machines",
    ]
    device = environment.records["devices"][0]
    assert all(
        vm["host_device_id"] == device["id"]
        for vm in environment.records["virtual-machines"]
    )
    assert {vm["source_id"] for vm in environment.records["virtual-machines"]} == {
        "qemu:101",
        "lxc:102",
    }
    environment.requests.clear()
    assert environment.run("--apply") == 0
    assert not writes(environment)
    assert "create=0 update=0 unchanged=3 missing=0" in capsys.readouterr().out


def test_rename_updates_only_managed_changes_and_missing_is_retained(
    environment, capsys
):
    assert environment.run("--apply") == 0
    vm = next(
        record
        for record in environment.records["virtual-machines"]
        if record["source_id"] == "qemu:101"
    )
    vm.update(name="old-name", memory_mb=2048, description="Keep operator notes")
    stale = {**vm, "id": str(UUID(int=900)), "source_id": "qemu:999", "name": "retired"}
    environment.records["virtual-machines"].append(stale)
    original = environment.snapshot()
    environment.requests.clear()
    assert environment.run() == 0
    assert environment.records == original
    output = capsys.readouterr().out
    assert "update=1 unchanged=2 missing=1" in output
    assert "name: old-name -> app-a" in output
    assert "memory_mb: 2048 -> 4096" in output
    assert not writes(environment)
    assert environment.run("--apply") == 0
    patches = writes(environment)
    assert len(patches) == 1 and patches[0].method == "PATCH"
    assert json.loads(patches[0].content) == {"name": "app-a", "memory_mb": 4096}
    assert vm["description"] == "Keep operator notes"
    assert stale in environment.records["virtual-machines"]


def test_guest_migration_references_new_node_uuid(environment):
    assert environment.run("--apply") == 0
    environment.source["resources"].append({"type": "node", "node": "pve-b"})
    environment.source["resources"][1]["node"] = "pve-b"
    environment.requests.clear()
    assert environment.run("--apply") == 0
    node = next(
        record for record in environment.records["devices"] if record["name"] == "pve-b"
    )
    vm = next(
        record
        for record in environment.records["virtual-machines"]
        if record["source_id"] == "qemu:101"
    )
    assert vm["host_device_id"] == node["id"]
    assert [request.method for request in writes(environment)] == ["POST", "PATCH"]


def test_other_scope_and_unowned_records_are_not_adopted(environment):
    environment.records["devices"] = [
        {"id": str(UUID(int=2)), "name": "pve-a", "source": None},
        {
            "id": str(UUID(int=3)),
            "name": "pve-a",
            "source": "proxmox",
            "source_scope": "other",
            "source_id": "node:pve-a",
        },
    ]
    original = environment.snapshot()
    assert environment.run("--apply") == 0
    assert environment.records["devices"][:2] == original["devices"]
    assert len(environment.records["devices"]) == 3


@pytest.mark.parametrize("damage", ["disk", "node", "duplicate"])
def test_invalid_source_fails_before_writes(environment, damage):
    if damage == "disk":
        environment.source["configs"]["qemu:101"]["virtio0"] = "local:vm-101-disk-0"
    elif damage == "node":
        environment.source["resources"][1]["node"] = "unknown"
    else:
        environment.source["resources"].append(environment.source["resources"][0])
    assert environment.run("--apply") == 1
    assert not writes(environment)


def test_duplicate_registry_identity_is_rejected(environment):
    assert environment.run("--apply") == 0
    environment.records["devices"].append(
        {**environment.records["devices"][0], "id": str(UUID(int=900))}
    )
    environment.requests.clear()
    assert environment.run("--apply") == 1
    assert not writes(environment)


def test_registry_reads_all_pages(environment, capsys):
    environment.records["devices"] = [
        {
            "id": str(UUID(int=2000 + i)),
            "name": f"old-{i}",
            "source": "proxmox",
            "source_scope": "demo",
            "source_id": f"node:old-{i}",
        }
        for i in range(201)
    ]
    assert environment.run() == 0
    pages = [
        request
        for request in environment.requests
        if request.url.path == "/api/v1/devices"
    ]
    assert [request.url.params["offset"] for request in pages] == ["0", "200"]
    assert "missing=201" in capsys.readouterr().out
    assert not writes(environment)


@pytest.mark.parametrize("status", [401, 403, 302])
def test_authentication_and_redirects_fail_without_following_or_writes(
    environment, status, capsys
):
    secret = environment.values[cli.SECRET_NAMES[0]]
    environment.failure = lambda request: httpx.Response(
        status,
        json={"message": secret},
        headers={"Location": "https://untrusted.example/"},
    )
    assert environment.run("--apply") == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert len(environment.requests) == 1
    assert not writes(environment)


@pytest.mark.parametrize("failure", ["tls", "read", "json", "body"])
def test_source_failures_are_sanitized_and_never_become_missing(
    environment, failure, capsys, caplog
):
    secret = environment.values[cli.SECRET_NAMES[1]]

    def fail(request):
        if "/lxc/102/config" not in request.url.path:
            return None
        if failure == "tls":
            raise httpx.ConnectError(secret)
        if failure == "read":
            raise httpx.ReadTimeout(secret)
        if failure == "json":
            return httpx.Response(200, text=secret)
        return httpx.Response(500, text=secret)

    environment.failure = fail
    assert environment.run("--apply") == 1
    output = capsys.readouterr()
    assert "MISSING" not in output.out
    assert secret not in output.out + output.err + caplog.text
    assert not writes(environment)


def test_secret_resolution_failure_prevents_any_api_call(environment, capsys):
    def fail(names):
        raise SecretResolutionError(environment.values[cli.SECRET_NAMES[0]])

    environment.provider.get_many = fail
    assert environment.run("--apply") == 2
    assert not environment.requests
    assert environment.values[cli.SECRET_NAMES[0]] not in capsys.readouterr().err


def test_unknown_write_outcome_stops_and_is_observed_on_next_run(environment, capsys):
    def fail(request):
        if request.method == "POST":
            environment.records["devices"].append(
                {"id": str(UUID(int=42)), **json.loads(request.content)}
            )
            raise httpx.ReadTimeout(environment.values[cli.SECRET_NAMES[1]])
        return None

    environment.failure = fail
    assert environment.run("--apply") == 1
    assert len(writes(environment)) == 1
    assert "write outcome unknown" in capsys.readouterr().err
    environment.failure = None
    environment.requests.clear()
    assert environment.run() == 0
    assert "create=2 update=0 unchanged=1" in capsys.readouterr().out
    assert not writes(environment)


def test_partial_apply_reports_success_count_and_stops(environment, capsys):
    def fail(request):
        if request.method == "POST" and request.url.path.endswith("virtual-machines"):
            return httpx.Response(
                409, json={"message": environment.values[cli.SECRET_NAMES[1]]}
            )
        return None

    environment.failure = fail
    assert environment.run("--apply") == 1
    assert len(writes(environment)) == 2
    output = capsys.readouterr()
    assert "created=1 updated=0 failed=1" in output.out
    assert environment.values[cli.SECRET_NAMES[1]] not in output.err


def test_known_secrets_in_asset_output_are_redacted(environment, capsys):
    environment.source["configs"]["qemu:101"]["name"] = environment.values[
        cli.SECRET_NAMES[0]
    ]
    assert environment.run() == 0
    output = capsys.readouterr()
    assert "[REDACTED]" in output.out
    assert not any(
        value in output.out + output.err for value in environment.values.values()
    )


def test_missing_location_fails_before_secrets_or_network(environment):
    del environment.config["registry"]["location"]
    environment.path.write_text(yaml.safe_dump(environment.config))
    assert environment.run() == 2
    assert not environment.requests


def test_unknown_location_does_not_create_it(environment):
    environment.config["registry"]["location"] = "unknown"
    environment.path.write_text(yaml.safe_dump(environment.config))
    assert environment.run("--apply") == 2
    assert len(environment.requests) == 1
    assert not writes(environment)


@pytest.mark.parametrize(
    "config",
    [
        "registry: {}\nregistry: {}",
        "proxmox: {url: 'https://user:secret@example.org'}\nregistry: {}",
        "- wrong",
    ],
)
def test_invalid_configuration_is_not_reflected(environment, config, capsys):
    environment.path.write_text(config)
    assert environment.run() == 2
    assert not environment.requests
    assert "user:secret" not in capsys.readouterr().err


def test_help_needs_no_configuration_secrets_or_network(environment):
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0
    assert not environment.requests


@pytest.mark.parametrize(
    "pages",
    [
        [{"items": [], "total": 1}],
        [{"items": [{"id": str(UUID(int=1))}], "total": 2}, {"items": [], "total": 2}],
        [
            {"items": [{"id": str(UUID(int=1))}], "total": 2},
            {"items": [{"id": str(UUID(int=1))}], "total": 2},
        ],
        [{"items": [{"id": str(UUID(int=1))}], "total": 2}, {"items": [], "total": 3}],
    ],
)
def test_incomplete_or_changing_pages_are_rejected(pages):
    with (
        httpx.Client(
            base_url="https://registry.example",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=pages.pop(0))
            ),
        ) as transport,
        pytest.raises(SynchronizationError),
    ):
        Registry(transport).list("devices")


def test_diskless_and_unlimited_lxc_and_qemu_hotplug(environment):
    resource = environment.source["resources"][2]
    guest = normalize_guest(resource, {"hostname": "diskless", "memory": 512})
    assert guest.vcpu is None and guest.disk_mb is None
    guest = normalize_guest(
        environment.source["resources"][1],
        {
            "name": "hotplug",
            "cores": 4,
            "sockets": 2,
            "vcpus": 3,
            "memory": "current=8192,max=16384",
        },
    )
    assert (guest.vcpu, guest.memory_mb) == (3, 8192)
    assert disk_mb("qemu", {"scsi0": "local:vm-101-cloudinit,size=4M"}) is None


def test_foreign_scope_records_are_not_classified_as_missing():
    assert plan([], [], [{"source": "other"}], [], "demo", str(UUID(int=1))) == []


def test_restricted_proxmox_token_cannot_produce_a_partial_plan(environment):
    def fail(request):
        if request.url.path.endswith("access/permissions"):
            return httpx.Response(200, json={"data": {"/": {"Sys.Audit": 1}}})
        return None

    environment.failure = fail
    assert environment.run("--apply") == 2
    assert not writes(environment)


def test_tls_server_name_verifies_certificate_and_rejects_wrong_name(
    tmp_path, monkeypatch
):
    import ssl
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from talos.proxmox import Proxmox

    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=api.example.test",
            "-addext",
            "subjectAltName=DNS:api.example.test",
        ],
        check=True,
        capture_output=True,
    )
    trust = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: trust)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = (
                {"/": {"Sys.Audit": 1, "VM.Audit": 1}}
                if "permissions" in self.path
                else [{"type": "node", "node": "pve-a"}]
            )
            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with client(f"https://127.0.0.1:{server.server_port}", {}) as transport:
                nodes, guests = Proxmox(transport, "api.example.test").assets()
                assert [node.name for node in nodes] == ["pve-a"] and not guests
                with pytest.raises(SynchronizationError):
                    Proxmox(transport, "wrong.example.test").assets()
                assert not transport.follow_redirects
                assert transport.timeout.connect == 10
        finally:
            server.shutdown()
            thread.join(timeout=5)
