"""API Docker de la VM, sur transport SSH.

La cle SSH est verrouillee dans authorized_keys sur la seule commande
`docker system dial-stdio` : elle ne peut ni ouvrir de shell, ni rebondir, ni
faire du port-forwarding. Tout passe donc par l'API Docker — y compris la
recuperation du monde, via get_archive(), ce qui evite rsync et une 2e cle.
"""

from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import docker
from docker.errors import DockerException, NotFound

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContainerState:
    exists: bool
    status: str = "absent"          # running | exited | created | absent
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    health: str | None = None       # None si l'image n'a pas de healthcheck

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def uptime_s(self) -> int:
        if not self.running or not self.started_at:
            return 0
        return int((datetime.now(timezone.utc) - self.started_at).total_seconds())


def _ts(raw: str | None) -> datetime | None:
    if not raw or raw.startswith("0001-01-01"):
        return None
    # Docker rend des nanosecondes ; fromisoformat n'en veut que 6 chiffres.
    txt = raw.replace("Z", "+00:00")
    if "." in txt:
        head, _, tail = txt.partition(".")
        frac, sign, off = tail.partition("+")
        txt = f"{head}.{frac[:6]}{sign}{off}" if sign else f"{head}.{frac[:6]}"
    try:
        d = datetime.fromisoformat(txt)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class DockerHost:
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url
        self.timeout = timeout
        self._c: docker.DockerClient | None = None

    def _client(self) -> docker.DockerClient:
        if self._c is None:
            self._c = docker.DockerClient(
                base_url=self.base_url, timeout=self.timeout, use_ssh_client=True
            )
        return self._c

    def close(self) -> None:
        if self._c is not None:
            try:
                self._c.close()
            finally:
                self._c = None

    def reachable(self) -> bool:
        try:
            self._client().ping()
            return True
        except Exception:                 # noqa: BLE001
            self.close()
            return False

    def info(self) -> dict:
        return self._client().info()

    # --- etat ----------------------------------------------------------------

    def state(self, name: str) -> ContainerState:
        try:
            c = self._client().containers.get(name)
        except NotFound:
            return ContainerState(exists=False)
        except DockerException as exc:
            self.close()
            raise
        s = c.attrs.get("State", {})
        health = (s.get("Health") or {}).get("Status")
        return ContainerState(
            exists=True,
            status=s.get("Status", "unknown"),
            exit_code=s.get("ExitCode"),
            started_at=_ts(s.get("StartedAt")),
            finished_at=_ts(s.get("FinishedAt")),
            health=health,
        )

    # --- logs ----------------------------------------------------------------

    @staticmethod
    def _split_ts(line: str) -> tuple[datetime, str]:
        """Docker prefixe chaque ligne d'un RFC3339 en UTC. On s'appuie dessus
        plutot que sur l'horodatage interne du jeu, qui est en heure locale du
        conteneur : melanger les fuseaux dans une porte de verification, c'est
        comparer des sauvegardes qui n'existent pas."""
        head, sep, rest = line.partition(" ")
        d = _ts(head) if sep else None
        return (d or datetime.now(timezone.utc)), (rest if sep else line)

    def logs(self, name: str, since: datetime | None = None, tail: int = 2000) -> list[tuple[datetime, str]]:
        c = self._client().containers.get(name)
        raw = c.logs(since=since, tail=tail, timestamps=True)
        return [self._split_ts(l) for l in raw.decode("utf-8", "replace").splitlines() if l]

    def stream_logs(self, name: str, since: datetime | None = None) -> Iterator[tuple[datetime, str]]:
        """Flux pousse : les lignes arrivent au fil de l'ecriture.

        C'est ce flux — et non une requete periodique — qui alimente l'etat de
        presence des joueurs. Une interruption doit etre traitee comme une
        perte de connaissance, pas comme un silence.
        """
        c = self._client().containers.get(name)
        for chunk in c.logs(stream=True, follow=True, since=since, tail=0, timestamps=True):
            for line in chunk.decode("utf-8", "replace").splitlines():
                if line:
                    yield self._split_ts(line)

    # --- actions -------------------------------------------------------------

    def stop(self, name: str, timeout: int) -> ContainerState:
        log.warning("docker : arret de %s (timeout %ss)", name, timeout)
        self._client().containers.get(name).stop(timeout=timeout)
        return self.state(name)

    def start(self, name: str) -> ContainerState:
        log.warning("docker : demarrage de %s", name)
        self._client().containers.get(name).start()
        return self.state(name)

    def exec(self, name: str, cmd: list[str], timeout: int = 60) -> tuple[int, str]:
        code, out = self._client().containers.get(name).exec_run(cmd, demux=False)
        return code, out.decode("utf-8", "replace")

    # --- extraction de fichiers ---------------------------------------------

    def fetch_dir(self, name: str, src: str, dest: Path) -> tuple[int, int]:
        """Copie un dossier du conteneur vers `dest` (l'archive est deballee).

        Renvoie (nombre de fichiers, octets). Passe par l'API Docker : ni shell
        distant, ni rsync, ni seconde cle SSH.
        """
        c = self._client().containers.get(name)
        stream, _stat = c.get_archive(src)
        buf = io.BytesIO()
        for chunk in stream:
            buf.write(chunk)
        buf.seek(0)
        dest.mkdir(parents=True, exist_ok=True)
        n = total = 0
        with tarfile.open(fileobj=buf, mode="r|*") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                # Aplatissement : on retire le premier composant (nom du dossier
                # source) et on refuse toute sortie de `dest`.
                rel = Path(*Path(member.name).parts[1:]) if len(Path(member.name).parts) > 1 else Path(member.name)
                target = (dest / rel).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    log.error("chemin d'archive suspect ignore : %s", member.name)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                data = fh.read()
                target.write_bytes(data)
                n += 1
                total += len(data)
        return n, total
