"""Sonde de sante du conteneur.

Elle ne doit JAMAIS parler a la prise en direct. Le healthcheck tournait
toutes les 60 s en ouvrant une session directe vers l'appareil : une prise Tuya
n'acceptant qu'une session locale a la fois, il entrait en conflit avec celle de
LocalTuya et a fini par faire passer les entites de Home Assistant en
`unavailable` — pendant 33 h, sans que rien ne le signale.

On verifie donc ce qui est vrai du BOT, pas de l'appareil : la configuration se
charge, et le canal de pilotage repond. L'etat de la prise, lui, a sa place
dans /status.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from .config import load
        conf = load()
    except Exception as exc:                # noqa: BLE001
        print(f"configuration illisible : {exc}", file=sys.stderr)
        return 1

    from .infra.homeassistant import HAClient
    if not HAClient(conf.ha.url, conf.ha.token).reachable():
        print(f"Home Assistant injoignable ({conf.ha.url})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
