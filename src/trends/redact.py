"""Replace personal email addresses in text that goes into a report.

Issue titles and bodies sometimes contain a reporter's email address. Reports are meant to
be shared, so addresses become "[email]". Support addresses (support@...) are kept, because
they are useful in a report and are not personal.
"""

from __future__ import annotations

import re

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _replace(match: re.Match) -> str:
    address = match.group(0)
    return address if address.lower().startswith("support@") else "[email]"


def redact_emails(text: str) -> str:
    return EMAIL.sub(_replace, text)
