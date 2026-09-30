"""La prise, vue à travers Home Assistant. Unique implémentation de `Plug`.

Trois invariants, les mêmes que partout ailleurs dans le projet :

  - une lecture ratée, indisponible ou non concluante n'est **jamais** une
    valeur : elle se déclare inconnue ;
  - une écriture n'est acquise que **confirmée par relecture**, jamais parce
    qu'un appel HTTP a rendu 200 ;
  - une prise déclarée sans interrupteur refuse toute écriture.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .homeassistant import HAClient, HAError
from .plug import PlugCommandFailed, PlugReading, PlugWriteDenied

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlugEntities:
    """Entités Home Assistant d'une prise.

    `switch` vide = prise non pilotable. C'est le cas de la prise qui alimente
    le NAS : LocalTuya ne l'expose qu'en capteur, sans interrupteur. Home
    Assistant est donc **structurellement** incapable de la couper — un
    garde-fou qui ne dépend pas de la correction de notre code.
    """

    nom: str
    switch: str = ""
    power: str = ""
    voltage: str = ""
    current: str = ""

    @property
    def pilotable(self) -> bool:
        return bool(self.switch)


class HAPlug:
    def __init__(self, ha: HAClient, entities: PlugEntities):
        self.ha = ha
        self.e = entities

    def describe(self) -> str:
        return self.e.switch or self.e.power or self.e.nom

    # --- lecture -------------------------------------------------------------

    def read(self) -> PlugReading:
        now = datetime.now(timezone.utc)

        on: bool | None = None
        if self.e.switch:
            st = self.ha.state(self.e.switch)
            if not st.ok:
                return PlugReading(ok=False, at=now, error=st.error)
            on = st.value == "on"

        watts = volts = None
        milliamps = None
        measured_at = None
        if self.e.power:
            st = self.ha.state(self.e.power)
            if not st.ok:
                return PlugReading(ok=False, at=now, error=st.error)
            watts = st.as_float()
            measured_at = st.at
            # Sans interrupteur exposé, la consommation dit quand même si la
            # prise débite.
            if on is None:
                on = bool(watts and watts > 0)

        if self.e.voltage:
            v = self.ha.state(self.e.voltage)
            volts = v.as_float() if v.ok else None
        if self.e.current:
            c = self.ha.state(self.e.current)
            a = c.as_float() if c.ok else None
            milliamps = int(a) if a is not None else None

        return PlugReading(ok=True, at=now, on=on, watts=watts, volts=volts,
                           milliamps=milliamps, measured_at=measured_at)

    def read_power(self) -> float | None:
        r = self.read()
        return r.watts if r.ok else None

    # --- écriture ------------------------------------------------------------

    def _guard(self) -> None:
        if not self.e.pilotable:
            raise PlugWriteDenied(
                f"prise « {self.e.nom} » : aucune entité switch dans Home "
                "Assistant, elle n'est pas pilotable")

    def _command(self, on: bool, tries: int = 2, confirm_s: int = 10) -> None:
        """Commande, puis **confirme par relecture**.

        Home Assistant répond 200 dès qu'il a accepté l'appel, pas quand
        l'appareil a basculé. Le 200 n'est donc pas une preuve : seule la
        relecture en est une.
        """
        ordre = "turn_on" if on else "turn_off"
        derniere = ""

        for essai in range(1, tries + 1):
            try:
                self.ha.call("switch", ordre, self.e.switch)
            except HAError as exc:
                derniere = str(exc)
                log.warning("prise %s : essai %d/%d refusé par HA — %s",
                            self.e.nom, essai, tries, derniere)
                continue

            for _ in range(confirm_s):
                time.sleep(1.0)
                r = self.read()
                if r.ok and r.on is on:
                    return
                derniere = r.error or (
                    f"état toujours {'allumée' if r.on else 'éteinte'}")
            log.warning("prise %s : essai %d/%d non confirmé — %s",
                        self.e.nom, essai, tries, derniere)

        raise PlugCommandFailed(
            f"Home Assistant n'a pas confirmé « {ordre} » sur {self.e.switch} "
            f"après {tries} essais : {derniere}")

    def turn_on(self) -> bool:
        self._guard()
        log.warning("prise %s : mise sous tension", self.e.nom)
        self._command(True)
        return True

    def turn_off(self) -> bool:
        self._guard()
        log.warning("prise %s : COUPURE", self.e.nom)
        self._command(False)
        return True
