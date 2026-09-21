"""Sondes reseau : ping et requete A2S.

L'A2S est le signal de presence RAPIDE. La ligne `Connections N` des logs
Valheim ne tombe que toutes les ~10 minutes : elle sert de recoupement lent,
jamais de source pour la porte "0 joueur".
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass

import a2s


def ping(host: str, timeout_s: int = 2) -> bool:
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout_s), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_s + 2,
            env={**os.environ, "LC_ALL": "C"},
        )
        return r.returncode == 0
    except Exception:                     # noqa: BLE001
        return False


@dataclass(frozen=True)
class A2SInfo:
    ok: bool
    name: str = ""
    players: int | None = None
    max_players: int | None = None
    error: str | None = None


def a2s_info(host: str, port: int, timeout: float = 3.0) -> A2SInfo:
    """Interroge le port de requete Steam.

    Chez Valheim c'est le port de jeu + 1 (2457), pas le port de jeu.
    Un echec renvoie ok=False : "je ne sais pas", jamais "0 joueur".
    """
    try:
        info = a2s.info((host, port), timeout=timeout)
        return A2SInfo(
            ok=True,
            name=getattr(info, "server_name", "") or "",
            players=int(getattr(info, "player_count", 0)),
            max_players=int(getattr(info, "max_players", 0)),
        )
    except Exception as exc:              # noqa: BLE001
        return A2SInfo(ok=False, error=f"{type(exc).__name__}: {exc}")


def wake_on_lan(mac: str, broadcast: str = "255.255.255.255", port: int = 9,
                repeat: int = 3) -> bool:
    """Envoie un paquet magique.

    Necessaire des que la prise est DEJA allumee : la machine est alors en S5
    avec le courant present, et « Restore on AC Power Loss » ne se declenche
    qu'au retour du courant, jamais sur sa presence. Sans WoL, une machine
    eteinte sans coupure de prise serait irreveillable a distance.
    """
    raw = mac.replace(":", "").replace("-", "").strip()
    if len(raw) != 12:
        raise ValueError(f"adresse MAC invalide : {mac}")
    packet = b"\xff" * 6 + bytes.fromhex(raw) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            s.sendto(packet, (broadcast, port))
        return True
    finally:
        s.close()
