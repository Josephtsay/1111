"""Query-time location allowlist ("simple mask").

Scope and non-goals
-------------------
This module is deliberately small. It is a *query-time* filter only:

* It does not touch graph Schema v0.1 and creates no ``Location`` nodes.
* It builds no pre-sharded ``(skill, location)`` index and no pre-split buckets.
* It never rewrites the skill graph or edge weights.

Why the rollup matters
----------------------
Job ``location_code`` values in the search index are city level
(``CodeType=2``): in ``dataset/職缺.csv`` 1,218,580 of 1,218,635 rows resolve to
a ``CodeType=2`` code. User-supplied ``location_code`` (``c0`` in the search log)
is frequently district level (``CodeType=3``, ~963k rows). An exact-match filter
on a district code therefore removes *every* job, so districts must be rolled up
to their parent city before the allowlist is usable.

Every ``CodeType=3`` row in ``dataset/城市對照表.csv`` has a ``CodeNameB`` that
resolves to a ``CodeType=2`` ``CodeNameA``, so the name-based rollup covers 100%
of districts. A numeric-prefix rule is kept only as a fallback for codes that are
absent from the lookup table.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence, TypeVar

CODE_TYPE_NATIONWIDE = "1"
CODE_TYPE_CITY = "2"
CODE_TYPE_DISTRICT = "3"

CITY_TABLE_ENV_VAR = "JOB_SEARCH_CITY_TABLE"
DEFAULT_CITY_TABLE_NAME = "dataset/城市對照表.csv"
DEFAULT_MIN_CANDIDATES = 10

_CODE_SPLIT = re.compile(r"[,;|/\s]+")

T = TypeVar("T")


class LocationMaskMode(str, Enum):
    """How a resolved allowlist should be enforced.

    ``hard_with_fallback`` is the recommended default: filter hard, but if the
    masked candidate pool is too thin to fill a page, drop the filter rather than
    returning an almost-empty result.
    """

    OFF = "off"
    SOFT = "soft"
    HARD = "hard"
    HARD_WITH_FALLBACK = "hard_with_fallback"

    @classmethod
    def coerce(cls, value: "LocationMaskMode | str | None") -> "LocationMaskMode":
        if value is None:
            return cls.HARD_WITH_FALLBACK
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        raise ValueError(
            f"Unknown location mask mode: {value!r}; "
            f"expected one of {[member.value for member in cls]}"
        )

    @property
    def filters(self) -> bool:
        """True when the mode removes non-matching candidates from the pool."""

        return self in (LocationMaskMode.HARD, LocationMaskMode.HARD_WITH_FALLBACK)

    @property
    def allows_fallback(self) -> bool:
        return self is LocationMaskMode.HARD_WITH_FALLBACK


def split_location_codes(values: object) -> list[str]:
    """Normalize location codes from either an API list or a log-style string.

    The API contract sends ``location_code`` as ``list[str]``, but the raw search
    log stores multiple codes joined by commas (``"100100,100200,100900"``).
    Accepting both keeps offline replay and online serving on one code path.
    """

    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raw: Iterable[object] = [values]
    elif isinstance(values, Iterable):
        raw = values
    else:
        raw = [values]
    codes: list[str] = []
    for value in raw:
        if value is None:
            continue
        text = value.decode() if isinstance(value, bytes) else str(value)
        for token in _CODE_SPLIT.split(text.strip()):
            token = token.strip()
            if token:
                codes.append(token)
    return list(dict.fromkeys(codes))


@dataclass(frozen=True)
class LocationMask:
    """An immutable query-time allowlist of job ``location_code`` values.

    ``allowed is None`` means "do not mask" and is used for both an empty request
    and an explicit nationwide (``CodeType=1``) request.
    """

    allowed: frozenset[str] | None = None
    requested: tuple[str, ...] = ()
    rollups: tuple[tuple[str, str], ...] = ()
    nationwide: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.allowed is not None and bool(self.allowed)

    @property
    def codes(self) -> tuple[str, ...]:
        """Deterministically ordered allowlist, safe to splice into SQL params."""

        return tuple(sorted(self.allowed)) if self.allowed else ()

    def allows(self, location_code: object) -> bool:
        if not self.active:
            return True
        return str(location_code or "").strip() in self.allowed

    def sql_filter(self, column: str = "location_code") -> tuple[str, tuple[str, ...]]:
        """Return an ``IN`` predicate and its bound parameters.

        ``column`` is caller-controlled (never request data), and the values are
        always passed as bound parameters, so this cannot be used for injection.
        """

        codes = self.codes
        if not codes:
            return "", ()
        placeholders = ",".join("?" for _ in codes)
        return f"{column} IN ({placeholders})", codes

    def describe(self) -> dict[str, object]:
        return {
            "active": self.active,
            "requested": list(self.requested),
            "allowed": list(self.codes),
            "rollups": {district: city for district, city in self.rollups},
            "nationwide": list(self.nationwide),
            "unresolved": list(self.unresolved),
        }


INACTIVE_MASK = LocationMask()


class LocationCodeTable:
    """Lookup over ``dataset/城市對照表.csv`` for district-to-city rollup."""

    def __init__(self, rows: Iterable[dict[str, str]]) -> None:
        self.code_types: dict[str, str] = {}
        self.city_codes: set[str] = set()
        self._city_code_by_name: dict[str, str] = {}
        self._parent_by_district: dict[str, str] = {}
        materialized = [dict(row) for row in rows]
        for row in materialized:
            code = str(row.get("CodeNo", "") or "").strip()
            code_type = str(row.get("CodeType", "") or "").strip()
            if not code:
                continue
            self.code_types[code] = code_type
            if code_type == CODE_TYPE_CITY:
                self.city_codes.add(code)
                name = str(row.get("CodeNameA", "") or "").strip()
                # Mirror the index-time preference in dataset_1111.city_name_map:
                # shortest/smallest CodeNo wins so repeated names stay stable.
                if name and (
                    name not in self._city_code_by_name
                    or (len(code), code) < (
                        len(self._city_code_by_name[name]),
                        self._city_code_by_name[name],
                    )
                ):
                    self._city_code_by_name[name] = code
        for row in materialized:
            code = str(row.get("CodeNo", "") or "").strip()
            if not code or self.code_types.get(code) != CODE_TYPE_DISTRICT:
                continue
            parent_name = str(row.get("CodeNameB", "") or "").strip()
            parent = self._city_code_by_name.get(parent_name)
            if parent:
                self._parent_by_district[code] = parent

    @classmethod
    def load(cls, path: str | Path) -> "LocationCodeTable":
        source = Path(path)
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            return cls(csv.DictReader(handle))

    @classmethod
    def empty(cls) -> "LocationCodeTable":
        return cls(())

    def code_type(self, code: str) -> str | None:
        return self.code_types.get(str(code).strip())

    def _numeric_parent(self, code: str) -> str | None:
        """Fallback rollup for codes missing from the table (100101 -> 100100)."""

        code = str(code).strip()
        if len(code) != 6 or not code.isdigit() or code.endswith("00"):
            return None
        candidate = f"{code[:4]}00"
        if not self.city_codes or candidate in self.city_codes:
            return candidate
        return None

    def resolve(self, code: str) -> tuple[str, str | None]:
        """Map one requested code to ``(kind, city_code)``.

        ``kind`` is one of ``"city"``, ``"rollup"``, ``"nationwide"`` or
        ``"unresolved"``. ``"unresolved"`` still yields the original code so an
        unknown-but-valid code is passed through rather than silently dropped.
        """

        code = str(code).strip()
        if not code:
            return "unresolved", None
        code_type = self.code_type(code)
        if code_type == CODE_TYPE_NATIONWIDE:
            return "nationwide", None
        if code_type == CODE_TYPE_CITY:
            return "city", code
        if code_type == CODE_TYPE_DISTRICT:
            parent = self._parent_by_district.get(code) or self._numeric_parent(code)
            if parent:
                return "rollup", parent
            return "unresolved", code
        parent = self._numeric_parent(code)
        if parent:
            return "rollup", parent
        return "unresolved", code

    def build_mask(self, codes: object) -> LocationMask:
        """Build the allowlist for a request.

        * empty request -> ``allowed=None`` (no mask)
        * ``CodeType=2`` -> kept as-is
        * ``CodeType=3`` -> rolled up to the parent city
        * ``CodeType=1`` -> no mask (nationwide)
        * multiple codes -> union
        """

        requested = tuple(split_location_codes(codes))
        if not requested:
            return LocationMask(allowed=None)
        allowed: set[str] = set()
        rollups: list[tuple[str, str]] = []
        nationwide: list[str] = []
        unresolved: list[str] = []
        for code in requested:
            kind, resolved = self.resolve(code)
            if kind == "nationwide":
                nationwide.append(code)
                continue
            if kind == "rollup" and resolved:
                allowed.add(resolved)
                rollups.append((code, resolved))
                continue
            if kind == "city" and resolved:
                allowed.add(resolved)
                continue
            if resolved:
                allowed.add(resolved)
            unresolved.append(code)
        if nationwide:
            # A nationwide code widens the request to everything; masking on top
            # of it would contradict the user's intent.
            return LocationMask(
                allowed=None,
                requested=requested,
                rollups=tuple(rollups),
                nationwide=tuple(nationwide),
                unresolved=tuple(unresolved),
            )
        return LocationMask(
            allowed=frozenset(allowed) if allowed else None,
            requested=requested,
            rollups=tuple(rollups),
            nationwide=(),
            unresolved=tuple(unresolved),
        )


def _default_city_table_path() -> Path | None:
    override = os.environ.get(CITY_TABLE_ENV_VAR, "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    base = Path(__file__).resolve().parent
    for root in (base, base.parent):
        candidate = root / DEFAULT_CITY_TABLE_NAME
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def default_location_table() -> LocationCodeTable:
    """Load the bundled city table once; degrade to a numeric-only table.

    A missing CSV must not break serving: the numeric-prefix rollup still
    handles the standard 6-digit district codes.
    """

    path = _default_city_table_path()
    if path is None:
        return LocationCodeTable.empty()
    return LocationCodeTable.load(path)


def resolve_location_mask(
    codes: object,
    *,
    table: LocationCodeTable | None = None,
) -> LocationMask:
    """Convenience entry point: request codes in, allowlist out."""

    if isinstance(codes, LocationMask):
        return codes
    resolver = table if table is not None else default_location_table()
    return resolver.build_mask(codes)


def effective_min_candidates(min_candidates: int, requested: int) -> int:
    """Clamp the fallback floor to the page size actually being requested.

    ``min_candidates`` is a quality floor for large candidate pools: below it,
    a masked pool is too thin to rank meaningfully. It must never exceed what
    the caller asked for, otherwise a request for ``top_k`` results widens back
    to nationwide even though the masked pool could already fill the page.
    """

    return max(0, min(int(min_candidates), int(requested)))


@dataclass(frozen=True)
class MaskApplication:
    """Outcome of enforcing a mask over an in-memory candidate pool."""

    items: tuple
    applied: bool
    reason: str
    considered: int = 0
    matched: int = 0

    def as_list(self) -> list:
        return list(self.items)


def apply_location_mask(
    items: Sequence[T],
    location_of: Callable[[T], object],
    mask: LocationMask,
    *,
    mode: LocationMaskMode | str | None = None,
    min_candidates: int = DEFAULT_MIN_CANDIDATES,
) -> MaskApplication:
    """Filter ``items`` by ``mask``, honouring the configured enforcement mode.

    Under ``hard_with_fallback`` the unfiltered pool is returned when the masked
    pool holds fewer than ``min_candidates`` entries, so a narrow or mismatched
    location never yields an empty page.
    """

    resolved_mode = LocationMaskMode.coerce(mode)
    pool = tuple(items)
    if not mask.active or not resolved_mode.filters:
        return MaskApplication(
            items=pool,
            applied=False,
            reason="mask_inactive" if not mask.active else f"mode_{resolved_mode.value}",
            considered=len(pool),
            matched=len(pool),
        )
    kept = tuple(item for item in pool if mask.allows(location_of(item)))
    if resolved_mode.allows_fallback and len(kept) < max(0, int(min_candidates)):
        return MaskApplication(
            items=pool,
            applied=False,
            reason="fallback_thin_pool",
            considered=len(pool),
            matched=len(kept),
        )
    return MaskApplication(
        items=kept,
        applied=True,
        reason="masked",
        considered=len(pool),
        matched=len(kept),
    )
