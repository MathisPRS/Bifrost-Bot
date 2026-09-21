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
import socket
from concurrent.futures import ThreadPoolExecutor
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


class PlugIdentityError(RuntimeError):
    """L'appareil joignable a cette adresse n'est pas celui qu'on croit."""


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
    """Client d'une prise, identifiee par sa CLE et non par son adresse.

    Le 2026-09-21, les deux prises du reseau ont echange d'adresse en DHCP. La
    configuration pointait donc l'ordre de coupure sur la prise du NAS — celle
    qui alimente la machine ou tourne ce bot. Seul le fait que la cle locale
    Tuya soit propre a chaque appareil a empeche la coupure : la prise a
    rejete la cle avec une erreur 914.

    On ne depend donc plus de l'adresse. `conf.ip` n'est qu'un POINT DE DEPART :
    si l'appareil qui y repond n'accepte pas notre cle, on balaie le /24 pour
    retrouver celui qui l'accepte. Une cle qui dechiffre EST la preuve
    d'identite — elle est unique par appareil.
    """

    def __init__(self, conf: PlugConf, timeout: float = 5.0,
                 forbidden: PlugConf | None = None):
        self.conf = conf
        self._ip = conf.ip              # adresse courante, peut changer
        self._timeout = timeout
        # Prise a NE JAMAIS commander (celle du NAS). Sert de controle explicite
        # avant toute ecriture, pour echouer bruyamment plutot que par hasard.
        self._forbidden = forbidden
        self._dev: tinytuya.OutletDevice | None = None

    # --- resolution d'adresse ------------------------------------------------

    def _reachable_plugs(self) -> list[str]:
        """Adresses du /24 qui ecoutent sur le port Tuya local."""
        base = self._ip.rsplit(".", 1)[0]

        def probe(n: int) -> str | None:
            ip = f"{base}.{n}"
            s = socket.socket()
            s.settimeout(0.6)
            try:
                s.connect((ip, 6668))
                return ip
            except OSError:
                return None
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=64) as pool:
            return [ip for ip in pool.map(probe, range(1, 255)) if ip]

    def _accepts_our_key(self, ip: str) -> bool:
        d = tinytuya.OutletDevice(dev_id=self.conf.dev_id, address=ip,
                                  local_key=self.conf.local_key,
                                  version=self.conf.version)
        d.set_socketTimeout(self._timeout)
        try:
            st = d.status()
        except Exception:                 # noqa: BLE001
            return False
        return isinstance(st, dict) and "dps" in st

    def resolve(self) -> str | None:
        """Retrouve l'adresse de NOTRE appareil. None si introuvable."""
        if self._accepts_our_key(self._ip):
            return self._ip
        log.warning("prise %s : l'appareil a cette adresse n'accepte pas notre "
                    "cle — recherche sur le reseau", self._ip)
        for ip in self._reachable_plugs():
            if ip == self._ip:
                continue
            if self._accepts_our_key(ip):
                log.warning("prise %s retrouvee a l'adresse %s (l'adresse a change)",
                            self.conf.dev_id[:8], ip)
                self._ip = ip
                self._reset()
                return ip
        log.error("prise %s introuvable sur le reseau", self.conf.dev_id[:8])
        return None

    def _device(self) -> tinytuya.OutletDevice:
        if self._dev is None:
            d = tinytuya.OutletDevice(
                dev_id=self.conf.dev_id,
                address=self._ip,
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

    def read(self, _retry: bool = True) -> PlugReading:
        now = datetime.now(timezone.utc)
        try:
            status = self._device().status()
        except Exception as exc:                       # noqa: BLE001 — tout est "inconnu"
            self._reset()
            return PlugReading(ok=False, at=now, error=f"{type(exc).__name__}: {exc}")

        if not isinstance(status, dict) or "dps" not in status:
            self._reset()
            err = (status or {}).get("Error", "reponse sans dps") if isinstance(status, dict) else "reponse invalide"
            # Cle refusee -> ce n'est probablement plus notre appareil a cette
            # adresse. On tente une resolution, une seule fois.
            if _retry and "key or version" in str(err).lower():
                if self.resolve() is not None:
                    return self.read(_retry=False)
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

        # L'appareil joignable a cette adresse est-il bien le NOTRE ?
        # Une cle qui dechiffre est une preuve d'identite : elle est unique par
        # appareil. On l'exige AVANT d'ecrire, jamais apres.
        if self.resolve() is None:
            raise PlugIdentityError(
                f"prise {self.conf.dev_id[:8]}… introuvable sur le reseau : "
                "aucun appareil n'accepte sa cle. Rien n'a ete commande.")

        # Et surtout : ce n'est pas la prise interdite. Controle redondant avec
        # le precedent, et c'est voulu — il coute une requete et il transforme
        # un accident silencieux en refus explicite.
        if self._forbidden is not None:
            d = tinytuya.OutletDevice(dev_id=self._forbidden.dev_id, address=self._ip,
                                      local_key=self._forbidden.local_key,
                                      version=self._forbidden.version)
            d.set_socketTimeout(self._timeout)
            try:
                st = d.status()
            except Exception:             # noqa: BLE001
                st = None
            if isinstance(st, dict) and "dps" in st:
                raise PlugIdentityError(
                    f"REFUS : l'appareil en {self._ip} repond a la cle de la prise "
                    "protegee (celle du NAS). Les adresses ont probablement change "
                    "en DHCP. Rien n'a ete commande.")

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
