"""La prise, vue a travers Home Assistant.

Meme interface que `PlugClient` (read / read_power / turn_on / turn_off), pour
que l'orchestrateur ne sache pas par quel chemin il passe. Seule la fabrique
`build_plug` choisit le backend.

Les invariants ne changent pas, ce sont les memes depuis le debut :
  - une lecture ratee, indisponible ou PERIMEE n'est jamais une valeur ;
  - une ecriture doit etre confirmee par relecture, jamais supposee ;
  - une prise declaree read_only refuse toute ecriture.
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
class HAPlugConf:
    """Entites HA d'une prise. `switch` vide = prise non pilotable.

    C'est le cas de la prise du NAS : LocalTuya ne l'expose que comme capteur
    binaire, pas comme interrupteur. HA est donc STRUCTURELLEMENT incapable de
    la couper — un garde-fou meilleur qu'un controle logiciel, puisqu'il ne
    depend pas de la correction de notre code.
    """
    nom: str
    switch: str = ""
    power: str = ""
    voltage: str = ""
    current: str = ""
    read_only: bool = False


class HAPlug:
    def __init__(self, ha: HAClient, conf: HAPlugConf):
        self.ha = ha
        self.conf = conf

    # --- lecture -------------------------------------------------------------

    def resolve(self) -> str | None:
        """Pas d'adresse a resoudre : HA s'en charge. On rend l'entite, pour
        que les messages restent parlants."""
        return self.conf.switch or self.conf.power or None

    def read(self) -> PlugReading:
        now = datetime.now(timezone.utc)

        on: bool | None = None
        if self.conf.switch:
            st = self.ha.state(self.conf.switch)
            if not st.ok:
                return PlugReading(ok=False, at=now, error=st.error)
            on = st.value == "on"

        watts = volts = None
        milliamps = None
        mesure_at = None
        if self.conf.power:
            st = self.ha.state(self.conf.power)
            if not st.ok:
                return PlugReading(ok=False, at=now, error=st.error)
            watts = st.as_float()
            mesure_at = st.at
            # Sans interrupteur expose (prise protegee), la consommation dit
            # quand meme si elle debite.
            if on is None:
                on = bool(watts and watts > 0)
        if self.conf.voltage:
            v = self.ha.state(self.conf.voltage)
            volts = v.as_float() if v.ok else None
        if self.conf.current:
            c = self.ha.state(self.conf.current)
            a = c.as_float() if c.ok else None
            milliamps = int(a) if a is not None else None

        return PlugReading(ok=True, at=now, on=on, watts=watts,
                           volts=volts, milliamps=milliamps,
                           measured_at=mesure_at)

    def read_power(self) -> float | None:
        r = self.read()
        return r.watts if r.ok else None

    # --- ecriture ------------------------------------------------------------

    def _guard(self) -> None:
        if self.conf.read_only:
            raise PlugWriteDenied(
                f"prise « {self.conf.nom} » déclarée en lecture seule")
        if not self.conf.switch:
            raise PlugWriteDenied(
                f"prise « {self.conf.nom} » : aucune entité switch dans Home "
                "Assistant, elle n'est pas pilotable")

    def _command(self, on: bool, tries: int = 2) -> None:
        """Commande puis CONFIRME par relecture.

        HA repond 200 des qu'il a accepte l'appel, pas quand l'appareil a
        bascule. Le 200 n'est donc pas une preuve : seule la relecture en est
        une. On laisse au passage le temps a HA de rafraichir son etat.
        """
        derniere = ""
        for essai in range(1, tries + 1):
            try:
                self.ha.call("switch", "turn_on" if on else "turn_off",
                             self.conf.switch)
            except HAError as exc:
                derniere = str(exc)
                log.warning("prise %s : essai %d/%d refusé par HA — %s",
                            self.conf.nom, essai, tries, derniere)
                continue

            for _ in range(10):           # jusqu'a ~10 s de confirmation
                time.sleep(1.0)
                r = self.read()
                if r.ok and r.on is on:
                    return
                derniere = r.error or f"état toujours {'allumée' if r.on else 'éteinte'}"
            log.warning("prise %s : essai %d/%d non confirmé — %s",
                        self.conf.nom, essai, tries, derniere)

        raise PlugCommandFailed(
            f"Home Assistant n'a pas confirmé l'ordre "
            f"« {'allumer' if on else 'couper'} » sur {self.conf.switch} "
            f"après {tries} essais : {derniere}")

    def turn_on(self) -> bool:
        self._guard()
        log.warning("prise %s : mise sous tension (via HA)", self.conf.nom)
        self._command(True)
        return True

    def turn_off(self) -> bool:
        self._guard()
        log.warning("prise %s : COUPURE (via HA)", self.conf.nom)
        self._command(False)
        return True
