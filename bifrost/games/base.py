"""Le contrat qu'un jeu doit remplir.

`core/` ne connait que ce protocole. Il ne contient pas une seule fois le mot
"valheim". Ajouter un jeu = un fichier ici + une entree dans config.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Presence:
    """Etat de presence, avec sa provenance.

    `known` distingue "personne n'est connecte" de "je n'ai pas pu savoir".
    La porte "0 joueur" exige known=True ET count==0.
    """
    known: bool
    count: int = 0
    names: tuple[str, ...] = ()
    source: str = ""
    error: str | None = None


@dataclass(frozen=True)
class SaveInfo:
    """Une sauvegarde, identifiee par son numero de generation.

    Valheim incremente ce numero a chaque ecriture et le porte a la fois dans
    le nom des fichiers (_main.<n>.ok) et dans le log (=> Save number <n>).
    Deux preuves independantes qui doivent concorder.
    """
    known: bool
    generation: int | None = None
    at: datetime | None = None
    complete: bool = False          # le marqueur de fin d'ecriture est present
    error: str | None = None

    def age_s(self, now: datetime) -> float | None:
        return (now - self.at).total_seconds() if self.at else None


@dataclass(frozen=True)
class BackupReport:
    ok: bool
    dest: Path | None = None
    files: int = 0
    bytes: int = 0
    generation: int | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class Event:
    """Ligne de log traduite en fait structure, rangeable en base."""
    kind: str                       # save | join | leave | connected | error
    at: datetime
    player: str | None = None
    generation: int | None = None
    detail: str = ""


class GameDriver(Protocol):
    key: str
    container: str

    def is_up(self) -> bool: ...
    def presence(self) -> Presence: ...
    def last_save(self) -> SaveInfo: ...
    def snapshot(self, dest_root: Path) -> BackupReport: ...
    def stop(self, timeout_s: int): ...
    def start(self): ...
    def parse_log_line(self, line: str) -> Event | None: ...
