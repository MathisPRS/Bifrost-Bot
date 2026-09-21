"""Prise Tuya, en LAN pur (aucun appel cloud).

Deux invariants portes par ce module, et pas ailleurs :

  1. Une lecture ratee n'est JAMAIS une valeur. Timeout, erreur reseau, DPS
     absent -> `PlugReading.ok is False`. Le code appelant doit traiter ce cas
     comme "inconnu", jamais comme "0 watt". Confondre les deux, c'est couper
     le courant d'une machine allumee parce que le wifi de la prise a hoquete.

  2. Une prise declaree read_only refuse toute ecriture, au niveau du client.
     C'est la prise qui alimente le NAS : le bot tourne dessus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import tinytuya

from ..config import PlugConf

log = logging.getLogger(__name__)

# Points de donnee releves sur l'appareil reel (cf. PLAN §3).
DPS_SWITCH = "1"
DPS_CURRENT_MA = "18"
DPS_POWER_DW = "19"       # dixiemes de watt
DPS_VOLTAGE_DV = "20"     # dixiemes de volt


class PlugWriteDenied(RuntimeError):
    """Tentative d'ecriture sur une prise declaree en lecture seule."""


class PlugCommandFailed(RuntimeError):
    """L'ordre n'a pas ete pris, et on sait le dire."""


@dataclass(frozen=True)
class PlugReading:
    ok: bool
    at: datetime
    on: bool | None = None
    watts: float | None = None
    volts: float | None = None
    milliamps: int | None = None
    error: str | None = None

    def __str__(self) -> str:
        if not self.ok:
            return f"lecture indisponible ({self.error})"
        etat = "allumee" if self.on else "eteinte"
        return f"{etat}, {self.watts:.1f} W, {self.volts:.1f} V, {self.milliamps} mA"


class PlugClient:
    def __init__(self, conf: PlugConf, timeout: float = 5.0):
        self.conf = conf
        self._timeout = timeout
        self._dev: tinytuya.OutletDevice | None = None

    def _device(self) -> tinytuya.OutletDevice:
        if self._dev is None:
            d = tinytuya.OutletDevice(
                dev_id=self.conf.dev_id,
                address=self.conf.ip,
                local_key=self.conf.local_key,
                version=self.conf.version,
            )
            d.set_socketTimeout(self._timeout)
            d.set_socketPersistent(True)
            self._dev = d
        return self._dev

    def _reset(self) -> None:
        # Une session persistante peut rester coincee apres une coupure reseau.
        self._dev = None

    def read(self) -> PlugReading:
        now = datetime.now(timezone.utc)
        try:
            status = self._device().status()
        except Exception as exc:                       # noqa: BLE001 — tout est "inconnu"
            self._reset()
            return PlugReading(ok=False, at=now, error=f"{type(exc).__name__}: {exc}")

        if not isinstance(status, dict) or "dps" not in status:
            self._reset()
            err = (status or {}).get("Error", "reponse sans dps") if isinstance(status, dict) else "reponse invalide"
            return PlugReading(ok=False, at=now, error=str(err))

        dps = status["dps"]
        # Le compteur d'energie peut manquer sur certains cycles : on n'exige
        # que l'interrupteur, et on laisse les mesures a None si absentes.
        if DPS_SWITCH not in dps:
            return PlugReading(ok=False, at=now, error="DPS 1 absent de la reponse")

        def tenth(key: str) -> float | None:
            v = dps.get(key)
            return round(v / 10.0, 1) if isinstance(v, (int, float)) else None

        return PlugReading(
            ok=True,
            at=now,
            on=bool(dps[DPS_SWITCH]),
            watts=tenth(DPS_POWER_DW),
            volts=tenth(DPS_VOLTAGE_DV),
            milliamps=dps.get(DPS_CURRENT_MA),
        )

    def read_power(self) -> float | None:
        """Watts, ou None si la lecture a echoue ou si le DPS est absent.

        None signifie "je ne sais pas" et doit remettre a zero tout compteur de
        maintien. Il ne signifie jamais "zero watt".
        """
        r = self.read()
        return r.watts if r.ok else None

    # --- ecritures -----------------------------------------------------------

    def _guard(self) -> None:
        if self.conf.read_only:
            raise PlugWriteDenied(
                f"prise {self.conf.dev_id} declaree en lecture seule "
                "(c'est celle qui alimente le NAS)"
            )

    def _command(self, on: bool, tries: int = 2) -> None:
        """Envoie l'ordre et EXIGE une confirmation.

        Deux lecons payees le 2026-09-20 :

        1. tinytuya ne leve pas d'exception quand un ordre echoue : il renvoie
           un dictionnaire contenant « Error »/« Err ». Ignorer cette valeur de
           retour, c'est perdre silencieusement une commande — le bot croit
           avoir allume la prise, et seule la porte suivante constate que rien
           n'a bouge, sans pouvoir dire pourquoi.
        2. Le socket persistant sert les lectures frequentes des portes, mais sa
           session peut expirer. Les lectures se rattrapent toutes seules, pas
           les commandes. On repart donc d'une connexion NEUVE avant chaque
           ordre : ils sont rares (quelques-uns par jour), la poignee de main ne
           coute rien a cote d'une commande perdue.
        """
        derniere = ""
        for essai in range(1, tries + 1):
            self._reset()                       # session fraiche, obligatoire
            dev = self._device()
            try:
                res = dev.turn_on() if on else dev.turn_off()
            except Exception as exc:            # noqa: BLE001
                derniere = f"{type(exc).__name__}: {exc}"
                log.warning("prise %s : essai %d/%d en erreur — %s",
                            self.conf.ip, essai, tries, derniere)
                continue

            if isinstance(res, dict) and (res.get("Error") or res.get("Err")):
                derniere = f"{res.get('Err', '?')} {res.get('Error', '')}".strip()
                log.warning("prise %s : essai %d/%d refuse — %s",
                            self.conf.ip, essai, tries, derniere)
                continue

            # L'appareil a accepte : on verifie quand meme l'etat reel.
            r = self.read()
            if r.ok and r.on is on:
                return
            derniere = (f"ordre accepte mais etat toujours "
                        f"{'eteinte' if r.on is False else 'inconnu'}"
                        if r.ok else f"relecture impossible ({r.error})")
            log.warning("prise %s : essai %d/%d — %s", self.conf.ip, essai, tries, derniere)

        raise PlugCommandFailed(
            f"la prise {self.conf.ip} n'a pas pris l'ordre "
            f"« {'allumer' if on else 'couper'} » apres {tries} essais : {derniere}")

    def turn_on(self) -> bool:
        self._guard()
        log.warning("prise %s : mise sous tension", self.conf.ip)
        self._command(True)
        return True

    def turn_off(self) -> bool:
        self._guard()
        log.warning("prise %s : COUPURE", self.conf.ip)
        self._command(False)
        return True
