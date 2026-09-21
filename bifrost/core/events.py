"""Journal d'evenements structures (SQLite).

Sert deux choses que les logs Docker ne peuvent pas rendre :

  - une HISTOIRE qui survit a la recreation du conteneur et a l'extinction de
    la machine. `docker logs` ne remonte qu'au conteneur courant ; quand le
    Proxmox est eteint, il ne remonte a rien du tout. C'est ici que /status va
    chercher « derniere session : hier 22h14, 3 joueurs ».
  - un JOURNAL D'AUDIT : qui a lance quoi, quand, et avec quel resultat.

On y range des faits, pas des lignes de texte. Les lignes brutes, si on en veut
un jour pour enqueter, c'est le role de Vector vers OpenSearch.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    at         TEXT    NOT NULL,          -- ISO 8601 UTC
    game       TEXT    NOT NULL,
    kind       TEXT    NOT NULL,          -- save | join | leave | connected | error
    player     TEXT,
    generation INTEGER,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at   ON events(at);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(game, kind, at);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    who      TEXT NOT NULL,
    command  TEXT NOT NULL,
    outcome  TEXT NOT NULL,               -- ok | refus | echec
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);

CREATE TABLE IF NOT EXISTS power_samples (
    at      TEXT NOT NULL,
    watts   REAL,                         -- NULL = lecture INCONNUE, pas zero
    phase   TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # --- ecriture ------------------------------------------------------------

    def add_event(self, game: str, kind: str, at: datetime | None = None,
                  player: str | None = None, generation: int | None = None,
                  detail: str = "") -> None:
        ts = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO events(at, game, kind, player, generation, detail) "
                "VALUES (?,?,?,?,?,?)", (ts, game, kind, player, generation, detail))
            c.commit()

    def audit(self, who: str, command: str, outcome: str, detail: str = "") -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT INTO audit(at, who, command, outcome, detail) VALUES (?,?,?,?,?)",
                      (_now(), who, command, outcome, detail[:2000]))
            c.commit()

    def add_power(self, watts: float | None, phase: str) -> None:
        """`watts=None` enregistre explicitement une lecture INCONNUE : la
        distinction avec 0 W doit survivre jusque dans l'historique."""
        with closing(self._conn()) as c:
            c.execute("INSERT INTO power_samples(at, watts, phase) VALUES (?,?,?)",
                      (_now(), watts, phase))
            c.commit()

    # --- lecture -------------------------------------------------------------

    def last_event(self, game: str, kind: str) -> sqlite3.Row | None:
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT * FROM events WHERE game=? AND kind=? ORDER BY at DESC LIMIT 1",
                (game, kind)).fetchone()

    def last_players(self, game: str, limit: int = 5) -> list[tuple[str, str]]:
        """Derniers joueurs vus, du plus recent au plus ancien."""
        with closing(self._conn()) as c:
            rows = c.execute(
                "SELECT player, MAX(at) AS at FROM events "
                "WHERE game=? AND kind='join' AND player IS NOT NULL "
                "GROUP BY player ORDER BY at DESC LIMIT ?", (game, limit)).fetchall()
        return [(r["player"], r["at"]) for r in rows]

    def recent_audit(self, limit: int = 10) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            return c.execute("SELECT * FROM audit ORDER BY at DESC LIMIT ?", (limit,)).fetchall()

    def power_series(self, since_iso: str) -> list[tuple[str, float | None, str]]:
        with closing(self._conn()) as c:
            rows = c.execute(
                "SELECT at, watts, phase FROM power_samples WHERE at >= ? ORDER BY at",
                (since_iso,)).fetchall()
        return [(r["at"], r["watts"], r["phase"]) for r in rows]
