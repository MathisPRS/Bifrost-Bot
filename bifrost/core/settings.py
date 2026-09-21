"""Reglages modifiables a chaud, ranges dans le meme SQLite que le reste.

Pourquoi une base plutot que `secrets.env` ou `config.yaml` :

  - `secrets.env` est pour ce qui est CONFIDENTIEL. Un ID de role Discord ne
    l'est pas : il est visible de tout le monde sur le serveur. L'y mettre
    obligerait en plus a redemarrer le bot pour le changer.
  - `config.yaml` est pour ce qui est STRUCTUREL et versionnable — quels jeux
    existent, quels seuils, quels timeouts.
  - ici vit ce qu'un HUMAIN change en cours de route et qui doit survivre au
    redemarrage.

Toujours en surcharge : pas de valeur ici -> on retombe sur la configuration
statique. La base ne peut donc pas casser un bot qui marchait.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""


class Settings:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(self.path, timeout=10)) as c:
            c.executescript(SCHEMA)
            c.commit()

    def _get(self, key: str) -> str | None:
        with closing(sqlite3.connect(self.path, timeout=10)) as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str, by: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.path, timeout=10)) as c:
            c.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (key, value, now, by))
            c.commit()

    # --- role autorise, par jeu ---------------------------------------------

    @staticmethod
    def _role_key(game: str) -> str:
        return f"role.{game}"

    def role_for(self, game: str) -> int | None:
        """ID du role autorise pour ce jeu, ou None s'il n'y a pas de surcharge.

        None n'est pas une erreur : l'appelant retombe sur DISCORD_ALLOWED_ROLE.
        """
        raw = self._get(self._role_key(game))
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def set_role(self, game: str, role_id: int, by: str) -> None:
        self._set(self._role_key(game), str(role_id), by)

    def clear_role(self, game: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as c:
            c.execute("DELETE FROM settings WHERE key = ?", (self._role_key(game),))
            c.commit()

    def roles(self) -> dict[str, tuple[int, str, str]]:
        """Toutes les surcharges : {jeu: (role_id, quand, par qui)}."""
        out = {}
        with closing(sqlite3.connect(self.path, timeout=10)) as c:
            for key, val, at, by in c.execute(
                    "SELECT key, value, updated_at, updated_by FROM settings "
                    "WHERE key LIKE 'role.%'"):
                try:
                    out[key[5:]] = (int(val), at, by or "?")
                except ValueError:
                    continue
        return out
