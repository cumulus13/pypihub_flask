"""
custom_logging.py

Create a custom logging level:
    EMERGENCY, ALERT, CRITICAL, ERROR,
    WARNING, NOTICE, INFO, DEBUG,
    SUCCESS, FATAL
With syslog format + additional SUCCESS and FATAL.
"""

import logging

# ============================================================
# 1. LEVEL DEFINITION (Syslog + Extra)
# ============================================================

CUSTOM_LOG_LEVELS = {
    # Syslog RFC5424 severity (0 = highest severity)
    # We map to the top of the Python logging range (10–60)
    "EMERGENCY": 60,   # System unusable
    "ALERT":     55,   # Immediate action required
    "CRITICAL":  logging.CRITICAL,  # 50
    "ERROR":     logging.ERROR,     # 40
    "WARNING":   logging.WARNING,   # 30
    "NOTICE":    25,   # Normal but significant condition
    "INFO":      logging.INFO,      # 20
    "DEBUG":     logging.DEBUG,     # 10

    # Additional custom levels
    "SUCCESS":   22,   # Operation successful
    "FATAL":     65,   # Hard failure beyond CRITICAL
}

# ============================================================
# 2. LEVEL REGISTRATION TO LOGGING
# ============================================================

def register_custom_levels():
    for level_name, level_value in CUSTOM_LOG_LEVELS.items():
        # Register for Python logging
        logging.addLevelName(level_value, level_name)

        # Add method to logging.Logger
        def log_for(level):
            def _log_method(self, message, *args, **kwargs):
                if self.isEnabledFor(level):
                    self._log(level, message, args, **kwargs)
            return _log_method

        # create method lowercase: logger.emergency(), logger.notice(), dll
        setattr(logging.Logger, level_name.lower(), log_for(level_value))


register_custom_levels()

# ============================================================
# 3. FORMATTER DETAIL & PROFESSIONAL
# ============================================================

DEFAULT_FORMAT = (
    "[%(asctime)s] "
    "%(levelname)-10s "
    "%(name)s: "
    "%(message)s"
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_default_handler():
    handler = logging.StreamHandler()
    formatter = logging.Formatter(DEFAULT_FORMAT, DATE_FORMAT)
    handler.setFormatter(formatter)
    return handler


# ============================================================
# 4. FUNCTION TO GET THE LOGGER THAT IS READY
# ============================================================

def get_logger(name="default", level=logging.DEBUG):
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:  # Avoid duplicated handler
        logger.addHandler(get_default_handler())

    return logger
