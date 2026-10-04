"""Only the Registry reads, creates, and patches needed by synchronization."""

from uuid import UUID

from .errors import ConfigurationError, SynchronizationError
from .http import request


def record_id(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, AttributeError):
        raise SynchronizationError(
            "Registry returned an invalid resource identifier"
        ) from None


class Registry:
    def __init__(self, client):
        self.client = client

    def list(self, resource, **filters):
        if resource not in {"locations", "devices", "virtual-machines"}:
            raise ValueError("unsupported Registry resource")
        items, seen, offset, expected_total = [], set(), 0, None
        while True:
            page = request(
                self.client,
                "GET",
                f"/api/v1/{resource}",
                params={**filters, "limit": 200, "offset": offset},
            )
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("items"), list)
                or type(page.get("total")) is not int
                or page["total"] < 0
                or len(page["items"]) > 200
            ):
                raise SynchronizationError("Registry returned an invalid list page")
            if expected_total is None:
                expected_total = page["total"]
            if page["total"] != expected_total:
                raise SynchronizationError(
                    "Registry changed during pagination; run again"
                )
            for item in page["items"]:
                if not isinstance(item, dict):
                    raise SynchronizationError("Registry returned an invalid resource")
                identifier = record_id(item.get("id"))
                if identifier in seen:
                    raise SynchronizationError(
                        "Registry pagination returned a duplicate resource"
                    )
                seen.add(identifier)
                items.append(item)
            offset += len(page["items"])
            if offset == expected_total:
                return items
            if not page["items"] or offset > expected_total or offset > 1_000_000:
                raise SynchronizationError("Registry pagination is incomplete")

    def location_id(self, slug):
        matches = [
            location
            for location in self.list("locations")
            if location.get("slug") == slug
        ]
        if len(matches) != 1:
            raise ConfigurationError(
                "registry.location must identify one existing Location"
            )
        return matches[0]["id"]

    def write(self, resource, fields, identifier=None):
        if resource not in {"devices", "virtual-machines"}:
            raise ValueError("unsupported Registry resource")
        path = f"/api/v1/{resource}"
        if identifier is not None:
            path += "/" + record_id(identifier)
        result = request(
            self.client, "PATCH" if identifier else "POST", path, json=fields
        )
        try:
            if not isinstance(result, dict):
                raise ValueError
            result_id = record_id(result.get("id"))
            if identifier is not None and result_id != identifier:
                raise ValueError
            if any(result.get(key) != value for key, value in fields.items()):
                raise ValueError
            return result_id
        except (ValueError, SynchronizationError):
            raise SynchronizationError(
                "Registry write response is invalid; observe current state before retrying"
            ) from None
