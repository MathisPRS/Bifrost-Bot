"""Machine a etats et verrou d'exclusion.

Le verrou ne sert pas a gerer plusieurs jeux — il n'y en a qu'un. Il empeche
qu'un /start et un /stop se chevauchent, ce qui suffit a eliminer une classe
entiere d'incidents.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class State(Enum):
    OFF = "eteint"
    BOOTING = "demarrage"
    UP = "en ligne"
    STOPPING = "extinction"
    ERROR = "erreur"
    UNKNOWN = "indetermine"

    @property
    def emoji(self) -> str:
        return {"eteint": "⚫", "demarrage": "🟡", "en ligne": "🟢",
                "extinction": "🟠", "erreur": "🔴", "indetermine": "⚪"}[self.value]


class Busy(RuntimeError):
    """Une sequence est deja en cours."""


@dataclass
class Machine:
    state: State = State.UNKNOWN
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    current_op: str | None = None
    last_error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def set(self, state: State, error: str | None = None) -> None:
        if state is not self.state:
            self.state = state
            self.since = datetime.now(timezone.utc)
        self.last_error = error

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def acquire(self, op: str) -> "_Op":
        return _Op(self, op)


class _Op:
    """Contexte d'une operation exclusive.

    Refuse immediatement plutot que d'attendre : l'utilisateur doit savoir
    qu'une sequence tourne deja, pas voir sa commande partir en file d'attente.
    """

    def __init__(self, m: Machine, op: str):
        self._m = m
        self._op = op

    async def __aenter__(self) -> Machine:
        if self._m._lock.locked():
            raise Busy(f"« {self._m.current_op} » est deja en cours")
        await self._m._lock.acquire()
        self._m.current_op = self._op
        self._m.cancel.clear()
        return self._m

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._m.current_op = None
        self._m._lock.release()
