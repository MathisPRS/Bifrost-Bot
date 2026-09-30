"""Accès à Home Assistant par son API REST.

On ne lit pas l'appareil, on lit l'**état mémorisé** par Home Assistant. Deux
conséquences, toutes deux mesurées sur l'installation réelle :

**L'âge d'un état n'est pas un signal de fraîcheur.** LocalTuya ne sonde pas :
il n'émet qu'au changement. Une consommation stable laisse `last_reported`
vieillir de dizaines de minutes sans que la valeur soit fausse, et un
interrupteur allumé depuis deux heures a légitimement un horodatage de deux
heures. Ce module rapporte donc l'âge sans en juger, et laisse les portes
décider de la fraîcheur dont elles ont besoin — celle de l'extinction exige une
mesure postérieure au début de la séquence, ce qui est la définition même de la
chute qu'elle cherche à constater.

**Le vrai signal de panne, c'est `unavailable`**, que l'intégration pose quand
elle perd l'appareil. Il est traité comme inconnu, et déclenche une tentative de
remise en service : voir `_heal()`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# Ce que Home Assistant renvoie quand il ne sait pas. Jamais des valeurs.
INDISPONIBLES = {"unavailable", "unknown", "none", ""}


class HAError(RuntimeError):
    pass


@dataclass(frozen=True)
class HAState:
    ok: bool
    value: str | None = None
    at: datetime | None = None          # dernière remontée connue
    age_s: float | None = None
    error: str | None = None

    def as_float(self) -> float | None:
        try:
            return float(self.value)     # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 8.0,
                 max_age_s: int = 86400, heal_cooldown_s: int = 600):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        # Plafond de dernier recours : au-delà l'intégration est figée, pas
        # lente. Ce n'est PAS un contrôle de fraîcheur — voir l'en-tête.
        self.max_age_s = max_age_s
        self.heal_cooldown_s = heal_cooldown_s
        self._last_heal = 0.0
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def reachable(self) -> bool:
        try:
            return self._s.get(f"{self.base}/api/", timeout=self.timeout).status_code == 200
        except Exception:                 # noqa: BLE001
            return False

    # --- lecture -------------------------------------------------------------

    def state(self, entity_id: str, heal: bool = True) -> HAState:
        st = self._state(entity_id)
        # Entité perdue : on tente une remise en service, puis on relit une fois.
        if not st.ok and heal and st.error and "indisponible" in st.error:
            if self._heal(entity_id):
                st = self._state(entity_id)
        return st

    def _state(self, entity_id: str) -> HAState:
        try:
            r = self._s.get(f"{self.base}/api/states/{entity_id}", timeout=self.timeout)
        except Exception as exc:          # noqa: BLE001
            return HAState(ok=False, error=f"{type(exc).__name__}: {exc}")

        if r.status_code == 404:
            return HAState(ok=False, error=f"entité inconnue : {entity_id}")
        if r.status_code != 200:
            return HAState(ok=False, error=f"HTTP {r.status_code}")

        d = r.json()
        val = str(d.get("state", "")).strip()
        if val.lower() in INDISPONIBLES:
            return HAState(ok=False,
                           error=f"entité indisponible côté Home Assistant "
                                 f"(« {val or 'vide'} ») — l'intégration ne "
                                 "joint plus l'appareil")

        at = None
        # `last_reported` d'abord : il bouge à chaque remontée, même sans
        # changement de valeur. Les deux autres ne bougent qu'au changement.
        for champ in ("last_reported", "last_updated", "last_changed"):
            brut = d.get(champ)
            if not brut:
                continue
            try:
                at = datetime.fromisoformat(brut.replace("Z", "+00:00"))
                break
            except ValueError:
                continue
        if at is None:
            return HAState(ok=False, error="horodatage absent de la réponse")

        age = (datetime.now(timezone.utc) - at).total_seconds()
        if age > self.max_age_s:
            return HAState(ok=False, value=val, at=at, age_s=age,
                           error=f"état figé depuis {age / 3600:.0f} h")
        return HAState(ok=True, value=val, at=at, age_s=age)

    # --- écriture ------------------------------------------------------------

    def call(self, domain: str, service: str, entity_id: str) -> None:
        """Appelle un service. Lève si Home Assistant ne l'accepte pas.

        Un 200 dit seulement que l'appel est accepté, pas que l'appareil a
        obéi : la confirmation se fait par relecture, chez l'appelant.
        """
        try:
            r = self._s.post(f"{self.base}/api/services/{domain}/{service}",
                             json={"entity_id": entity_id}, timeout=self.timeout)
        except Exception as exc:          # noqa: BLE001
            raise HAError(f"{domain}.{service} sur {entity_id} : "
                          f"{type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            raise HAError(f"{domain}.{service} sur {entity_id} : "
                          f"HTTP {r.status_code} — {r.text[:160]}")

    def _heal(self, entity_id: str) -> bool:
        """Recharge l'intégration qui porte cette entité.

        Une intégration qui a perdu son appareil ne le retrouve pas toujours
        seule ; un rechargement la fait repartir. C'est la seule automatisation
        de remise en service du projet, et elle est bornée : au plus une
        tentative par `heal_cooldown_s`, parce qu'elle ne peut rien contre un
        appareil réellement absent du réseau et qu'insister n'aiderait pas.
        """
        maintenant = time.monotonic()
        if maintenant - self._last_heal < self.heal_cooldown_s:
            return False
        self._last_heal = maintenant
        log.warning("%s indisponible — rechargement de son intégration", entity_id)
        try:
            self.call("homeassistant", "reload_config_entry", entity_id)
        except HAError as exc:
            log.warning("rechargement refusé : %s", exc)
            return False
        time.sleep(5.0)                   # laisser l'intégration se rétablir
        return True
