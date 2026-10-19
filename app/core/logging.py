import logging
import sys

import structlog

_configured = False


def configure_logging(level: str) -> None:
    global _configured
    if _configured:
        return

    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(noisy)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(log_level)

    # SQLAlchemy's engine logger emits every statement and every parameter set
    # at INFO, which buries application events and leaks row values into the
    # journal. Keep it at WARNING unless the operator explicitly asks for DEBUG.
    sqlalchemy_level = logging.DEBUG if log_level <= logging.DEBUG else logging.WARNING
    for noisy in ("sqlalchemy", "sqlalchemy.engine"):
        lg = logging.getLogger(noisy)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(sqlalchemy_level)

    _configured = True
