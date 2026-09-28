"""Fabrique des clients de prise.

Le reste du code ne sait pas par quel chemin il parle a la prise : il recoit un
objet qui expose read / read_power / turn_on / turn_off, et c'est tout. Changer
de backend se fait ici et dans config.yaml, nulle part ailleurs.
"""

from __future__ import annotations

import logging

from ..config import Conf
from .haplug import HAPlug, HAPlugConf
from .homeassistant import HAClient
from .plug import PlugClient

log = logging.getLogger(__name__)


def build(conf: Conf) -> tuple[object, object | None]:
    """Rend (prise pilotable, prise protegee). La seconde peut etre None."""
    if conf.plug_backend == "ha":
        if not conf.ha_url or not conf.ha_token:
            raise RuntimeError(
                "backend « ha » demande mais HA_URL / HA_TOKEN manquent dans "
                "secrets.env")
        ha = HAClient(conf.ha_url, conf.ha_token, max_age_s=conf.plug_max_age_s)
        log.info("prises pilotees via Home Assistant (%s)", conf.ha_url)
        pilote = HAPlug(ha, HAPlugConf(nom="proxmox", **conf.ha_plug_proxmox))
        protegee = (HAPlug(ha, HAPlugConf(nom="nas", **conf.ha_plug_nas))
                    if conf.ha_plug_nas else None)
        return pilote, protegee

    # Repli historique : dialogue Tuya direct. Conserve comme echappatoire si
    # HA tombe, mais il reprend le conflit de session avec LocalTuya.
    log.warning("prises pilotees en Tuya direct — conflit possible avec "
                "LocalTuya si Home Assistant tourne")
    return (PlugClient(conf.plug, forbidden=conf.nas_plug),
            PlugClient(conf.nas_plug) if conf.nas_plug else None)
