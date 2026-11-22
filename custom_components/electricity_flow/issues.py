"""Repair issues (Settings → System → Repairs) for configuration problems.

The integration keeps running when something is wrong with a source or price
entity, but the sensors then quietly stay at 0 or costs are not booked. These
issues tell the user what is wrong and where to fix it. Every issue clears
itself as soon as the problem is gone.

Issues:
- duplicate_source: one sensor is selected for more than one meter role.
- source_unavailable: a source meter is missing, unavailable or not a number
  for longer than UNUSABLE_GRACE.
- source_invalid_unit: a source meter is not in Wh, kWh or MWh.
- price_unavailable: a price entity or template gave no usable price for
  longer than UNUSABLE_GRACE (the last valid price is used meanwhile).
- price_invalid_unit: a price entity is not a price per kWh/MWh/Wh.
- price_currency: a price entity is in another currency than Home Assistant.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

# Short outages (a restart of the source integration, a P1 reader that
# reconnects) are normal and must not raise an issue.
UNUSABLE_GRACE = timedelta(hours=1)

ISSUE_DUPLICATE_SOURCE = "duplicate_source"
ISSUE_SOURCE_UNAVAILABLE = "source_unavailable"
ISSUE_SOURCE_INVALID_UNIT = "source_invalid_unit"
ISSUE_PRICE_UNAVAILABLE = "price_unavailable"
ISSUE_PRICE_INVALID_UNIT = "price_invalid_unit"
ISSUE_PRICE_CURRENCY = "price_currency"

TEMPLATE_PREFIX = "template:"
# Longest piece of a template shown in an issue
_TEMPLATE_SHOWN = 60


def describe_source(source: str) -> str:
    """Return how a source or price source is shown in an issue."""
    if source.startswith(TEMPLATE_PREFIX):
        template = " ".join(source.removeprefix(TEMPLATE_PREFIX).split())
        if len(template) > _TEMPLATE_SHOWN:
            template = template[: _TEMPLATE_SHOWN - 1] + "…"
        return f"template {template}"
    return source


class IssueTracker:
    """Create and delete the repair issues of the integration."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the tracker."""
        self.hass = hass
        # issue ids created by this tracker
        self._active: set[str] = set()
        # source -> moment from which it is unusable without interruption
        self._unusable_since: dict[str, datetime] = {}

    # --------------------------------------------------------------- basics

    @callback
    def _set(
        self,
        issue_type: str,
        key: str,
        active: bool,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        """Create (active) or delete the issue issue_type for key."""
        issue_id = f"{issue_type}_{key}"
        if active:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=issue_type,
                translation_placeholders=placeholders,
            )
            self._active.add(issue_id)
        elif issue_id in self._active or ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self._active.discard(issue_id)

    @callback
    def async_clear_all(self) -> None:
        """Delete every issue of the integration (on unload or removal)."""
        async_delete_all_issues(self.hass)
        self._active.clear()
        self._unusable_since.clear()

    # -------------------------------------------------------------- checks

    @callback
    def check_duplicates(self, sources: dict[str, list[str]]) -> None:
        """Raise an issue for every sensor selected for more than one role."""
        roles: dict[str, list[str]] = {}
        for role, entity_ids in sources.items():
            for entity_id in entity_ids:
                roles.setdefault(entity_id, []).append(role)
        for entity_id, entity_roles in roles.items():
            self._set(
                ISSUE_DUPLICATE_SOURCE,
                entity_id,
                len(entity_roles) > 1,
                {"entity": entity_id, "roles": ", ".join(entity_roles)},
            )

    @callback
    def source_unit(self, entity_id: str, invalid_unit: str | None) -> None:
        """Report the unit check of a source meter (None means the unit is fine)."""
        self._set(
            ISSUE_SOURCE_INVALID_UNIT,
            entity_id,
            invalid_unit is not None,
            {"entity": entity_id, "unit": str(invalid_unit)},
        )

    @callback
    def price_unit(self, entity_id: str, invalid_unit: str | None) -> None:
        """Report the unit check of a price entity (None means the unit is fine)."""
        self._set(
            ISSUE_PRICE_INVALID_UNIT,
            entity_id,
            invalid_unit is not None,
            {"entity": entity_id, "unit": str(invalid_unit)},
        )

    @callback
    def price_currency(self, entity_id: str, currency: str | None) -> None:
        """Report the currency of a price entity (None when unknown, e.g. cents)."""
        expected = self.hass.config.currency
        wrong = currency is not None and bool(expected) and currency != expected.upper()
        self._set(
            ISSUE_PRICE_CURRENCY,
            entity_id,
            wrong,
            {"entity": entity_id, "currency": str(currency), "expected": str(expected)},
        )

    @callback
    def usable(self, source: str, usable: bool, now: datetime) -> None:
        """Register whether a source meter or price source is usable now."""
        if usable:
            self._unusable_since.pop(source, None)
            self._set(self._unavailable_type(source), _key(source), False)
        else:
            self._unusable_since.setdefault(source, now)

    @callback
    def check_unusable(self, now: datetime) -> None:
        """Raise an issue for every source that is unusable for too long."""
        for source, since in self._unusable_since.items():
            if now - since >= UNUSABLE_GRACE:
                self._set(
                    self._unavailable_type(source),
                    _key(source),
                    True,
                    {"entity": describe_source(source.removeprefix("price:"))},
                )

    @staticmethod
    def _unavailable_type(source: str) -> str:
        if source.startswith("price:"):
            return ISSUE_PRICE_UNAVAILABLE
        return ISSUE_SOURCE_UNAVAILABLE


def price_key(source: str) -> str:
    """Return the key under which a price source is tracked by usable()."""
    return f"price:{source}"


def _key(source: str) -> str:
    """Return the part of the issue id for a (price) source."""
    source = source.removeprefix("price:")
    if source.startswith(TEMPLATE_PREFIX):
        # A template can contain anything; a short hash keeps the id clean
        digest = hashlib.sha1(source.encode(), usedforsecurity=False).hexdigest()
        return f"template_{digest[:8]}"
    return source


@callback
def async_delete_all_issues(hass: HomeAssistant) -> None:
    """Delete every repair issue of the integration."""
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
