"""
Logging partagé pour tout le projet.

Remplace les print() éparpillés dans le code par de vrais logs, écrits à la
fois dans la console (comme avant) et dans un fichier avec rotation
(LOG_FILE, voir config.py), pour pouvoir déboguer après coup sans avoir
besoin d'un terminal ouvert en permanence.
"""
import logging
from logging.handlers import RotatingFileHandler

from config import LOG_FILE

_configured_loggers = {}


def get_logger(name: str) -> logging.Logger:
    if name in _configured_loggers:
        return _configured_loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    _configured_loggers[name] = logger
    return logger
