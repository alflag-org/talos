"""Retrieve current Proxmox configuration and normalize supported assets."""

import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from urllib.parse import quote

from .errors import AuthenticationError, SynchronizationError
from .http import request


@dataclass(frozen=True)
class Node:
    name: str

    @property
    def source_id(self):
        return f"node:{self.name}"


@dataclass(frozen=True)
class Guest:
    kind: str
    vmid: int
    name: str
    node: str
    vcpu: int | None
    memory_mb: int
    disk_mb: int | None

    @property
    def source_id(self):
        return f"{self.kind}:{self.vmid}"


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise SynchronizationError("source has an invalid asset name or identifier")
    return value.strip()


def positive(value):
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise SynchronizationError("source has an invalid resource quantity")
    return value


def properties(value):
    if not isinstance(value, str):
        raise SynchronizationError("source has invalid guest configuration")
    result = {}
    for entry in value.split(","):
        key, sep, val = entry.partition("=")
        key, val = (key, val) if sep else ("volume", key)
        if not key or not val or key in result:
            raise SynchronizationError("source has invalid guest configuration")
        result[key] = val
    return result


def _size(value):
    if not isinstance(value, str):
        raise SynchronizationError("data disk capacity is unavailable")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTPE]?)", value)
    if not match:
        raise SynchronizationError("data disk capacity is invalid")
    try:
        amount = Decimal(match[1]) * 1024 ** (
            " KMGTPE".index(match[2]) if match[2] else 0
        )
        if amount <= 0:
            raise ValueError
        return amount
    except (InvalidOperation, ValueError):
        raise SynchronizationError("data disk capacity is invalid") from None


def disk_mb(kind, config):
    total = Decimal(0)
    seen = set()
    for key, value in config.items():
        if kind == "qemu":
            if not re.fullmatch(r"(?:ide|sata|scsi|virtio)\d+", key):
                continue
        elif key != "rootfs" and not re.fullmatch(r"mp\d+", key):
            continue
        disk = properties(value)
        volume = disk.get("volume", disk.get("file", ""))
        if kind == "qemu":
            if disk.get("media") == "cdrom" or volume in {"none", "cdrom"}:
                continue
            if re.search(r"(?:^|[/:-])(?:vm-\d+-)?cloudinit(?:$|\.)", volume):
                continue
        elif volume.startswith("/"):
            continue
        if not volume or volume in seen:
            raise SynchronizationError("source has ambiguous data disk references")
        seen.add(volume)
        total += _size(disk.get("size"))
    return (
        positive(int((total / 1024**2).to_integral_value(rounding=ROUND_CEILING)))
        if total
        else None
    )


def normalize_guest(resource, config):
    if not isinstance(config, dict):
        raise SynchronizationError("API returned invalid guest configuration")
    kind = resource["type"]
    if kind == "qemu":
        cpu = positive(
            config.get(
                "vcpus",
                positive(config.get("cores", 1)) * positive(config.get("sockets", 1)),
            )
        )
        memory = config.get("memory", 512)
        if isinstance(memory, str):
            opts = properties(memory)
            memory = opts.get("current", opts.get("volume"))
            if not memory or not memory.isdecimal():
                raise SynchronizationError("source has invalid memory configuration")
            memory = int(memory)
        name = config.get("name", resource.get("name"))
    else:
        cpu = positive(config["cores"]) if "cores" in config else None
        memory = config.get("memory", 512)
        name = config.get("hostname", resource.get("name"))
    return Guest(
        kind,
        positive(resource["vmid"]),
        text(name),
        text(resource["node"]),
        cpu,
        positive(memory),
        disk_mb(kind, config),
    )


class Proxmox:
    def __init__(self, client, tls_server_name=None):
        self.client = client
        self.extensions = {"sni_hostname": tls_server_name} if tls_server_name else {}

    def _get(self, path, **kwargs):
        response = request(
            self.client,
            "GET",
            "/api2/json" + path,
            extensions=self.extensions,
            **kwargs,
        )
        if not isinstance(response, dict) or "data" not in response:
            raise SynchronizationError("API returned invalid Proxmox data")
        return response["data"]

    def assets(self):
        # Cluster listings silently omit guests outside the token's audit scope.
        permissions = self._get("/access/permissions", params={"path": "/"})
        if not isinstance(permissions, dict) or not all(
            permissions.get("/", {}).get(name) == 1
            for name in ("Sys.Audit", "VM.Audit")
        ):
            raise AuthenticationError(
                "Proxmox token requires cluster-wide Sys.Audit and VM.Audit"
            )
        resources = self._get("/cluster/resources")
        if not isinstance(resources, list):
            raise SynchronizationError("API returned invalid Proxmox resources")
        nodes, guests, seen = [], [], set()
        for resource in resources:
            if not isinstance(resource, dict) or not isinstance(
                resource.get("type"), str
            ):
                raise SynchronizationError("API returned invalid Proxmox resource")
            kind = resource["type"]
            if kind == "node":
                asset = Node(text(resource.get("node")))
            elif kind in {"qemu", "lxc"}:
                vmid = positive(resource.get("vmid"))
                node = text(resource.get("node"))
                config = self._get(
                    f"/nodes/{quote(node, safe='')}/{kind}/{vmid}/config",
                    params={"current": 1},
                )
                if not isinstance(config, dict):
                    raise SynchronizationError(
                        "API returned invalid guest configuration"
                    )
                if config.get("template", resource.get("template", 0)) == 1:
                    continue
                asset = normalize_guest(resource, config)
            else:
                continue
            if asset.source_id in seen:
                raise SynchronizationError(
                    "Proxmox returned duplicate external identity"
                )
            seen.add(asset.source_id)
            (nodes if kind == "node" else guests).append(asset)
        if not nodes or any(
            guest.node not in {node.name for node in nodes} for guest in guests
        ):
            raise SynchronizationError("Proxmox node references are incomplete")
        return sorted(nodes, key=lambda n: n.source_id), sorted(
            guests, key=lambda g: g.source_id
        )
