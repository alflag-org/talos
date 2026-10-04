"""The single operator command; all network and secret reads happen after parsing."""

import argparse
import sys
from pathlib import Path

from atlas_core.secrets import (
    SecretConfigurationError,
    SecretResolutionError,
    load_provider,
)

from .config import load_config
from .errors import AuthenticationError, ConfigurationError, SynchronizationError
from .http import client
from .proxmox import Proxmox
from .registry import Registry
from .sync import apply, display_plan, plan

SECRET_NAMES = (
    "proxmox.api_token_id",
    "proxmox.api_token_secret",
    "global_registry.access_client_id",
    "global_registry.access_client_secret",
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="registry-sync",
        description="Compare Proxmox assets with Global Registry; read-only by default.",
    )
    parser.add_argument("--config", type=Path, default=Path("/etc/talos/config.yml"))
    parser.add_argument(
        "--apply", action="store_true", help="Create and update assets; never delete."
    )
    args = parser.parse_args(argv)
    values = {}

    def emit(message, file=None):
        for value in sorted(values.values(), key=len, reverse=True):
            if value:
                message = message.replace(value, "[REDACTED]")
        print(message, file=file)

    try:
        config = load_config(args.config)
        retrieved = load_provider().get_many(list(SECRET_NAMES))
        if (
            not isinstance(retrieved, dict)
            or set(retrieved) != set(SECRET_NAMES)
            or any(
                not isinstance(value, str)
                or not value
                or "\r" in value
                or "\n" in value
                for value in retrieved.values()
            )
        ):
            raise AuthenticationError("required credentials could not be resolved")
        values = retrieved
        pve_headers = {
            "Authorization": f"PVEAPIToken={values[SECRET_NAMES[0]]}={values[SECRET_NAMES[1]]}"
        }
        registry_headers = {
            "CF-Access-Client-Id": values[SECRET_NAMES[2]],
            "CF-Access-Client-Secret": values[SECRET_NAMES[3]],
        }
        with (
            client(config.proxmox_url, pve_headers) as pve_http,
            client(config.registry_url, registry_headers) as registry_http,
        ):
            registry = Registry(registry_http)
            location_id = registry.location_id(config.location)
            nodes, guests = Proxmox(pve_http, config.tls_server_name).assets()
            filters = {"source": "proxmox", "source_scope": config.scope}
            changes = plan(
                nodes,
                guests,
                registry.list("devices", **filters),
                registry.list("virtual-machines", **filters),
                config.scope,
                location_id,
            )
            display_plan(changes, emit)
            if args.apply:
                apply(changes, registry, emit)
        return 0
    except (SecretConfigurationError, SecretResolutionError):
        emit("Error: required Atlas secrets are unavailable", file=sys.stderr)
        return 2
    except (ConfigurationError, AuthenticationError) as error:
        emit(f"Error: {error}", file=sys.stderr)
        return 2
    except SynchronizationError as error:
        emit(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception:
        emit("Error: synchronization failed", file=sys.stderr)
        return 1
