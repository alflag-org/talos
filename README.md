# Talos

Operator-triggered infrastructure commands executed through Atlas. Host configuration
and deployment belong to Provisioning.

## Registry synchronization

`registry-sync` compares Proxmox nodes and non-template QEMU/LXC guests with Global
Registry. It matches external identities, so guest renames and moves update the
same asset. LXC guests use Registry's Virtual Machine resource.

```sh
atlas run registry-sync
registry-sync --config /etc/talos/config.yml
registry-sync --apply
```

The default only reads APIs and displays CREATE, UPDATE, UNCHANGED, and MISSING.
`--apply` creates and updates managed fields, preserving operator descriptions and
other unmanaged data. MISSING records are retained. There is no deletion option.
Each execution fetches current state; a displayed plan is not a saved apply artifact.

## Set up

Provisioning owns the checkout, dedicated Atlas Python venv, program registration,
shims, and non-secret configuration. Install this package in the venv. Atlas
discovers `commands/registry/sync.py`; execution records remain owned by Atlas.

Configuration defaults to `/etc/talos/config.yml`:

```yaml
proxmox:
  url: https://pve.example.internal:8006
  cluster: example-cluster
  tls_server_name: pve.example.org
registry:
  url: https://registry.example.org
  location: existing-location-slug
```

Choose `registry.location` from the authenticated `/api/v1/locations` response;
the command never creates a Location. Keep `cluster` stable: it is the identity
scope, not a hostname. When an internal alias differs from the certificate name,
`tls_server_name` selects the TLS SNI and certificate verification name while the
URL remains the connection destination. Omit it when the names match. CA and
hostname verification always remain enabled.

Use Atlas's external secret provider with these logical names:

```text
proxmox.api_token_id
proxmox.api_token_secret
global_registry.access_client_id
global_registry.access_client_secret
```

The Proxmox token needs cluster-wide `Sys.Audit` and `VM.Audit` permissions, including
all guests. Registry credentials are a Cloudflare Access Service Token permitted
by a Service Auth policy. Host-side secret mappings and bootstrap credentials stay
outside this repository. Resolve the names with `atlas secret check` without
displaying values. Missing configuration or secrets stop execution before writes.

Resource quantities come from current guest configuration, without pending changes.
Disk capacity is the sum of attached QEMU data disks or LXC managed volumes, rounded
up to MiB. CD-ROM, Cloud-Init, EFI/TPM, unused disks, and LXC bind/device mounts are
excluded. Unknown data disk sizes fail the execution. Diskless guests and LXC guests
without a core limit use `null` for those fields.

Exit status is `0` for a completed comparison/application (including differences
or MISSING), `1` for API/synchronization failure, and `2` for configuration, input,
or authentication errors. Application stops on its first error and reports completed
writes. After a timeout or invalid write response, the outcome can be unknown:
inspect a fresh read-only comparison before requesting another application.

## Development

```sh
mise run setup
mise run check
```

CI runs the same Ruff, pytest, and package-build checks. Tests use synthetic data
and local transports; they do not prove live API access or synchronization.
