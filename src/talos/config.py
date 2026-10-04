"""Load non-secret endpoint and synchronization settings."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .errors import ConfigurationError


class ConfigLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than silently replacing settings."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConfigurationError("duplicate configuration key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


ConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class Config:
    proxmox_url: str
    scope: str
    tls_server_name: str | None
    registry_url: str
    location: str


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ConfigurationError("configuration requires a non-empty string")
    return value.strip()


def _url(value):
    value = _text(value)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or parsed.port == 0
        ):
            raise ValueError
    except ValueError:
        raise ConfigurationError(
            "API URL must be an HTTPS origin without credentials"
        ) from None
    return value.rstrip("/")


def load_config(path: Path) -> Config:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=ConfigLoader)
        if not isinstance(raw, dict) or set(raw) != {"proxmox", "registry"}:
            raise ConfigurationError(
                "configuration requires proxmox and registry sections"
            )
        pve, registry = raw["proxmox"], raw["registry"]
        if not isinstance(pve, dict) or not isinstance(registry, dict):
            raise ConfigurationError("invalid configuration section")
        if not {"url", "cluster"} <= set(pve) or set(pve) - {
            "url",
            "cluster",
            "tls_server_name",
        }:
            raise ConfigurationError("invalid proxmox configuration keys")
        if not {"url", "location"} <= set(registry):
            raise ConfigurationError(
                "registry.location must select a verified existing Location"
            )
        if set(registry) - {"url", "location"}:
            raise ConfigurationError("invalid registry configuration keys")
        location = _text(registry["location"])
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", location):
            raise ConfigurationError("registry.location must be a Location slug")
        server_name = pve.get("tls_server_name")
        if server_name is not None:
            server_name = _text(server_name)
            if not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", server_name
            ):
                raise ConfigurationError("invalid TLS server name")
        return Config(
            _url(pve["url"]),
            _text(pve["cluster"]),
            server_name,
            _url(registry["url"]),
            location,
        )
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("configuration could not be read") from None
