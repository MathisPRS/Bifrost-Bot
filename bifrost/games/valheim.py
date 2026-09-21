"""Pilote Valheim — image ghcr.io/lloesche/valheim-server.

Tout ce qui suit a ete releve sur un serveur reel, pas suppose. Ce qui n'a pas
pu etre observe est explicitement marque comme tel.

Format de monde : Valheim n'utilise plus <monde>.db / <monde>.fwl mais un
DOSSIER worlds_local/<monde>/ contenant des *.chunk et quatre fichiers
_main.<generation>.{db2,fwl2,chunks,ok}. Le `.ok`, 4 octets, est ecrit en
dernier : c'est le temoin d'ecriture complete du jeu lui-meme.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import GameConf
from ..infra import net
from ..infra.dockerhost import DockerHost
from .base import BackupReport, Event, Presence, SaveInfo

log = logging.getLogger(__name__)

# --- motifs de logs, releves sur la sortie reelle -----------------------------

# "World save (1/5) Cloud & Backup checks done [0ms] => Save number 288"
#   -> la seule ligne qui atteste une sauvegarde TERMINEE. Les lignes
#      "FWL writing done" / "DB2 writing done" sont des etapes intermediaires.
RE_SAVE_DONE = re.compile(r"Cloud & Backup checks done.*?=>\s*Save number\s+(\d+)")

# " Connections 0 ZDOS:439016  sent:0 recv:0"  — cadence ~10 min : recoupement
# lent uniquement, jamais la source de la porte "0 joueur".
RE_CONNECTIONS = re.compile(r"\bConnections\s+(\d+)\s+ZDOS:")

# "Game server connected" / "Game server connected failed" — enregistrement
# aupres de PlayFab, donc visibilite dans la liste publique des serveurs.
RE_REGISTERED = re.compile(r"Game server connected(?!\s+failed)")
RE_REGISTER_FAIL = re.compile(r"Game server connected failed")

# NON OBSERVES a ce jour (aucun joueur ne s'est connecte depuis l'ouverture de
# l'acces). Motifs attendus, a confirmer a la premiere connexion reelle.
RE_JOIN = re.compile(r"Got connection SteamID\s+(\d+)")
RE_LEAVE = re.compile(r"Closing socket\s+(\d+)")
RE_CHARACTER = re.compile(r"Got character ZDOID from\s+(.+?)\s*:")


# Nom de fichier de generation : "_main.288.ok"
RE_GEN = re.compile(r"_main\.(\d+)\.(ok|db2|fwl2|chunks)$")


class ValheimDriver:
    def __init__(self, conf: GameConf, dh: DockerHost, vm_ip: str):
        self.conf = conf
        self.key = conf.key
        self.container = conf.container
        self._dh = dh
        self._vm_ip = vm_ip

    # --- etat ----------------------------------------------------------------

    def is_up(self) -> bool:
        """Le conteneur tourne ET le jeu repond sur son port de requete."""
        if not self._dh.state(self.container).running:
            return False
        return net.a2s_info(self._vm_ip, self.conf.query_port).ok

    def presence(self) -> Presence:
        """Source rapide : A2S. Les pseudos viennent des logs (l'A2S de Valheim
        ne les remplit pas de facon fiable)."""
        info = net.a2s_info(self._vm_ip, self.conf.query_port)
        if not info.ok:
            return Presence(known=False, source="a2s", error=info.error)
        return Presence(
            known=True, count=info.players or 0, source="a2s",
        )

    def presence_from_logs(self, lines: list[tuple[datetime, str]]) -> Presence:
        """Recoupement lent sur la derniere ligne `Connections N`."""
        for _at, line in reversed(lines):
            m = RE_CONNECTIONS.search(line)
            if m:
                return Presence(known=True, count=int(m.group(1)), source="logs")
        return Presence(known=False, source="logs", error="aucune ligne Connections")

    def registered(self, lines: list[tuple[datetime, str]]) -> bool | None:
        """Le serveur est-il enregistre dans la liste publique ?

        None = indetermine. Ce n'est PAS une porte : un serveur non enregistre
        reste joignable par IP directe.
        """
        for _at, line in reversed(lines):
            if RE_REGISTER_FAIL.search(line):
                return False
            if RE_REGISTERED.search(line):
                return True
        return None

    # --- sauvegardes ---------------------------------------------------------

    def last_save(self) -> SaveInfo:
        """Lu sur le systeme de fichiers, pas sur les logs.

        Les logs ne remontent qu'aussi loin que le conteneur courant ; les
        fichiers, eux, sont toujours la. On prend la generation la plus haute
        et on exige la presence de son marqueur .ok.

        Exige un conteneur EN MARCHE (exec). Conteneur arrete, c'est scan_local()
        sur la copie qui prend le relais.
        """
        # `exec` exige un conteneur en marche. Serveur eteint, ce n'est pas une
        # erreur : c'est l'etat normal. On le dit simplement, et /status se
        # rabat sur la derniere copie posee sur le NAS.
        try:
            if not self._dh.state(self.container).running:
                return SaveInfo(known=False, error="conteneur arrêté")
        except Exception as exc:          # noqa: BLE001
            return SaveInfo(known=False, error=f"VM injoignable ({type(exc).__name__})")

        cmd = ["sh", "-c", f'cd "{self.conf.world_dir}" 2>/dev/null && ls -1 _main.* 2>/dev/null | '
                           f'while read f; do echo "$(stat -c %Y "$f") $f"; done']
        try:
            code, out = self._dh.exec(self.container, cmd)
        except Exception as exc:          # noqa: BLE001
            return SaveInfo(known=False, error=f"{type(exc).__name__}: {exc}")
        if code != 0 or not out.strip():
            return SaveInfo(known=False, error="dossier de monde illisible")

        gens: dict[int, dict[str, int]] = {}
        for line in out.strip().splitlines():
            try:
                mtime_s, fname = line.split(" ", 1)
                m = RE_GEN.search(fname.strip())
                if not m:
                    continue
                gens.setdefault(int(m.group(1)), {})[m.group(2)] = int(mtime_s)
            except ValueError:
                continue
        if not gens:
            return SaveInfo(known=False, error="aucun fichier _main.<n>.*")

        gen = max(gens)
        parts = gens[gen]
        complete = "ok" in parts and "db2" in parts
        at = datetime.fromtimestamp(max(parts.values()), tz=timezone.utc)
        return SaveInfo(known=True, generation=gen, at=at, complete=complete)

    @staticmethod
    def scan_local(path: Path) -> SaveInfo:
        """Meme lecture, mais sur une copie deja posee sur le NAS.

        Indispensable : apres l'arret du conteneur on ne peut plus y faire
        d'exec. On verifie donc exactement les octets qu'on vient de copier,
        ce qui est de toute facon la seule chose qui compte.
        """
        gens: dict[int, dict[str, float]] = {}
        if not path.exists():
            return SaveInfo(known=False, error="copie absente")
        for f in path.iterdir():
            m = RE_GEN.search(f.name)
            if m and f.is_file():
                gens.setdefault(int(m.group(1)), {})[m.group(2)] = f.stat().st_mtime
        if not gens:
            return SaveInfo(known=False, error="aucun fichier _main.<n>.* dans la copie")
        gen = max(gens)
        parts = gens[gen]
        return SaveInfo(
            known=True, generation=gen,
            at=datetime.fromtimestamp(max(parts.values()), tz=timezone.utc),
            complete="ok" in parts and "db2" in parts,
        )

    def snapshot(self, dest_root: Path, min_generation: int | None = None) -> BackupReport:
        """Copie le dossier du monde vers le NAS, puis verifie la COPIE.

        Fonctionne conteneur arrete — get_archive lit le systeme de fichiers du
        conteneur, pas son processus. C'est meme le cas nominal : le jeu mort,
        il n'y a plus d'ecrivain concurrent ni de joueur possible.

        `min_generation` exige que la copie porte une generation au moins egale
        a celle relevee avant l'arret : c'est ce qui prouve que la sauvegarde de
        fermeture a bien eu lieu.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        tmp = dest_root / self.key / f".{stamp}.partiel"
        try:
            files, size = self._dh.fetch_dir(self.container, self.conf.world_dir, tmp)
        except Exception as exc:          # noqa: BLE001
            shutil.rmtree(tmp, ignore_errors=True)
            return BackupReport(ok=False, error=f"{type(exc).__name__}: {exc}")

        got = self.scan_local(tmp)
        checks = {
            "copie_lisible": got.known,
            "marqueur_ok": got.complete,
            "non_vide": size > 0,
            "generation_a_jour": (
                min_generation is None or (got.generation or -1) >= min_generation
            ),
        }

        # Comparaison avec la copie precedente : garde-fou contre un monde tronque.
        prev = sorted(q for q in (dest_root / self.key).glob("*-gen*") if q.is_dir())
        if prev:
            prev_size = sum(f.stat().st_size for f in prev[-1].rglob("*") if f.is_file())
            checks["taille_plausible"] = prev_size == 0 or size >= prev_size * 0.5
        else:
            checks["taille_plausible"] = True

        if not all(checks.values()):
            failed = [k for k, v in checks.items() if not v]
            shutil.rmtree(tmp, ignore_errors=True)   # on ne garde pas une copie douteuse
            log.error("snapshot %s rejete : %s", self.key, failed)
            return BackupReport(
                ok=False, files=files, bytes=size, generation=got.generation,
                checks=checks, error="controles en echec : " + ", ".join(failed),
            )

        # Renommage final : un dossier ne porte son nom definitif que verifie.
        dest = dest_root / self.key / f"{stamp}-gen{got.generation}"
        tmp.rename(dest)
        return BackupReport(ok=True, dest=dest, files=files, bytes=size,
                            generation=got.generation, checks=checks)

    def prune(self, dest_root: Path, keep_count: int, keep_days: int) -> int:
        """Rotation. Ne supprime jamais la copie la plus recente, quel que soit
        son age : mieux vaut une sauvegarde vieille que pas de sauvegarde."""
        d = dest_root / self.key
        if not d.exists():
            return 0
        copies = sorted((q for q in d.glob("*-gen*") if q.is_dir()),
                        key=lambda q: q.stat().st_mtime, reverse=True)
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
        removed = 0
        for i, q in enumerate(copies):
            if i == 0:
                continue
            if i >= keep_count or q.stat().st_mtime < cutoff:
                shutil.rmtree(q, ignore_errors=True)
                removed += 1
        for junk in d.glob(".*.partiel"):
            shutil.rmtree(junk, ignore_errors=True)
        return removed

    # --- actions -------------------------------------------------------------

    def stop(self, timeout_s: int | None = None):
        return self._dh.stop(self.container, timeout_s or self.conf.stop_timeout_s)

    def start(self):
        return self._dh.start(self.container)

    # --- logs ----------------------------------------------------------------

    def parse_log_line(self, line: str, at: datetime | None = None) -> Event | None:
        """`at` vient de Docker (UTC). L'horodatage interne du jeu est ignore :
        il est en heure locale du conteneur."""
        stamp = at or datetime.now(timezone.utc)
        m = RE_SAVE_DONE.search(line)
        if m:
            return Event(kind="save", at=stamp, generation=int(m.group(1)))
        m = RE_CHARACTER.search(line)
        if m:
            return Event(kind="join", at=stamp, player=m.group(1).strip())
        m = RE_JOIN.search(line)
        if m:
            return Event(kind="join", at=stamp, detail=m.group(1))
        m = RE_LEAVE.search(line)
        if m:
            return Event(kind="leave", at=stamp, detail=m.group(1))
        if RE_REGISTER_FAIL.search(line):
            return Event(kind="error", at=stamp, detail="enregistrement PlayFab echoue")
        if RE_REGISTERED.search(line):
            return Event(kind="connected", at=stamp)
        return None
