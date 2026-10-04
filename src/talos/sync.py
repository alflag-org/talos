"""Calculate an in-memory plan and apply only managed fields."""

from dataclasses import dataclass

from .errors import SynchronizationError
from .registry import record_id

STATES = ("CREATE", "UPDATE", "UNCHANGED", "MISSING")


@dataclass(frozen=True)
class Change:
    resource: str
    source_id: str
    state: str
    name: str
    fields: dict
    differences: dict
    identifier: str | None = None
    host_source_id: str | None = None


def _index(records, scope, resource):
    index = {}
    for record in records:
        if not isinstance(record, dict):
            raise SynchronizationError("Registry returned invalid assets")
        if record.get("source") != "proxmox" or record.get("source_scope") != scope:
            continue
        source_id = record.get("source_id")
        prefixes = ("node:",) if resource == "devices" else ("qemu:", "lxc:")
        if (
            not isinstance(source_id, str)
            or not source_id.startswith(prefixes)
            or not source_id.partition(":")[2]
        ):
            raise SynchronizationError(
                "Registry contains an unsupported Proxmox identity"
            )
        if source_id in index:
            raise SynchronizationError("Registry contains duplicate external identity")
        record_id(record.get("id"))
        if not isinstance(record.get("name"), str) or not record["name"].strip():
            raise SynchronizationError("Registry returned an invalid asset name")
        index[source_id] = record
    return index


def _change(resource, source_id, fields, current, host_source_id=None):
    differences = (
        {}
        if current is None
        else {
            key: (current.get(key), value)
            for key, value in fields.items()
            if current.get(key) != value
        }
    )
    state = "CREATE" if current is None else "UPDATE" if differences else "UNCHANGED"
    return Change(
        resource,
        source_id,
        state,
        fields["name"],
        fields,
        differences,
        current["id"] if current else None,
        host_source_id,
    )


def plan(nodes, guests, devices, virtual_machines, scope, location_id):
    device_index = _index(devices, scope, "devices")
    vm_index = _index(virtual_machines, scope, "virtual-machines")
    changes, source_seen = [], set()
    node_ids = {
        node.source_id: device_index[node.source_id]["id"]
        if node.source_id in device_index
        else None
        for node in nodes
    }
    for node in nodes:
        if node.source_id in source_seen:
            raise SynchronizationError("source contains duplicate external identity")
        source_seen.add(node.source_id)
        fields = dict(
            name=node.name,
            role="hypervisor",
            location_id=location_id,
            source="proxmox",
            source_scope=scope,
            source_id=node.source_id,
        )
        changes.append(
            _change("devices", node.source_id, fields, device_index.get(node.source_id))
        )
    for guest in guests:
        if guest.source_id in source_seen or f"node:{guest.node}" not in node_ids:
            raise SynchronizationError(
                "source identities or node references are ambiguous"
            )
        source_seen.add(guest.source_id)
        host_source_id = f"node:{guest.node}"
        # A source identity represents an uncreated Device in the displayed plan.
        host_id = node_ids[host_source_id] or host_source_id
        fields = dict(
            name=guest.name,
            host_device_id=host_id,
            vcpu=guest.vcpu,
            memory_mb=guest.memory_mb,
            disk_mb=guest.disk_mb,
            source="proxmox",
            source_scope=scope,
            source_id=guest.source_id,
        )
        changes.append(
            _change(
                "virtual-machines",
                guest.source_id,
                fields,
                vm_index.get(guest.source_id),
                host_source_id,
            )
        )
    for resource, index in (("devices", device_index), ("virtual-machines", vm_index)):
        for source_id, current in index.items():
            if source_id not in source_seen:
                changes.append(
                    Change(
                        resource,
                        source_id,
                        "MISSING",
                        current["name"],
                        {},
                        {},
                        current["id"],
                    )
                )
    return sorted(
        changes, key=lambda change: (STATES.index(change.state), change.source_id)
    )


def display_plan(changes, emit):
    emit("Global Registry sync")
    for state in STATES:
        emit(f"\n{state}")
        for change in changes:
            if change.state != state:
                continue
            emit(f"  {change.source_id}  {change.name}")
            for key, (old, new) in change.differences.items():
                emit(f"    {key}: {old} -> {new}")
    emit("\nSummary:")
    emit(
        "  "
        + " ".join(
            f"{state.lower()}={sum(change.state == state for change in changes)}"
            for state in STATES
        )
    )
    if any(change.state == "MISSING" for change in changes):
        emit("Warning: MISSING assets are retained; no deletion is performed.")


def apply(changes, registry, emit):
    created, updated = 0, 0
    host_ids = {
        change.source_id: change.identifier
        for change in changes
        if change.resource == "devices" and change.identifier
    }
    ordered = sorted(
        changes, key=lambda change: (change.resource != "devices", change.source_id)
    )
    try:
        for change in ordered:
            if change.state not in {"CREATE", "UPDATE"}:
                continue
            fields = (
                dict(change.fields)
                if change.state == "CREATE"
                else {key: new for key, (_, new) in change.differences.items()}
            )
            if change.resource == "virtual-machines" and "host_device_id" in fields:
                if change.host_source_id not in host_ids:
                    raise SynchronizationError("host Device was not created")
                fields["host_device_id"] = host_ids[change.host_source_id]
            identifier = registry.write(change.resource, fields, change.identifier)
            if change.resource == "devices":
                host_ids[change.source_id] = identifier
            if change.state == "CREATE":
                created += 1
            else:
                updated += 1
    except Exception:
        emit(f"\nApplying:\n  created={created} updated={updated} failed=1")
        raise
    emit(f"\nApplying:\n  created={created} updated={updated} failed=0")
