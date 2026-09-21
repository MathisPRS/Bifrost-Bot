"""Fabrique de pilotes de jeu.

`core/` ne connait que le protocole GameDriver ; c'est ici, et nulle part
ailleurs, qu'on fait le lien entre le champ `driver:` de config.yaml et une
classe. Ajouter un jeu = un fichier dans games/ + une ligne dans ce tableau.
"""

from __future__ import annotations

from ..config import GameConf
from ..infra.dockerhost import DockerHost
from .valheim import ValheimDriver

DRIVERS = {
    "valheim": ValheimDriver,
}


class UnknownDriver(RuntimeError):
    pass


def build(conf: GameConf, dh: DockerHost, vm_ip: str):
    cls = DRIVERS.get(conf.driver)
    if cls is None:
        raise UnknownDriver(
            f"jeu « {conf.key} » : pilote « {conf.driver} » inconnu. "
            f"Pilotes disponibles : {', '.join(sorted(DRIVERS))}. "
            "Un jeu declare mais sans pilote doit rester enabled: false.")
    return cls(conf, dh, vm_ip)
