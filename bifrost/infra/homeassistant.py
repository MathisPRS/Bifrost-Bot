"""Pilotage de la prise via Home Assistant, et non plus en Tuya direct.

POURQUOI. Un appareil Tuya n'accepte en pratique qu'UNE session de controle
locale a la fois. HA en tient une en permanence via LocalTuya ; Bifrost en
ouvrait une seconde avec tinytuya. Les deux se disputaient le meme socket, ce
qui explique toute la serie de pannes de septembre : lectures courtes qui
passent, commandes perdues en silence, premiere lecture qui rate apres une
periode creuse, erreur 914 sans logique apparente.

Passer par HA supprime le conflit au lieu de le contourner : un seul
programme parle au materiel, et c'est celui qui sait tenir la connexion.

CE QUE CA CHANGE DANS LES RISQUES. On ne lit plus l'appareil, on lit l'ETAT
MEMORISE par HA. Une valeur peut donc etre perimee : « 0 W » vieux de dix
minutes ressemble a une machine eteinte alors qu'elle tourne peut-etre encore.

Mesure du 2026-09-28 : LocalTuya ne SONDE pas, il attend que l'appareil signale
un changement. Une consommation stable a 27,5 W ne produit aucun evenement, et
meme `last_reported` affichait 39 minutes. Un simple seuil d'age est donc
inexploitable : il declarerait perimee une valeur parfaitement juste.

Ce module rapporte donc l'AGE de chaque etat sans en juger, et laisse les portes
decider de la fraicheur dont elles ont besoin. Celle de l'extinction exige que
la valeur ait ete rafraichie APRES le debut de la sequence — ce qui est la
definition meme de la chute qu'on cherche a prouver, et ce qui reste vrai meme
si l'integration se fige.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

# Etats que HA renvoie quand il ne sait pas. Ce ne sont JAMAIS des valeurs.
INCONNUS = {"unavailable", "unknown", "none", ""}


class HAError(RuntimeError):
    pass


@dataclass(frozen=True)
class HAState:
    ok: bool
    value: str | None = None
    at: datetime | None = None          # last_updated cote HA
    age_s: float | None = None
    error: str | None = None

    def as_float(self) -> float | None:
        try:
            return float(self.value)     # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = 8.0,
                 max_age_s: int = 3600):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        # Plafond de bon sens : au-dela, l'integration est figee, pas lente.
        self.max_age_s = max_age_s
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def reachable(self) -> bool:
        try:
            r = self._s.get(f"{self.base}/api/", timeout=self.timeout)
            return r.status_code == 200
        except Exception:                 # noqa: BLE001
            return False

    def state(self, entity_id: str) -> HAState:
        """Etat d'une entite, avec son age. Perime == inconnu."""
        try:
            r = self._s.get(f"{self.base}/api/states/{entity_id}",
                            timeout=self.timeout)
        except Exception as exc:          # noqa: BLE001
            return HAState(ok=False, error=f"{type(exc).__name__}: {exc}")

        if r.status_code == 404:
            return HAState(ok=False, error=f"entité inconnue : {entity_id}")
        if r.status_code != 200:
            return HAState(ok=False, error=f"HTTP {r.status_code}")

        d = r.json()
        val = str(d.get("state", "")).strip()
        if val.lower() in INCONNUS:
            return HAState(ok=False, error=f"état « {val or 'vide'} » côté HA")

        at = None
        # `last_reported` d'abord : il bouge a chaque remontee, meme sans
        # changement de valeur. Les deux autres ne bougent qu'au changement.
        for champ in ("last_reported", "last_updated", "last_changed"):
            brut = d.get(champ)
            if brut:
                try:
                    at = datetime.fromisoformat(brut.replace("Z", "+00:00"))
                    break
                except ValueError:
                    continue
        if at is None:
            return HAState(ok=False, error="horodatage absent de la réponse HA")

        age = (datetime.now(timezone.utc) - at).total_seconds()
        if age > self.max_age_s:
            return HAState(ok=False, value=val, at=at, age_s=age,
                           error=f"état figé depuis {age / 60:.0f} min — "
                                 "l'intégration ne remonte plus rien")
        # L'age est rendu tel quel : c'est a l'appelant d'exiger la fraicheur
        # dont il a besoin.
        return HAState(ok=True, value=val, at=at, age_s=age)

    def call(self, domain: str, service: str, entity_id: str) -> None:
        """Appelle un service. Leve si HA ne confirme pas."""
        try:
            r = self._s.post(
                f"{self.base}/api/services/{domain}/{service}",
                json={"entity_id": entity_id}, timeout=self.timeout)
        except Exception as exc:          # noqa: BLE001
            raise HAError(f"{domain}.{service} sur {entity_id} : "
                          f"{type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            raise HAError(f"{domain}.{service} sur {entity_id} : "
                          f"HTTP {r.status_code} — {r.text[:160]}")
