"""Коллекторы источников.

Импорт конкретных коллекторов здесь регистрирует их в COLLECTORS.
"""

from gtm.collectors.base import COLLECTORS, Collector, CollectorError, RawFact, get_collector

__all__ = ["COLLECTORS", "Collector", "CollectorError", "RawFact", "get_collector"]
