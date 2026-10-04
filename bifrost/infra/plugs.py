"""Construction des prises à partir de la configuration."""

from __future__ import annotations

import logging

from ..config import Conf
from .haplug import HAPlug, PlugEntities
from .homeassistant import HAClient

log = logging.getLogger(__name__)


def build(conf: Conf) -> tuple[HAPlug, HAPlug | None]:
    """Rend (prise pilotable, prise protégée). La seconde peut être absente."""
    ha = HAClient(conf.ha.url, conf.ha.token)
    log.info("prises pilotées via Home Assistant (%s)", conf.ha.url)
    pilotee = HAPlug(ha, PlugEntities(nom="proxmox", **conf.ha.proxmox))
    protegee = (HAPlug(ha, PlugEntities(nom="nas", **conf.ha.nas))
                if conf.ha.nas else None)
    return pilotee, protegee
