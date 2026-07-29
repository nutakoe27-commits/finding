"""Коллекторы источников.

Импорт конкретных коллекторов здесь регистрирует их в COLLECTORS —
без него get_collector("manual") не найдёт ничего.
"""

from gtm.collectors.base import COLLECTORS, Collector, CollectorError, RawFact, get_collector
from gtm.collectors.events_archive import EventsArchiveCollector
from gtm.collectors.hh import HHCollector
from gtm.collectors.manual import ManualCollector
from gtm.collectors.registry import RegistryCollector
from gtm.collectors.zakupki import ZakupkiCollector

__all__ = [
    "COLLECTORS",
    "Collector",
    "CollectorError",
    "EventsArchiveCollector",
    "HHCollector",
    "ManualCollector",
    "RawFact",
    "RegistryCollector",
    "ZakupkiCollector",
    "get_collector",
]
