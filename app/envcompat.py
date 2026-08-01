# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 cTheXIV

"""Environment-variable back-compat shim for the MonarchAegis rename.

Every setting is read through `env()` under its current ``MONARCHAEGIS_*`` name,
transparently falling back to the pre-rename ``LSYNCD_*`` / ``LIVESYNCD_*``
variable with the same suffix. So a deployment whose Docker template still
carries the old variable names keeps working after the upgrade — the operator
updates the template whenever convenient, not at the moment they pull the image.

The two legacy prefixes never collide (``LSYNCD_`` carried the path/tuning vars,
``LIVESYNCD_`` the app-level ones — ROLE/PASSWORD/PAIR_SECRET/SAFE_MODE/USERNAME),
so checking both for a given suffix is unambiguous. New name wins when both set.

NOTE: this is the ONE module that must keep the literal legacy prefixes — it is
excluded from the rename sweep for exactly that reason.
"""
import os

CURRENT_PREFIX = "MONARCHAEGIS_"
LEGACY_PREFIXES = ("LSYNCD_", "LIVESYNCD_")


def legacy_names(name: str):
    """The pre-rename variable names that `name` supersedes."""
    if not name.startswith(CURRENT_PREFIX):
        return []
    suffix = name[len(CURRENT_PREFIX):]
    return [prefix + suffix for prefix in LEGACY_PREFIXES]


def env(name: str, default=None):
    """Read a MONARCHAEGIS_* setting, falling back to its legacy name, then
    `default`.

    An EMPTY value is treated as unset. Container templates (Unraid, compose)
    routinely pass optional variables through as empty strings when the operator
    leaves the field blank, and "" is never a meaningful value for any setting
    here — taking it literally turned an untouched optional field into a crash
    (e.g. int("") for HASH_WORKERS).
    """
    for candidate in (name, *legacy_names(name)):
        val = os.environ.get(candidate)
        if val is not None and val.strip() != "":
            return val
    return default
