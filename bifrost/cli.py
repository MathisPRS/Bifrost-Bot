"""Bifrost en ligne de commande — phase 1, LECTURE SEULE.

Aucune sous-commande de ce module ne modifie quoi que ce soit : ni prise, ni VM,
ni conteneur. Le chemin de coupure n'existe pas encore dans le code.

    ./.venv/bin/python -m bifrost.cli status
    ./.venv/bin/python -m bifrost.cli power --watch 60
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from .config import load
from .games.valheim import ValheimDriver
from .infra import net
from .infra.dockerhost import DockerHost
from .infra.plug import PlugClient
from .infra.proxmox import ProxmoxClient

OK, KO, UNK = "\033[32m●\033[0m", "\033[31m●\033[0m", "\033[33m●\033[0m"


def dur(s: float | int | None) -> str:
    if s is None:
        return "?"
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d}"
    return f"{s // 86400} j {(s % 86400) // 3600} h"


def cmd_status(conf) -> int:
    now = datetime.now(timezone.utc)
    print(f"\n\033[1mBifrost — etat au {now.astimezone():%H:%M:%S}\033[0m\n")

    # --- prise ---------------------------------------------------------------
    plug = PlugClient(conf.plug, forbidden=conf.nas_plug)
    r = plug.read()
    mark = OK if r.ok else UNK
    print(f"  {mark} \033[1mPrise\033[0m {conf.plug.ip}   {r}")
    if r.ok and r.watts is not None:
        allowed, why = conf.power.cut_allowed()
        verrou = "coupure AUTORISEE" if allowed else f"coupure verrouillee — {why}"
        print(f"      seuils : eteint < {conf.power.off_threshold_w} W · "
              f"allume > {conf.power.on_threshold_w} W   [{verrou}]")

    # --- hote ----------------------------------------------------------------
    px = ProxmoxClient(conf.proxmox)
    host_ping = net.ping(conf.host_ip)
    host_api = px.reachable()
    mark = OK if (host_ping and host_api) else (UNK if host_ping or host_api else KO)
    print(f"\n  {mark} \033[1mProxmox\033[0m {conf.host_ip}   "
          f"ping {'OK' if host_ping else 'KO'} · api {'OK' if host_api else 'KO'}")

    vms = {}
    if host_api:
        try:
            ns = px.node_status()
            temp = f" · {ns.cpu_temp_c:.0f} °C" if ns.cpu_temp_c else ""
            print(f"      uptime {dur(ns.uptime_s)} · cpu {ns.cpu_pct} % · "
                  f"ram {ns.mem_used_mb} / {ns.mem_total_mb} Mo{temp}")
            vms = px.vms()
            for vmid in (conf.proxmox.vmid, *conf.proxmox.other_vmids):
                v = vms.get(vmid)
                if not v:
                    continue
                m = OK if v.running else KO
                up = f" · uptime {dur(v.uptime_s)}" if v.running else ""
                print(f"      {m} VM {v.vmid:<4} {v.name:<14} {v.status}{up}")
        except Exception as exc:                      # noqa: BLE001
            print(f"      \033[31mAPI en erreur : {exc}\033[0m")

    # --- VM / docker ---------------------------------------------------------
    vm_ping = net.ping(conf.vm_ip)
    dh = DockerHost(conf.docker_host)
    docker_ok = dh.reachable() if vm_ping else False
    mark = OK if docker_ok else (UNK if vm_ping else KO)
    print(f"\n  {mark} \033[1mVM Docker\033[0m {conf.vm_ip}   "
          f"ping {'OK' if vm_ping else 'KO'} · docker {'OK' if docker_ok else 'KO'}")

    # --- jeux ----------------------------------------------------------------
    for gconf in conf.enabled_games:
        print(f"\n  \033[1m{gconf.key}\033[0m  ({gconf.container})")
        if not docker_ok:
            print(f"      {UNK} indisponible — la VM ne repond pas")
            continue

        drv = ValheimDriver(gconf, dh, conf.vm_ip)
        st = dh.state(gconf.container)
        m = OK if st.running else KO
        print(f"      {m} conteneur {st.status}"
              + (f" · uptime {dur(st.uptime_s)}" if st.running else "")
              + (f" · code {st.exit_code}" if not st.running and st.exit_code is not None else "")
              + (f" · health {st.health}" if st.health else " · pas de healthcheck"))

        if st.running:
            info = net.a2s_info(conf.vm_ip, gconf.query_port)
            if info.ok:
                print(f"      {OK} jeu joignable — « {info.name} » · "
                      f"\033[1m{info.players} / {info.max_players} joueurs\033[0m "
                      f"(a2s {gconf.query_port})")
            else:
                print(f"      {KO} ne repond pas sur a2s {gconf.query_port} — {info.error}")

            lines = dh.logs(gconf.container, tail=800)
            pl = drv.presence_from_logs(lines)
            if pl.known:
                print(f"      · recoupement logs : {pl.count} connexion(s) "
                      f"(ligne emise toutes les ~10 min)")
            reg = drv.registered(lines)
            if reg is False:
                print(f"      {UNK} \033[33mnon enregistre aupres de PlayFab\033[0m — "
                      "absent de la liste publique, joignable par IP directe")

        sv = drv.last_save()
        if sv.known:
            m = OK if sv.complete else KO
            age = sv.age_s(now)
            flag = "" if sv.complete else "  \033[31m← marqueur .ok absent\033[0m"
            print(f"      {m} derniere sauvegarde : generation {sv.generation}, "
                  f"il y a {dur(age)}{flag}")
        else:
            print(f"      {UNK} sauvegarde du jeu illisible — {sv.error}")

        # copies locales sur le NAS
        d = conf.backup_dest / gconf.key
        copies = sorted(d.glob("*-gen*")) if d.exists() else []
        if copies:
            last = copies[-1]
            size = sum(f.stat().st_size for f in last.rglob("*") if f.is_file())
            age = now.timestamp() - last.stat().st_mtime
            print(f"      {OK} copie sur le NAS : {last.name} · "
                  f"{size / 1048576:.1f} Mo · il y a {dur(age)} · {len(copies)} conservee(s)")
        else:
            print(f"      {KO} \033[31maucune copie du monde sur le NAS\033[0m")

    dh.close()
    print()
    return 0


def cmd_power(conf, seconds: int, interval: float) -> int:
    """Releve la courbe de consommation. Sert a calibrer les seuils (phase 4)."""
    plug = PlugClient(conf.plug, forbidden=conf.nas_plug)
    print(f"releve pendant {seconds} s, toutes les {interval} s — Ctrl-C pour arreter\n")
    t0 = time.time()
    vals: list[float] = []
    try:
        while time.time() - t0 < seconds:
            r = plug.read()
            ts = datetime.now().strftime("%H:%M:%S")
            if r.ok and r.watts is not None:
                vals.append(r.watts)
                bar = "█" * min(60, int(r.watts / 4))
                print(f"  {ts}  {r.watts:7.1f} W  {r.volts:6.1f} V  {bar}")
            else:
                # Une lecture ratee n'est PAS une valeur : elle n'entre pas dans
                # la serie et remettrait a zero tout compteur de maintien.
                print(f"  {ts}  \033[33m   INCONNU\033[0m  ({r.error})")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  interrompu")
    if vals:
        print(f"\n  {len(vals)} mesures · min {min(vals):.1f} W · "
              f"max {max(vals):.1f} W · moy {sum(vals) / len(vals):.1f} W")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bifrost", description="pilotage du serveur de jeu (lecture seule)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="etat complet, ne modifie rien")
    p = sub.add_parser("power", help="releve la courbe de consommation")
    p.add_argument("--watch", type=int, default=60, help="duree en secondes")
    p.add_argument("--interval", type=float, default=2.0)
    sub.add_parser("discord-probe", help="liste les ID Discord visibles par le bot")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    conf = load()
    if args.cmd == "status":
        return cmd_status(conf)
    if args.cmd == "power":
        return cmd_power(conf, args.watch, args.interval)
    if args.cmd == "discord-probe":
        from dotenv import dotenv_values
        from .config import ROOT
        from .discordui.probe import run
        token = (dotenv_values(ROOT / "secrets.env").get("DISCORD_BOT_TOKEN") or "").strip()
        if not token:
            print("DISCORD_BOT_TOKEN est vide dans secrets.env")
            return 2
        return run(token)
    return 1


if __name__ == "__main__":
    sys.exit(main())
