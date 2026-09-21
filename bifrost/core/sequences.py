"""L'orchestrateur : c'est ici qu'on assemble les portes en sequences.

Rien de specifique a un jeu dans ce fichier : il ne parle qu'au protocole
GameDriver. Les seules choses qu'il sait, c'est l'ordre des etapes et ce qu'il
faut prouver avant de passer a la suivante.

Toute la couche infra est synchrone (requests, SDK docker, tinytuya). On la
bascule dans un thread avec asyncio.to_thread plutot que de bloquer la boucle
d'evenements de Discord — un /status ne doit jamais figer le bot pendant qu'une
extinction est en cours.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..config import Conf
from ..games.base import SaveInfo
from ..infra.dockerhost import DockerHost
from ..infra.plug import PlugClient
from ..infra.proxmox import ProxmoxClient
from ..infra import net
from .events import Store
from .gate import Check, Gate, Stage

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, conf: Conf, driver, plug: PlugClient, px: ProxmoxClient,
                 dh: DockerHost, store: Store):
        self.conf = conf
        self.drv = driver
        self.plug = plug
        self.px = px
        self.dh = dh
        self.store = store
        # Memorise entre deux etapes d'une meme sequence.
        self._gen_before: int | None = None
        self._watts_before: float | None = None

    # --- sondes elementaires -------------------------------------------------

    async def _watts(self, phase: str) -> float | None:
        w = await asyncio.to_thread(self.plug.read_power)
        self.store.add_power(w, phase)
        return w

    async def chk_power_above(self, threshold: float) -> Check:
        """La machine est-elle sous tension ? DEUX preuves, l'une suffit.

        La consommation est le signal RAPIDE : elle monte des la mise sous
        tension, bien avant que le reseau ne reponde. Mais elle depend d'un
        seuil calibre a la main, et un seuil mal choisi bloque un demarrage
        parfaitement normal — c'est arrive le 2026-09-17 avec 20 W exiges pour
        11 W consommes.

        Si l'hote repond deja, la question ne se pose plus : la machine tourne,
        quoi qu'en dise la prise. Contrairement a la preuve d'extinction, ou
        l'on exige que TOUT concorde, ici une seule preuve positive suffit —
        se tromper en declarant "allumee" une machine allumee ne coute rien.
        """
        w = await self._watts("allumage")
        if w is not None and w > threshold:
            return Check.yes(f"{w:.1f} W")
        if await asyncio.to_thread(self.px.reachable):
            mesure = f"{w:.1f} W" if w is not None else "prise illisible"
            return Check.yes(f"l'hôte répond déjà ({mesure})")
        if w is None:
            return Check.unknown("lecture de la prise indisponible, hôte muet")
        return Check.no(f"{w:.1f} W — sous le seuil, et l'hôte ne répond pas")

    async def chk_power_below(self, threshold: float) -> Check:
        """La preuve physique : une CHUTE observee, pas une valeur absolue.

        Mesure du 2026-09-17 : cette prise renvoie exactement 0,0 W machine
        eteinte — les 1 a 3 W du rail 5VSB sont sous son seuil de resolution.
        Zero est donc une valeur legitime, et non le signe d'un capteur muet.

        Du coup, exiger « sous le seuil » ne prouverait rien si la prise se
        mettait a renvoyer zero en permanence. Ce qui prouve, c'est la
        TRANSITION : on a vu la machine consommer au debut de la sequence, on
        la voit ne plus consommer maintenant. Une prise bloquee sur zero
        n'aurait jamais passe le releve initial.

        La seule vraie mesure absente reste la lecture qui echoue -> INCONNU.
        """
        w = await self._watts("extinction")
        if w is None:
            return Check.unknown("lecture de la prise indisponible")
        if w >= threshold:
            return Check.no(f"{w:.1f} W — la machine consomme encore")
        if self.conf.power.require_drop:
            avant = self._watts_before
            if avant is None:
                return Check.unknown(
                    "consommation initiale inconnue — chute invérifiable")
            if avant < self.conf.power.on_threshold_w:
                return Check.unknown(
                    f"consommation initiale de {avant:.1f} W trop basse : "
                    "la prise ne mesurait peut-être déjà rien")
            return Check.yes(f"{avant:.1f} W → {w:.1f} W")
        return Check.yes(f"{w:.1f} W")

    async def _note_watts(self) -> str:
        """Releve la consommation AVANT de commencer.

        Sert de terme de comparaison a la preuve physique, et atteste au
        passage que la prise sait mesurer quelque chose.
        """
        w = await self._watts("avant")
        self._watts_before = w
        if w is None:
            return "lecture indisponible — la preuve physique sera invérifiable"
        return f"{w:.1f} W"

    async def chk_host_up(self) -> Check:
        ping = await asyncio.to_thread(net.ping, self.conf.host_ip)
        api = await asyncio.to_thread(self.px.reachable)
        if ping and api:
            return Check.yes("ping + api")
        return Check.no(f"ping {'OK' if ping else 'KO'}, api {'OK' if api else 'KO'}")

    async def chk_host_down(self) -> Check:
        ping = await asyncio.to_thread(net.ping, self.conf.host_ip)
        api = await asyncio.to_thread(self.px.reachable)
        if not ping and not api:
            return Check.yes("ping KO et api KO")
        return Check.no(f"repond encore — ping {'OK' if ping else 'KO'}, "
                        f"api {'OK' if api else 'KO'}")

    async def chk_vm(self, vmid: int, want: str) -> Check:
        try:
            v = await asyncio.to_thread(self.px.vm, vmid)
        except Exception as exc:                        # noqa: BLE001
            return Check.unknown(f"api : {exc}")
        if v is None:
            return Check.unknown(f"VM {vmid} introuvable")
        return Check(v.status == want, f"VM {vmid} {v.status}")

    async def chk_docker(self) -> Check:
        ok = await asyncio.to_thread(self.dh.reachable)
        return Check(ok, "api docker joignable" if ok else "api docker muette")

    async def chk_container(self, want_running: bool) -> Check:
        try:
            st = await asyncio.to_thread(self.dh.state, self.drv.container)
        except Exception as exc:                        # noqa: BLE001
            return Check.unknown(f"{type(exc).__name__}: {exc}")
        if want_running:
            return Check(st.running, f"conteneur {st.status}")
        if st.running:
            return Check.no("conteneur encore en marche")
        if st.exit_code not in (0, None):
            return Check.no(f"arrete mais code de sortie {st.exit_code}")
        return Check.yes("arrete proprement (code 0)")

    async def chk_game_up(self) -> Check:
        info = await asyncio.to_thread(
            net.a2s_info, self.conf.vm_ip, self.drv.conf.query_port)
        if not info.ok:
            return Check.no(f"pas de reponse a2s : {info.error}")
        return Check.yes(f"« {info.name} » {info.players}/{info.max_players}")

    async def chk_no_other_game(self) -> Check:
        """Aucun AUTRE jeu declare ne tourne sur cette machine.

        /stop ne coupe pas un jeu, il coupe la machine entiere. Si un autre
        serveur tourne dessus, l'arreter par ce chemin le tuerait sans passer
        par sa propre sequence de sauvegarde. On refuse plutot que de choisir
        a la place de l'utilisateur.
        """
        autres = [g for g in self.conf.games.values() if g.key != self.drv.key]
        if not autres:
            return Check.yes("aucun autre jeu declare")
        vivants = []
        for g in autres:
            try:
                st = await asyncio.to_thread(self.dh.state, g.container)
            except Exception as exc:                    # noqa: BLE001
                return Check.unknown(f"état de « {g.key} » illisible : {exc}")
            if st.running:
                vivants.append(g.key)
        if vivants:
            return Check.no("tourne encore : " + ", ".join(vivants))
        return Check.yes(f"{len(autres)} autre(s) jeu(x) à l'arrêt")

    async def chk_no_players(self) -> Check:
        p = await asyncio.to_thread(self.drv.presence)
        if not p.known:
            return Check.unknown(f"presence indeterminee : {p.error}")
        if p.count > 0:
            noms = ", ".join(p.names) if p.names else ""
            return Check.no(f"{p.count} joueur(s) connecte(s){' : ' + noms if noms else ''}")
        return Check.yes("personne connecte")

    async def presence(self):
        return await asyncio.to_thread(self.drv.presence)

    async def last_save(self) -> SaveInfo:
        return await asyncio.to_thread(self.drv.last_save)

    # --- actions -------------------------------------------------------------

    async def _note_generation(self) -> str:
        """Releve la generation AVANT l'arret, pour pouvoir exiger ensuite que
        la copie en porte une au moins egale : c'est la preuve que la
        sauvegarde de fermeture a bien eu lieu."""
        sv = await self.last_save()
        self._gen_before = sv.generation if sv.known else None
        if not sv.known:
            return "generation courante inconnue — la copie sera acceptee sans comparaison"
        return f"generation courante : {sv.generation}"

    async def _snapshot(self, require_new: bool) -> str:
        min_gen = self._gen_before if require_new else None
        rep = await asyncio.to_thread(
            self.drv.snapshot, self.conf.backup_dest, min_gen)
        if not rep.ok:
            raise RuntimeError(rep.error or "copie refusee")
        removed = await asyncio.to_thread(
            self.drv.prune, self.conf.backup_dest,
            self.conf.backup_keep_count, self.conf.backup_keep_days)
        self.store.add_event(self.drv.key, "save", generation=rep.generation,
                             detail=f"copie {rep.dest.name}")
        extra = f", {removed} ancienne(s) purgee(s)" if removed else ""
        return (f"generation {rep.generation} · {rep.files} fichiers · "
                f"{rep.bytes / 1048576:.1f} Mo{extra}")

    async def _stop_container(self) -> str:
        st = await asyncio.to_thread(self.drv.stop, self.drv.conf.stop_timeout_s)
        return f"conteneur {st.status}"

    async def _start_container(self) -> str:
        st = await asyncio.to_thread(self.drv.start)
        return f"conteneur {st.status}"

    async def _ensure_vm(self) -> str:
        """Demarre la VM seulement si elle est reellement a l'arret.

        En temps normal elle revient seule : `onboot: 1` est un drapeau statique
        que Proxmox relit a chaque demarrage de l'hote, sans memoire de la
        maniere dont la VM s'etait arretee. Ce filet ne sert donc qu'au cas ou
        l'autostart serait desactive ou aurait echoue.

        On ne touche a rien tant que l'etat n'est pas franchement `stopped` :
        un `qm start` lance sur une VM en cours de demarrage se heurterait au
        verrou de tache, et on ne veut pas transformer un demarrage normal en
        echec de sequence.
        """
        v = await asyncio.to_thread(self.px.vm, self.conf.proxmox.vmid)
        if v is None:
            return "état indéterminé — on laisse la porte trancher"
        if v.status != "stopped":
            return f"{v.status} — autostart en cours, on laisse faire"
        try:
            await asyncio.to_thread(self.px.vm_start, self.conf.proxmox.vmid)
            return "à l'arrêt malgré onboot — démarrage forcé"
        except Exception as exc:                        # noqa: BLE001
            # Souvent « already running » ou un verrou : la porte qui suit
            # constatera l'etat reel de toute facon.
            log.warning("demarrage VM refuse (%s) — la porte tranchera", exc)
            return f"démarrage refusé ({type(exc).__name__}) — la porte tranchera"

    async def _ensure_container(self) -> str:
        """Demarre le conteneur s'il ne tourne pas deja.

        Idempotent : si la VM l'a relance toute seule au boot, on ne fait rien.
        """
        st = await asyncio.to_thread(self.dh.state, self.drv.container)
        if st.running:
            return "déjà en marche"
        await asyncio.to_thread(self.drv.start)
        return "démarré"

    async def _vm_shutdown(self, vmid: int) -> str:
        await asyncio.to_thread(self.px.vm_shutdown, vmid, 300)
        return f"ordre d'arret envoye a la VM {vmid}"

    async def _node_shutdown(self) -> str:
        await asyncio.to_thread(self.px.node_shutdown)
        return "ordre d'arret envoye a l'hote"

    async def _power_up(self) -> str:
        """Deux chemins, selon l'etat de la prise.

        Prise coupee  -> la rallumer suffit : le front montant du 5VSB declenche
                         « Restore on AC Power Loss » et la machine boote.
        Prise allumee -> la machine est en S5 sans que le courant ait jamais ete
                         coupe. Aucun front montant ne viendra. C'est le paquet
                         magique qui la reveille.
        """
        r = await asyncio.to_thread(self.plug.read)
        if r.ok and r.on is False:
            await asyncio.to_thread(self.plug.turn_on)
            return "prise rallumee — demarrage au retour du courant"

        if await asyncio.to_thread(self.px.reachable):
            return "machine deja en marche"

        if not self.conf.proxmox.mac:
            raise RuntimeError(
                "prise deja allumee et machine eteinte : il faut un paquet "
                "Wake-on-LAN, mais PROXMOX_MAC n'est pas renseigne")
        await asyncio.to_thread(net.wake_on_lan, self.conf.proxmox.mac)
        return f"prise deja allumee — Wake-on-LAN envoye a {self.conf.proxmox.mac}"

    async def _plug_off(self) -> str:
        await asyncio.to_thread(self.plug.turn_off)
        return "prise coupee"

    async def _settle(self) -> str:
        """Laisse les caches disque finir de se vider avant la coupure."""
        await asyncio.sleep(self.conf.power.settle_after_off_s)
        return f"{self.conf.power.settle_after_off_s} s d'attente"

    # --- sequences -----------------------------------------------------------

    def stages_save(self) -> list[Stage]:
        """Copie du monde sans rien arreter.

        La copie reflete la derniere ecriture du JEU, pas l'instant present : le
        serveur dedie Valheim n'expose pas de commande de sauvegarde. L'age reel
        est donc rapporte plutot que sous-entendu.
        """
        return [
            Stage("Relever la génération", action=self._note_generation),
            Stage("Copie du monde vers le NAS",
                  action=lambda: self._snapshot(require_new=False)),
        ]

    def stages_restart(self) -> list[Stage]:
        """L'ordre repond a « verifier qu'il y a une sauvegarde AVANT ».

        La copie d'assurance est prise en premier, donc elle ne depend pas de la
        sauvegarde de fermeture. Si l'arret se passe mal, on a deja quelque chose
        en main.
        """
        p = self.conf.power
        return [
            Stage("Aucun joueur connecté",
                  gate=Gate("Aucun joueur connecte", self.chk_no_players, timeout=10, interval=2)),
            Stage("Relever la génération", action=self._note_generation),
            Stage("Copie d'assurance",
                  action=lambda: self._snapshot(require_new=False)),
            Stage("Arrêt du conteneur", action=self._stop_container,
                  gate=Gate("Conteneur arrete proprement",
                            lambda: self.chk_container(False), timeout=210, interval=3)),
            Stage("Copie définitive (à froid)",
                  action=lambda: self._snapshot(require_new=True)),
            Stage("Redémarrage du conteneur", action=self._start_container,
                  gate=Gate("Conteneur en marche",
                            lambda: self.chk_container(True), timeout=60, interval=3)),
            Stage("Serveur joignable",
                  gate=Gate("Jeu joignable", self.chk_game_up, timeout=300, interval=5)),
        ]

    def stages_stop(self) -> list[Stage]:
        p = self.conf.power
        stages = [
            Stage("Aucun joueur connecté",
                  gate=Gate("Aucun joueur connecte", self.chk_no_players, timeout=10, interval=2)),
            Stage("Aucun autre jeu en marche",
                  gate=Gate("Aucun autre jeu", self.chk_no_other_game,
                            timeout=20, interval=3)),
            Stage("Consommation initiale", action=self._note_watts),
            Stage("Relever la génération", action=self._note_generation),
            Stage("Arrêt du conteneur", action=self._stop_container,
                  gate=Gate("Conteneur arrete proprement",
                            lambda: self.chk_container(False), timeout=210, interval=3)),
            Stage("Copie du monde (à froid)",
                  action=lambda: self._snapshot(require_new=True)),
            Stage(f"Arrêt de la VM {self.conf.proxmox.vmid}",
                  action=lambda: self._vm_shutdown(self.conf.proxmox.vmid),
                  gate=Gate(f"VM {self.conf.proxmox.vmid} arretee",
                            lambda: self.chk_vm(self.conf.proxmox.vmid, "stopped"),
                            timeout=300, interval=5)),
        ]
        for vmid in self.conf.proxmox.other_vmids:
            stages.append(Stage(
                f"VM {vmid} à l'arrêt",
                gate=Gate(f"VM {vmid} arretee", lambda v=vmid: self.chk_vm(v, "stopped"),
                          timeout=300, interval=5)))
        stages += [
            Stage("Arrêt de l'hôte Proxmox", action=self._node_shutdown,
                  gate=Gate("Hote eteint", self.chk_host_down,
                            timeout=180, interval=3, hold=self.conf.host_down_hold_s)),
            Stage("Preuve physique d'extinction",
                  gate=Gate(f"Puissance sous {p.off_threshold_w} W",
                            lambda: self.chk_power_below(p.off_threshold_w),
                            timeout=180, interval=2, hold=p.off_hold_s)),
            Stage("Vidage des caches disque", action=self._settle),
        ]

        allowed, why = p.cut_allowed()
        if allowed:
            stages.append(Stage("Coupure de la prise", action=self._plug_off,
                                gate=Gate("Prise coupee",
                                          lambda: self._chk_plug(False), timeout=15, interval=2)))
        else:
            stages.append(Stage("Coupure de la prise", action=self._explain_lock))
        return stages

    async def _explain_lock(self) -> str:
        """La sequence reussit, mais la derniere etape ne coupe rien et le dit.

        Verrouillee n'est pas echouee : la machine est bel et bien eteinte, ce
        qui est l'essentiel. Seul le courant reste applique.
        """
        _, why = self.conf.power.cut_allowed()
        return f"🔒 verrouillée ({why}) — machine éteinte, prise laissée sous tension"

    async def _chk_plug(self, want_on: bool) -> Check:
        r = await asyncio.to_thread(self.plug.read)
        if not r.ok:
            return Check.unknown(r.error or "lecture indisponible")
        return Check(r.on is want_on, "allumee" if r.on else "eteinte")

    def stages_start(self) -> list[Stage]:
        p = self.conf.power
        return [
            Stage("Mise sous tension", action=self._power_up,
                  gate=Gate("Prise allumee", lambda: self._chk_plug(True),
                            timeout=15, interval=2)),
            Stage("Machine démarrée",
                  gate=Gate(f"Puissance au-dessus de {p.on_threshold_w} W",
                            lambda: self.chk_power_above(p.on_threshold_w),
                            timeout=90, interval=2, hold=p.on_hold_s)),
            Stage("Hôte Proxmox",
                  gate=Gate("Ping + API Proxmox", self.chk_host_up, timeout=240, interval=5)),
            Stage(f"VM {self.conf.proxmox.vmid} · Docker", action=self._ensure_vm,
                  gate=Gate(f"VM {self.conf.proxmox.vmid} en marche",
                            lambda: self.chk_vm(self.conf.proxmox.vmid, "running"),
                            timeout=180, interval=5)),
            Stage("API Docker",
                  gate=Gate("API Docker", self.chk_docker, timeout=180, interval=5)),
            # `unless-stopped` ne relance PAS un conteneur arrete explicitement :
            # apres un /stop, il faut le demarrer, pas seulement l'attendre.
            Stage("Conteneur du jeu", action=self._ensure_container,
                  gate=Gate("Conteneur en marche", lambda: self.chk_container(True),
                            timeout=180, interval=5)),
            # L'image n'a pas de healthcheck : c'est l'A2S qui fait foi.
            Stage("Serveur joignable",
                  gate=Gate("Jeu joignable (a2s)", self.chk_game_up, timeout=300, interval=5)),
        ]
