"""Le contrat d'une prise pilotée, et rien d'autre.

Ce module ne parle à aucun matériel : il ne porte que les types partagés et le
protocole que l'orchestrateur manipule. L'unique implémentation est `HAPlug`,
qui passe par Home Assistant.

Pourquoi Home Assistant et pas un dialogue direct avec l'appareil : une prise
Tuya n'accepte qu'**une session de contrôle locale à la fois**. Home Assistant
en tient une en permanence via LocalTuya ; toute seconde session entre en
conflit avec elle. Un seul programme parle au matériel, et c'est celui qui sait
tenir la connexion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


class PlugWriteDenied(RuntimeError):
    """Écriture refusée sur une prise qui n'est pas censée être commandée."""


class PlugCommandFailed(RuntimeError):
    """L'ordre n'a pas été confirmé, et on sait le dire."""


@dataclass(frozen=True)
class PlugReading:
    """Un relevé, ou l'aveu qu'on n'a pas pu en obtenir.

    `ok=False` signifie « je ne sais pas » — jamais « zéro ». Confondre les deux
    reviendrait à couper le courant d'une machine allumée parce qu'une lecture a
    échoué.
    """

    ok: bool
    at: datetime
    on: bool | None = None
    watts: float | None = None
    volts: float | None = None
    milliamps: int | None = None
    error: str | None = None
    # Quand la mesure de puissance a été rafraîchie côté source. Sert à exiger
    # une mesure postérieure au début d'une séquence : voir chk_power_below().
    measured_at: datetime | None = None

    @property
    def age_s(self) -> float | None:
        if self.measured_at is None:
            return None
        return (datetime.now(timezone.utc) - self.measured_at).total_seconds()

    def __str__(self) -> str:
        if not self.ok:
            return f"lecture indisponible ({self.error})"
        etat = "allumée" if self.on else "éteinte"
        w = f"{self.watts:.1f} W" if self.watts is not None else "? W"
        v = f"{self.volts:.1f} V" if self.volts is not None else "? V"
        a = f"{self.milliamps} mA" if self.milliamps is not None else "? mA"
        age = self.age_s
        vieux = f" (mesure d'il y a {age / 60:.0f} min)" if age and age > 90 else ""
        return f"{etat}, {w}, {v}, {a}{vieux}"


class Plug(Protocol):
    """Ce que l'orchestrateur attend d'une prise, quelle que soit la source."""

    def read(self) -> PlugReading: ...
    def read_power(self) -> float | None: ...
    def turn_on(self) -> bool: ...
    def turn_off(self) -> bool: ...
    def describe(self) -> str: ...
