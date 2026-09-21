"""API Proxmox via token dedie.

Le token porte le role BifrostCtl : VM.Audit, VM.PowerMgmt, Sys.Audit,
Sys.PowerMgmt. Il peut interroger, eteindre et demarrer. Il ne peut ni detruire
une VM, ni ouvrir un shell — verifie : une creation de VM renvoie 403.

`reachable()` sert de sonde d'extinction de l'hote : tant que l'API repond,
l'hote tourne. Combinee au ping, elle donne deux signaux independants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
import urllib3

from ..config import ProxmoxConf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VmStatus:
    vmid: int
    name: str
    status: str            # running | stopped | paused
    uptime_s: int
    maxmem_mb: int

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass(frozen=True)
class NodeStatus:
    uptime_s: int
    cpu_pct: float
    mem_used_mb: int
    mem_total_mb: int
    cpu_temp_c: float | None = None


class ProxmoxClient:
    def __init__(self, conf: ProxmoxConf, timeout: float = 8.0):
        self.conf = conf
        self.timeout = timeout
        self.base = f"https://{conf.host}:8006/api2/json"
        self._s = requests.Session()
        self._s.headers["Authorization"] = (
            f"PVEAPIToken={conf.token_id}={conf.token_secret}"
        )
        self._s.verify = conf.verify_ssl
        if not conf.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get(self, path: str) -> dict | list:
        r = self._s.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def _post(self, path: str, **data) -> str:
        r = self._s.post(f"{self.base}{path}", data=data, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]          # UPID de la tache

    # --- lectures ------------------------------------------------------------

    def reachable(self) -> bool:
        """Vrai si l'hote repond. Toute exception vaut "non joignable"."""
        try:
            self._get("/version")
            return True
        except Exception:                 # noqa: BLE001
            return False

    def node_status(self) -> NodeStatus:
        d = self._get(f"/nodes/{self.conf.node}/status")
        mem = d.get("memory", {})
        return NodeStatus(
            uptime_s=int(d.get("uptime", 0)),
            cpu_pct=round(float(d.get("cpu", 0.0)) * 100, 1),
            mem_used_mb=int(mem.get("used", 0)) // 1048576,
            mem_total_mb=int(mem.get("total", 0)) // 1048576,
            cpu_temp_c=self._cpu_temp(),
        )

    def _cpu_temp(self) -> float | None:
        # Disponible seulement si lm-sensors est installe cote hote ; le node
        # exporter sur :9100 reste la source de secours.
        try:
            d = self._get(f"/nodes/{self.conf.node}/status")
            pkg = (d.get("thermalstate") or {}).get("cputemp")
            return float(pkg) if pkg is not None else None
        except Exception:                 # noqa: BLE001
            return None

    def vms(self) -> dict[int, VmStatus]:
        out = {}
        for v in self._get(f"/nodes/{self.conf.node}/qemu"):
            out[int(v["vmid"])] = VmStatus(
                vmid=int(v["vmid"]),
                name=v.get("name", "?"),
                status=v.get("status", "unknown"),
                uptime_s=int(v.get("uptime", 0)),
                maxmem_mb=int(v.get("maxmem", 0)) // 1048576,
            )
        return out

    def vm(self, vmid: int) -> VmStatus | None:
        return self.vms().get(vmid)

    # --- ecritures -----------------------------------------------------------

    def vm_start(self, vmid: int) -> str:
        log.warning("proxmox : demarrage VM %s", vmid)
        return self._post(f"/nodes/{self.conf.node}/qemu/{vmid}/status/start")

    def vm_shutdown(self, vmid: int, timeout_s: int = 300) -> str:
        """Arret ACPI propre. `forceStop` reste volontairement absent : on ne
        veut pas d'un arret brutal declenche automatiquement."""
        log.warning("proxmox : arret VM %s (timeout %ss)", vmid, timeout_s)
        return self._post(
            f"/nodes/{self.conf.node}/qemu/{vmid}/status/shutdown", timeout=timeout_s
        )

    def node_shutdown(self) -> str:
        log.warning("proxmox : ARRET DE L'HOTE %s", self.conf.node)
        return self._post(f"/nodes/{self.conf.node}/status", command="shutdown")
