"""Le bot Discord : les commandes, un verrou, un journal.

Trois niveaux de droits, et la distinction compte :

  - `/status` est ouvert a tous et ne prend aucun verrou ;
  - `/start /stop /save /restart` exigent le role autorise POUR CE JEU, qui se
    change a chaud via /allowrole et vit en base ;
  - `/allowrole` exige le role CHEF, fixe dans secrets.env. Changer qui detient
    le pouvoir n'est pas un acte d'operateur : si le role de jeu pouvait se
    reattribuer lui-meme, n'importe lequel de ses membres pourrait exclure tous
    les autres.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands

from ..config import Conf
from ..core.events import Store
from ..core.gate import SequenceReport, Stage, run_sequence
from ..core.sequences import Orchestrator
from ..core.settings import Settings
from ..games.registry import build as build_driver
from ..core.state import Busy, Machine, State
from ..infra import net
from ..infra.dockerhost import DockerHost
from ..infra.plug import PlugClient
from ..infra.proxmox import ProxmoxClient

log = logging.getLogger(__name__)
BLEU, VERT, ROUGE, ORANGE = 0x5865F2, 0x2ECC71, 0xE74C3C, 0xE67E22


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
    return f"{s // 86400} j"


def barre(reste: float, total: float, cases: int = 12) -> str:
    """Jauge qui se vide. Plus lisible qu'un nombre seul du coin de l'oeil."""
    pleines = max(0, min(cases, round(cases * reste / total))) if total else 0
    return "▰" * pleines + "▱" * (cases - pleines)


class Cancel(discord.ui.View):
    """Compte a rebours annulable avant une sequence destructive.

    Retient QUI a annule : une action collective doit laisser une trace
    nominative, sinon personne ne sait pourquoi l'extinction n'a pas eu lieu.
    """

    def __init__(self, event: asyncio.Event, seconds: int, role_id: int):
        super().__init__(timeout=float(seconds))
        self.event = event
        self.role_id = role_id
        self.by: discord.abc.User | None = None

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel(self, itx: discord.Interaction, _b: discord.ui.Button):
        # Meme exigence de role que pour lancer : annuler reste une decision
        # sur le serveur, pas un bouton ouvert a tout le salon.
        autorise = isinstance(itx.user, discord.Member) and any(
            r.id == self.role_id for r in itx.user.roles)
        if not autorise:
            return await itx.response.send_message(
                "⛔ Seul le rôle autorisé peut annuler.", ephemeral=True)

        self.by = itx.user
        self.event.set()
        for child in self.children:
            child.disabled = True
        await itx.response.edit_message(view=self)
        self.stop()


class Bifrost(discord.Client):
    def __init__(self, conf: Conf, token: str, guild_id: int, channel_id: int,
                 role_id: int):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.conf = conf
        self.token_ = token
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.role_id = role_id

        self.machine = Machine()
        self.store = Store(conf.backup_dest.parent / "bifrost.db")
        self.plug = PlugClient(conf.plug, forbidden=conf.nas_plug)
        self.px = ProxmoxClient(conf.proxmox)
        self.dh = DockerHost(conf.docker_host)

        self.settings = Settings(conf.backup_dest.parent / "bifrost.db")

        # Un pilote et un orchestrateur PAR JEU actif. Le verrou reste unique :
        # les sequences touchent la meme machine physique, elles ne doivent
        # jamais se chevaucher, meme pour deux jeux differents.
        self.drvs = {}
        self.orchs = {}
        for gconf in conf.enabled_games:
            drv = build_driver(gconf, self.dh, conf.vm_ip)
            self.drvs[gconf.key] = drv
            self.orchs[gconf.key] = Orchestrator(
                conf, drv, self.plug, self.px, self.dh, self.store)
        if not self.drvs:
            raise RuntimeError("aucun jeu actif dans config.yaml")
        self.defaut = next(iter(self.drvs))
        self._register()

    # --- cycle de vie --------------------------------------------------------

    async def setup_hook(self) -> None:
        g = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=g)
        await self.tree.sync(guild=g)
        # Un consommateur de logs par jeu, chacun avec son propre client Docker.
        for key in self.drvs:
            self.loop.create_task(self._log_consumer(key))

    async def on_ready(self) -> None:
        log.warning("connecte : %s — serveur %s, salon %s",
                    self.user, self.guild_id, self.channel_id)

    async def _log_consumer(self, game: str) -> None:
        """Lit le flux de logs en continu et range les faits dans le SQLite.

        Trois precautions apprises a la dure :

        1. Sur un conteneur ARRETE, `logs(follow=True)` ne leve rien : il rend
           les lignes existantes puis se termine aussitot. Sans temporisation,
           la boucle repart immediatement et ouvre une connexion par tour —
           1024 descripteurs epuises en quelques secondes. On ne tente donc le
           flux que si le conteneur tourne, et on dort TOUJOURS entre deux tours.
        2. Une interruption est une PERTE DE CONNAISSANCE, pas un silence : on
           se reconnecte, et la presence reste fondee sur l'A2S, qui ne depend
           pas de ce flux.
        3. Le client est ferme a chaque echec, pour ne pas accumuler de sockets.
        """
        await self.wait_until_ready()
        drv = self.drvs[game]
        dh = DockerHost(self.conf.docker_host)   # un client par flux
        IDLE = 30.0          # conteneur a l'arret : on re-teste sans s'acharner
        CALME = 2.0          # garde-fou anti-boucle-serree, toujours applique
        backoff = 5.0

        while not self.is_closed():
            try:
                st = await asyncio.to_thread(dh.state, drv.container)
                if not st.running:
                    await asyncio.sleep(IDLE)
                    continue

                def pump():
                    for at, line in dh.stream_logs(drv.container):
                        ev = drv.parse_log_line(line, at)
                        if ev is not None:
                            self.store.add_event(
                                drv.key, ev.kind, at=ev.at,
                                player=ev.player, generation=ev.generation,
                                detail=ev.detail)

                await asyncio.to_thread(pump)
                backoff = 5.0
                # Le flux s'est termine sans erreur : le conteneur vient de
                # s'arreter. On temporise quand meme avant de retenter.
                await asyncio.sleep(CALME)

            except asyncio.CancelledError:
                raise
            except Exception as exc:                    # noqa: BLE001
                log.warning("flux de logs [%s] interrompu (%s) — reprise dans %.0fs",
                            game, exc, backoff)
                dh.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

    # --- garde-fous ----------------------------------------------------------

    def role_for(self, game: str) -> int:
        """Surcharge en base si elle existe, sinon le role par defaut.

        La base n'est qu'une couche au-dessus : pas de valeur -> rien ne casse.
        """
        return self.settings.role_for(game) or self.role_id

    def _has_role(self, itx: discord.Interaction, role_id: int) -> bool:
        return isinstance(itx.user, discord.Member) and any(
            r.id == role_id for r in itx.user.roles)

    def _authorised(self, itx: discord.Interaction, game: str) -> bool:
        return self._has_role(itx, self.role_for(game))

    async def _deny(self, itx: discord.Interaction, cmd: str, role_id: int) -> None:
        self.store.audit(str(itx.user), cmd, "refus", f"role {role_id} manquant")
        await itx.response.send_message(
            f"⛔ Réservé au rôle <@&{role_id}>.", ephemeral=True)

    async def _resolve(self, itx: discord.Interaction, jeu: str | None) -> str | None:
        """Quel jeu ? Un seul actif -> pas la peine de le nommer."""
        if jeu is None:
            if len(self.drvs) == 1:
                return self.defaut
            await itx.response.send_message(
                "Précise le jeu : " + ", ".join(f"`{k}`" for k in self.drvs),
                ephemeral=True)
            return None
        if jeu not in self.drvs:
            await itx.response.send_message(
                f"Jeu inconnu : `{jeu}`. Actifs : "
                + ", ".join(f"`{k}`" for k in self.drvs), ephemeral=True)
            return None
        return jeu

    # --- rendu ---------------------------------------------------------------

    async def build_status(self, game: str) -> discord.Embed:
        drv = self.drvs[game]
        now = datetime.now(timezone.utc)
        r = await asyncio.to_thread(self.plug.read)
        host_ping = await asyncio.to_thread(net.ping, self.conf.host_ip)
        host_api = await asyncio.to_thread(self.px.reachable)

        docker_ok = False
        cstate = None
        if host_api:
            docker_ok = await asyncio.to_thread(self.dh.reachable)
        if docker_ok:
            try:
                cstate = await asyncio.to_thread(self.dh.state, drv.container)
            except Exception:                           # noqa: BLE001
                cstate = None

        info = None
        if cstate is not None and cstate.running:
            info = await asyncio.to_thread(
                net.a2s_info, self.conf.vm_ip, drv.conf.query_port)

        if info is not None and info.ok:
            st = State.UP
        elif not host_ping and not host_api:
            st = State.OFF
        else:
            st = State.UNKNOWN
        if self.machine.busy:
            st = State.BOOTING if "demarr" in (self.machine.current_op or "") else State.STOPPING
        self.machine.set(st)

        e = discord.Embed(
            title=f"{st.emoji}  {drv.conf.world or drv.key} — {st.value}",
            colour={State.UP: VERT, State.OFF: 0x95A5A6, State.ERROR: ROUGE}.get(st, ORANGE),
            timestamp=now,
        )

        if info is not None and info.ok:
            e.add_field(name="Joueurs",
                        value=f"**{info.players} / {info.max_players}**", inline=True)
        if cstate is not None and cstate.running:
            e.add_field(name="En ligne depuis", value=dur(cstate.uptime_s), inline=True)

        if r.ok and r.watts is not None:
            e.add_field(name="Consommation", value=f"{r.watts:.1f} W", inline=True)
        elif not r.ok:
            e.add_field(name="Consommation", value="lecture indisponible", inline=True)

        # Sauvegardes : celle du jeu, et la copie sur le NAS.
        lignes = []
        if cstate is not None and cstate.running:
            sv = await self.orchs[game].last_save()
            if sv.known:
                mark = "" if sv.complete else " ⚠️ écriture incomplète"
                lignes.append(f"jeu : génération {sv.generation}, "
                              f"il y a {dur(sv.age_s(now))}{mark}")
            else:
                lignes.append(f"jeu : inconnue ({sv.error})")
        d = self.conf.backup_dest / drv.key
        copies = sorted((q for q in d.glob("*-gen*") if q.is_dir()),
                        key=lambda q: q.stat().st_mtime) if d.exists() else []
        if copies:
            last = copies[-1]
            size = sum(f.stat().st_size for f in last.rglob("*") if f.is_file())
            lignes.append(f"NAS : il y a {dur(now.timestamp() - last.stat().st_mtime)} · "
                          f"{size / 1048576:.1f} Mo · {len(copies)} conservées")
        else:
            lignes.append("NAS : **aucune copie**")
        e.add_field(name="Sauvegardes", value="\n".join(lignes), inline=False)

        # Chaine d'acces, utile quand quelque chose ne repond pas.
        chaine = (f"prise {'🟢' if r.ok and r.on else '⚫'} · "
                  f"hôte {'🟢' if host_api else '🔴'} · "
                  f"docker {'🟢' if docker_ok else '🔴'} · "
                  f"conteneur {'🟢' if cstate and cstate.running else '🔴'} · "
                  f"jeu {'🟢' if info and info.ok else '🔴'}")
        e.add_field(name="Chaîne", value=chaine, inline=False)

        if st is not State.UP:
            vus = self.store.last_players(drv.key, 5)
            if vus:
                e.add_field(name="Derniers joueurs vus",
                            value=", ".join(n for n, _ in vus), inline=False)

        allowed, why = self.conf.power.cut_allowed()
        if not allowed:
            e.set_footer(text=f"Coupure de la prise verrouillée — {why}")
        return e

    # --- moteur de sequence --------------------------------------------------

    async def _run(self, itx: discord.Interaction, titre: str, op: str,
                   stages: list[Stage], role_id: int, countdown: int = 0) -> None:
        try:
            async with self.machine.acquire(op):
                cancel = self.machine.cancel
                if countdown:
                    # Compte a rebours vivant : la jauge se vide, le bouton reste
                    # actif jusqu'a la derniere seconde. On sonde l'annulation
                    # deux fois par seconde mais on n'edite que toutes les 3 s,
                    # pour rester sous la limite de Discord.
                    view = Cancel(cancel, countdown, role_id)

                    def cd_embed(reste: float) -> discord.Embed:
                        e = discord.Embed(
                            title=f"⏳ {titre} dans {max(0, int(round(reste)))} s",
                            description=f"`{barre(reste, countdown)}`\n"
                                        "Dernier moment pour annuler.",
                            colour=ORANGE)
                        return e

                    msg_cd = await itx.followup.send(
                        embed=cd_embed(countdown), view=view, wait=True)
                    fin = time.monotonic() + countdown
                    derniere = time.monotonic()
                    while True:
                        reste = fin - time.monotonic()
                        if cancel.is_set() or reste <= 0:
                            break
                        now = time.monotonic()
                        if now - derniere >= 3.0:
                            derniere = now
                            try:
                                await msg_cd.edit(embed=cd_embed(reste), view=view)
                            except discord.HTTPException:
                                pass
                        await asyncio.sleep(0.5)
                    view.stop()

                    if cancel.is_set():
                        par = view.by
                        qui = par.mention if par else "quelqu'un"
                        self.store.audit(str(itx.user), op, "refus",
                                         f"annulé par {par}" if par else "annulé")
                        e = discord.Embed(
                            title="🛑 Annulé",
                            description=f"**{titre}** n'a pas été lancé. "
                                        "Rien n'a été touché.",
                            colour=0x95A5A6)
                        e.add_field(name="Annulé par", value=qui, inline=True)
                        e.add_field(name="Demandé par", value=itx.user.mention, inline=True)
                        if par is not None:
                            e.set_author(name=par.display_name,
                                         icon_url=par.display_avatar.url)
                        await msg_cd.edit(embed=e, view=None)
                        log.warning("%s annulee par %s (demandee par %s)", op, par, itx.user)
                        return
                    await msg_cd.edit(
                        embed=discord.Embed(title=f"▶️ {titre} — lancé", colour=BLEU),
                        view=None)

                apercu = "\n".join(f"⬜ {st.label}" for st in stages)
                msg = await itx.followup.send(
                    embed=discord.Embed(title=titre, description=apercu, colour=BLEU),
                    wait=True)

                # Discord limite le rythme d'edition d'un message. On edite a
                # chaque etape franchie, et au plus toutes les 2,5 s pendant une
                # attente — assez pour que ca vive, pas assez pour etre bride.
                etat = {"t": 0.0, "txt": "", "pause": 2.5}

                async def progress(rep: SequenceReport) -> None:
                    now = time.monotonic()
                    txt = rep.render()
                    franchie = rep.current is None
                    if not franchie:
                        if now - etat["t"] < etat["pause"] or txt == etat["txt"]:
                            return
                    e = discord.Embed(title=titre, description=txt, colour=BLEU)
                    e.set_footer(
                        text=f"{rep.done_count}/{len(rep.planned)} étapes · "
                             f"{dur(rep.elapsed_s)}")
                    try:
                        await msg.edit(embed=e)
                        etat["t"], etat["txt"] = now, txt
                        etat["pause"] = 2.5
                    except discord.HTTPException as exc:
                        # 429 : on ralentit au lieu d'insister.
                        etat["t"] = now
                        etat["pause"] = min(etat["pause"] * 2, 20.0)
                        log.warning("edition Discord refusee (%s) — pause %.0fs",
                                    exc, etat["pause"])

                rep = await run_sequence(op, stages, on_progress=progress, cancel=cancel)

                if rep.ok:
                    self.machine.set(State.UP if op == "demarrage" else State.OFF)
                    e = discord.Embed(title=f"✅ {titre}", description=rep.render(),
                                      colour=VERT)
                    e.set_footer(text=f"terminé en {dur(rep.elapsed_s)}")
                    self.store.audit(str(itx.user), op, "ok", f"{rep.elapsed_s:.0f}s")
                else:
                    bad = rep.failed
                    self.machine.set(State.ERROR, bad.detail if bad else None)
                    e = discord.Embed(
                        title=f"❌ {titre} — interrompu",
                        description=rep.render(), colour=ROUGE)
                    e.add_field(
                        name="Rien n'a été forcé",
                        value=f"La séquence s'est arrêtée à « {bad.label} ».\n"
                              "Aucune étape suivante n'a été exécutée.",
                        inline=False)
                    # Le detail utile vit dans le rapport de la PORTE, pas dans
                    # celui de l'action : sans ca l'audit dit "echec" sans dire
                    # pourquoi, ce qui est exactement ce qu'on veut eviter.
                    raison = ""
                    if bad is not None:
                        raison = (bad.gate.detail if bad.gate else "") or bad.detail
                    self.store.audit(str(itx.user), op, "echec",
                                     f"{bad.label}: {raison}" if bad else "")
                await msg.edit(embed=e)

        except Busy as exc:
            await itx.followup.send(f"⏳ {exc}. Réessaie quand c'est fini.", ephemeral=True)
        except Exception as exc:                        # noqa: BLE001
            log.exception("sequence %s en erreur", op)
            self.machine.set(State.ERROR, str(exc))
            self.store.audit(str(itx.user), op, "echec", str(exc))
            await itx.followup.send(f"💥 Erreur inattendue : `{exc}`")

    # --- commandes -----------------------------------------------------------

    def _register(self) -> None:
        tree, me = self.tree, self

        # CHOIX STRICTS, pas autocompletion : Discord impose une liste
        # deroulante et refuse toute autre valeur cote client. Une faute de
        # frappe devient impossible, au lieu d'etre rattrapee apres coup.
        # La liste est figee au demarrage, ce qui convient : les jeux viennent
        # de config.yaml et en ajouter un impose de toute facon un redemarrage,
        # puisqu'il faut aussi lui ecrire un pilote.
        CHOIX_JEU = [
            app_commands.Choice(
                name=(f"{d.conf.world} ({k})" if d.conf.world else k)[:100],
                value=k)
            for k, d in me.drvs.items()
        ]
        DESC_JEU = "Jeu concerné (facultatif si un seul est actif)"

        @tree.command(name="status", description="État du serveur — ne modifie rien")
        @app_commands.describe(jeu=DESC_JEU)
        @app_commands.choices(jeu=CHOIX_JEU)
        async def status(itx: discord.Interaction, jeu: str | None = None):
            key = await me._resolve(itx, jeu)
            if key is None:
                return
            await itx.response.defer()
            await itx.followup.send(embed=await me.build_status(key))

        async def lancer(itx, jeu, op, titre, fabrique, countdown=0):
            key = await me._resolve(itx, jeu)
            if key is None:
                return
            role = me.role_for(key)
            if not me._has_role(itx, role):
                return await me._deny(itx, op, role)
            await itx.response.defer()
            await me._run(itx, f"{titre} — {key}", op,
                          fabrique(me.orchs[key]), role, countdown=countdown)

        @tree.command(name="start", description="Allumer la machine et le serveur de jeu")
        @app_commands.describe(jeu=DESC_JEU)
        @app_commands.choices(jeu=CHOIX_JEU)
        async def start(itx: discord.Interaction, jeu: str | None = None):
            await lancer(itx, jeu, "demarrage", "Démarrage",
                         lambda o: o.stages_start())

        @tree.command(name="stop", description="Éteindre proprement, jusqu'à la machine")
        @app_commands.describe(jeu=DESC_JEU)
        @app_commands.choices(jeu=CHOIX_JEU)
        async def stop(itx: discord.Interaction, jeu: str | None = None):
            await lancer(itx, jeu, "extinction", "Extinction",
                         lambda o: o.stages_stop(), countdown=me.conf.countdown_s)

        @tree.command(name="save", description="Copier le monde vers le NAS, sans rien arrêter")
        @app_commands.describe(jeu=DESC_JEU)
        @app_commands.choices(jeu=CHOIX_JEU)
        async def save(itx: discord.Interaction, jeu: str | None = None):
            await lancer(itx, jeu, "sauvegarde", "Sauvegarde",
                         lambda o: o.stages_save())

        @tree.command(name="restart", description="Redémarrer le monde (sauvegarde vérifiée avant)")
        @app_commands.describe(jeu=DESC_JEU)
        @app_commands.choices(jeu=CHOIX_JEU)
        async def restart(itx: discord.Interaction, jeu: str | None = None):
            await lancer(itx, jeu, "redemarrage", "Redémarrage",
                         lambda o: o.stages_restart(), countdown=me.conf.countdown_s)

        @tree.command(name="allowrole",
                      description="Changer le rôle autorisé pour un jeu (réservé au CHEF)")
        @app_commands.describe(
            jeu=DESC_JEU,
            role="Rôle à autoriser. Laisser vide pour afficher les rôles en place.")
        @app_commands.choices(jeu=CHOIX_JEU)
        async def allowrole(itx: discord.Interaction, jeu: str | None = None,
                            role: discord.Role | None = None):
            # Sans role -> lecture seule, ouverte : savoir qui a le droit n'est
            # pas un privilege, et ca evite un aller-retour avant de demander.
            if role is None:
                lignes = []
                surcharges = me.settings.roles()
                for k in me.drvs:
                    rid = me.role_for(k)
                    src = surcharges.get(k)
                    origine = (f"changé le {src[1][:16].replace('T', ' ')} par {src[2]}"
                               if src else "valeur par défaut")
                    lignes.append(f"**{k}** → <@&{rid}>  ·  _{origine}_")
                e = discord.Embed(title="Rôles autorisés", colour=BLEU,
                                  description="\n".join(lignes))
                e.set_footer(text="Seul le rôle CHEF peut les modifier.")
                return await itx.response.send_message(embed=e, ephemeral=True)

            if not me.conf.discord.admin_role_id:
                return await itx.response.send_message(
                    "⛔ Aucun rôle CHEF n'est configuré (`DISCORD_ADMIN_ROLE`).",
                    ephemeral=True)
            if not me._has_role(itx, me.conf.discord.admin_role_id):
                return await me._deny(itx, "allowrole", me.conf.discord.admin_role_id)

            key = await me._resolve(itx, jeu)
            if key is None:
                return

            # Un role gere par une integration ne peut etre porte par personne
            # d'autre que son bot : l'accepter fermerait la porte a tout le monde.
            if role.managed or role.is_default():
                return await itx.response.send_message(
                    f"⛔ <@&{role.id}> n'est pas un rôle attribuable manuellement.",
                    ephemeral=True)

            ancien = me.role_for(key)
            me.settings.set_role(key, role.id, str(itx.user))
            me.store.audit(str(itx.user), "allowrole", "ok",
                           f"{key} : {ancien} -> {role.id} ({role.name})")
            log.warning("allowrole %s : %s -> %s par %s", key, ancien, role.id, itx.user)

            e = discord.Embed(
                title="✅ Rôle mis à jour", colour=VERT,
                description=f"**{key}** est désormais piloté par <@&{role.id}>.")
            e.add_field(name="Avant", value=f"<@&{ancien}>", inline=True)
            e.add_field(name="Après", value=f"<@&{role.id}>", inline=True)
            e.add_field(name="Membres concernés", value=str(len(role.members)), inline=True)
            e.set_footer(text=f"Modifié par {itx.user.display_name} · effet immédiat")
            await itx.response.send_message(embed=e)

    def go(self) -> None:
        self.run(self.token_, log_handler=None)
