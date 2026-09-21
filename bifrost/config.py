"""Chargement et validation de la configuration.

Deux sources, volontairement separees :
  - secrets.env  : identifiants, jamais versionne, mode 600
  - config.yaml  : seuils, timeouts, declaration des jeux — versionnable

Toute valeur manquante leve a l'import plutot qu'au premier appel : on veut
savoir qu'il manque un token au demarrage du bot, pas au milieu d'une sequence
d'extinction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def _req(src: dict, key: str) -> str:
    val = (src.get(key) or "").strip()
    if not val:
        raise ConfigError(f"{key} est vide ou absent de secrets.env")
    return val


@dataclass(frozen=True)
class PlugConf:
    ip: str
    dev_id: str
    local_key: str
    version: float
    # Verrou structurel : la prise du NAS est declaree en lecture seule ici, et
    # PlugClient refuse toute ecriture sur une instance read_only.
    read_only: bool = False


@dataclass(frozen=True)
class ProxmoxConf:
    host: str
    token_id: str
    token_secret: str
    node: str
    vmid: int
    other_vmids: tuple[int, ...]
    verify_ssl: bool
    mac: str = ""   # Wake-on-LAN


@dataclass(frozen=True)
class GameConf:
    key: str
    driver: str
    enabled: bool
    container: str
    world: str = ""
    world_dir: str = ""
    game_port: int = 0
    query_port: int = 0
    stop_timeout_s: int = 180


@dataclass(frozen=True)
class PowerConf:
    allow_cut: bool
    nas_on_separate_plug: bool
    off_threshold_w: float
    on_threshold_w: float
    floor_min_w: float
    require_drop: bool
    off_hold_s: int
    on_hold_s: int
    settle_after_off_s: int

    def cut_allowed(self) -> tuple[bool, str]:
        """La coupure est doublement verrouillee : le drapeau explicite ET la
        confirmation que le NAS ne depend plus de cette prise."""
        if not self.nas_on_separate_plug:
            return False, "le NAS n'est pas declare sur une prise separee"
        if not self.allow_cut:
            return False, "allow_cut est desactive dans config.yaml"
        return True, ""


@dataclass(frozen=True)
class DiscordConf:
    token: str
    guild_id: int
    channel_id: int
    role_id: int          # role par defaut, surchargeable par jeu via /allowrole
    # Role habilite a CHANGER les roles. Volontairement hors base : s'il etait
    # lui-meme modifiable par /allowrole, un seul mauvais appel suffirait a
    # verrouiller tout le monde dehors. Il ne se change qu'en editant ce fichier.
    admin_role_id: int = 0


@dataclass(frozen=True)
class Conf:
    plug: PlugConf
    nas_plug: PlugConf | None
    proxmox: ProxmoxConf
    power: PowerConf
    host_ip: str
    host_down_hold_s: int
    vm_ip: str
    docker_host: str
    backup_dest: Path
    backup_keep_count: int
    backup_keep_days: int
    backup_min_size_ratio: float
    discord: DiscordConf | None = None
    games: dict[str, GameConf] = field(default_factory=dict)
    status_refresh_s: int = 60
    countdown_s: int = 60

    @property
    def enabled_games(self) -> list[GameConf]:
        return [g for g in self.games.values() if g.enabled]


def load(root: Path = ROOT) -> Conf:
    env = {**dotenv_values(root / "secrets.env"), **os.environ}
    with open(root / "config.yaml", encoding="utf-8") as fh:
        y = yaml.safe_load(fh)

    plug = PlugConf(
        ip=_req(env, "TUYA_PLUG_IP"),
        dev_id=_req(env, "TUYA_PLUG_ID"),
        local_key=_req(env, "TUYA_PLUG_LOCAL_KEY"),
        version=float(env.get("TUYA_PLUG_VERSION") or 3.5),
    )

    # La prise du NAS n'est chargee que si son IP est connue, et toujours en
    # lecture seule : le bot tourne dessus.
    nas_ip = (env.get("TUYA_NAS_PLUG_IP") or "").strip()
    nas_plug = (
        PlugConf(
            ip=nas_ip,
            dev_id=_req(env, "TUYA_NAS_PLUG_ID"),
            local_key=_req(env, "TUYA_NAS_PLUG_LOCAL_KEY"),
            version=float(env.get("TUYA_NAS_PLUG_VERSION") or 3.5),
            read_only=True,
        )
        if nas_ip
        else None
    )

    if plug.dev_id == (nas_plug.dev_id if nas_plug else None):
        raise ConfigError("la prise pilotee et la prise du NAS ont le meme dev_id")

    px = y["proxmox"]
    proxmox = ProxmoxConf(
        host=_req(env, "PROXMOX_HOST"),
        token_id=_req(env, "PROXMOX_TOKEN_ID"),
        token_secret=_req(env, "PROXMOX_TOKEN_SECRET"),
        node=px["node"],
        vmid=int(px["vmid"]),
        other_vmids=tuple(int(v) for v in px.get("other_vmids", [])),
        verify_ssl=(env.get("PROXMOX_VERIFY_SSL", "false").lower() == "true"),
        mac=(env.get("PROXMOX_MAC") or "").strip(),
    )

    p = y["power"]
    power = PowerConf(
        allow_cut=bool(p["allow_cut"]),
        nas_on_separate_plug=bool(p["nas_on_separate_plug"]),
        off_threshold_w=float(p["off_threshold_w"]),
        on_threshold_w=float(p["on_threshold_w"]),
        floor_min_w=float(p.get("floor_min_w", 0.0)),
        require_drop=bool(p.get("require_drop", True)),
        off_hold_s=int(p["off_hold_s"]),
        on_hold_s=int(p["on_hold_s"]),
        settle_after_off_s=int(p["settle_after_off_s"]),
    )

    games = {}
    for key, g in (y.get("games") or {}).items():
        games[key] = GameConf(
            key=key,
            driver=g["driver"],
            enabled=bool(g.get("enabled", False)),
            container=g["container"],
            world=g.get("world", ""),
            world_dir=g.get("world_dir", ""),
            game_port=int(g.get("game_port", 0)),
            query_port=int(g.get("query_port", 0)),
            stop_timeout_s=int(g.get("stop_timeout_s", 180)),
        )

    # Discord n'est exige que par le bot ; la CLI lecture seule s'en passe.
    try:
        dconf = DiscordConf(
            token=_req(env, "DISCORD_BOT_TOKEN"),
            guild_id=int(_req(env, "DISCORD_GUILD_ID")),
            channel_id=int(_req(env, "DISCORD_CHANNEL_ID")),
            role_id=int(_req(env, "DISCORD_ALLOWED_ROLE")),
            admin_role_id=int((env.get("DISCORD_ADMIN_ROLE") or "0").strip() or 0),
        )
    except (ConfigError, ValueError):
        dconf = None

    b = y["backup"]
    dest = Path(b["dest"])
    if not (dest.is_absolute() and dest.parent.exists()):
        dest = root / "data" / "worlds"      # hors conteneur
    dest.mkdir(parents=True, exist_ok=True)

    return Conf(
        plug=plug,
        nas_plug=nas_plug,
        proxmox=proxmox,
        power=power,
        host_ip=y["host"]["ip"],
        host_down_hold_s=int(y["host"]["down_hold_s"]),
        vm_ip=y["vm"]["ip"],
        docker_host=y["vm"]["docker_host"],
        backup_dest=dest,
        backup_keep_count=int(b["keep_count"]),
        backup_keep_days=int(b["keep_days"]),
        backup_min_size_ratio=float(b["min_size_ratio"]),
        discord=dconf,
        games=games,
        status_refresh_s=int(y["discord"]["status_refresh_s"]),
        countdown_s=int(y["discord"]["countdown_s"]),
    )
