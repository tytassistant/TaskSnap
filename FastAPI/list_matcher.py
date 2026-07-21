"""Deterministic fallback matcher for the list_table refactor.

By design, essentially all of the real matching work (recognizing an
explicit category/list mention, or judging which list's keywords fit a
task's content) happens inside the single AI extraction call in
poe_client.py -- it has real context (the photo, the phrasing) that
Python substring-matching can't reliably approximate. This module's job
is deliberately small: given what the AI already decided
(categoryIdentified, always present; listIdentified, maybe null),
resolve the final list -- or leave it unmatched for manual assignment.

Pure function, no AI/DB/network imports -- callers pass plain data
(list_entries from crud.list_all_list_entries()).
"""

from typing import Optional


def resolve_list(category_identified: str, list_identified: Optional[str], list_entries: list) -> Optional[dict]:
    """Returns the matching list_table row dict, or None (unmatched).

    If list_identified is present, it must be an exact (case-insensitive)
    list_name already in list_table -- the AI is only ever instructed to
    return a known list_name or null, never raw/unrecognized words, so a
    non-match here is treated as unmatched rather than "create a new
    list." If list_identified is null, fall back to whichever list is
    flagged list_is_category_default for category_identified; zero or
    more than one such list is also unmatched (never guess)."""
    if list_identified:
        lowered = list_identified.lower()
        for row in list_entries:
            if row["list_name"].lower() == lowered:
                return row
        return None

    defaults = [
        row for row in list_entries
        if category_identified in row["list_category"] and row["list_is_category_default"]
    ]
    return defaults[0] if len(defaults) == 1 else None
