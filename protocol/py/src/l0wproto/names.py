"""Member names (membership.md)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .errors import NameRuleError

NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*\Z")
MIN_LEN, MAX_LEN = 3, 32

RESERVED = frozenset(
    "admin api www hub files web mail email board spaces rooms relay status help support abuse "
    "postmaster root security tunnel directory well-known".split()
)

_LOOKALIKE = str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s", "-": None})


def validate_name(name: str) -> str:
    """Raise NameRuleError (`bad_registration`) unless `name` follows the syntax rules."""
    if not isinstance(name, str) or not (MIN_LEN <= len(name) <= MAX_LEN):
        raise NameRuleError(f"name must be {MIN_LEN} to {MAX_LEN} characters")
    if "--" in name or not NAME_RE.match(name):
        raise NameRuleError("name must be lowercase letters and digits with single dashes between them")
    return name


def lookalike_key(name: str) -> str:
    """Map 0->o, 1->l, i->l, 5->s and remove dashes."""
    return name.translate(_LOOKALIKE)


def is_reserved(name: str, extra: Iterable[str] = ()) -> bool:
    return name in RESERVED or name in set(extra)


def check_name(name: str, *, taken: Iterable[str] = (), reserved_extra: Iterable[str] = ()) -> str:
    """Full hub-side check. Codes: bad_registration (syntax), name_reserved, name_taken.
    `taken` includes names that left (never reused). `reserved_extra` is the hub's services and its own name."""
    validate_name(name)
    reserved = set(RESERVED) | set(reserved_extra)
    if name in reserved:
        raise NameRuleError("name is reserved", code="name_reserved")
    if name in set(taken):
        raise NameRuleError("name is taken", code="name_taken")
    key = lookalike_key(name)
    if any(lookalike_key(r) == key for r in reserved):
        raise NameRuleError("name looks like a reserved name", code="name_reserved")
    if any(lookalike_key(t) == key for t in taken):
        raise NameRuleError("name looks like an existing name", code="name_taken")
    return name
