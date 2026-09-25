"""Deterministic DLP redaction for JSON-compatible MCP input and output values."""
from __future__ import annotations

import json
import os
import re

PATTERNS = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I | re.S)),
    ("CREDENTIAL", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}\b")),
    ("EMAIL", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)")),
]
CARD = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")
SENSITIVE_KEYS = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential|authorization|cookie|ssn|social_security|credit.?card|card_number)")


def _luhn(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19: return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9: digit -= 9
        total += digit
    return total % 10 == 0


def redact_text(value: str, counts: dict[str, int] | None = None) -> str:
    counts = counts if counts is not None else {}
    for label, pattern in PATTERNS:
        value, number = pattern.subn(f"[REDACTED:{label}]", value)
        if number: counts[label] = counts.get(label, 0) + number

    def card_replace(match):
        if not _luhn(match.group(0)): return match.group(0)
        counts["PAYMENT_CARD"] = counts.get("PAYMENT_CARD", 0) + 1
        return "[REDACTED:PAYMENT_CARD]"
    value = CARD.sub(card_replace, value)

    custom = json.loads(os.getenv("MCP_DLP_CUSTOM_PATTERNS", "{}"))
    for label, expression in custom.items():
        pattern = re.compile(expression, re.I)
        value, number = pattern.subn(f"[REDACTED:{label}]", value)
        if number: counts[label] = counts.get(label, 0) + number
    phrases = json.loads(os.getenv("MCP_DLP_SECRET_PHRASES", "[]"))
    if isinstance(phrases, str): phrases = [phrases]
    for phrase in phrases:
        if isinstance(phrase, str) and phrase:
            value, number = re.subn(re.escape(phrase), "[REDACTED:PROPRIETARY]", value, flags=re.I)
            if number: counts["PROPRIETARY"] = counts.get("PROPRIETARY", 0) + number
    return value


def redact_tree(value, counts: dict[str, int] | None = None):
    counts = counts if counts is not None else {}
    if isinstance(value, str): return redact_text(value, counts)
    if isinstance(value, list): return [redact_tree(item, counts) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SENSITIVE_KEYS.search(str(key)) and item not in (None, "", [], {}):
                counts["SENSITIVE_FIELD"] = counts.get("SENSITIVE_FIELD", 0) + 1
                result[key] = "[REDACTED:SENSITIVE_FIELD]"
            else:
                result[key] = redact_tree(item, counts)
        return result
    return value
