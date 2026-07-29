from __future__ import annotations

import logging
import re


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:access|refresh|session)[_-]?token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+"),
)


def redact(value: object) -> str:
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


def _redact_argument(value: object) -> object:
    return redact(value) if isinstance(value, str) else value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: _redact_argument(value) for key, value in record.args.items()}
            else:
                record.args = tuple(_redact_argument(value) for value in record.args)
        return True


def install_redaction_filter() -> None:
    for logger_name in ("chatgpt_web_gateway", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(item, RedactingFilter) for item in logger.filters):
            logger.addFilter(RedactingFilter())
        for handler in logger.handlers:
            if not any(isinstance(item, RedactingFilter) for item in handler.filters):
                handler.addFilter(RedactingFilter())
