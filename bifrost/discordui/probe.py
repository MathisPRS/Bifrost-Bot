"""Sonde Discord : se connecte, liste ce que le bot voit, et ressort.

Sert une seule fois, pour recuperer les ID a mettre dans secrets.env sans avoir
a les chercher a la main. Ne poste rien, ne modifie rien.
"""

from __future__ import annotations

import discord


def run(token: str) -> int:
    intents = discord.Intents.default()          # aucune intention privilegiee
    client = discord.Client(intents=intents)
    code = {"v": 1}

    @client.event
    async def on_ready():
        try:
            print(f"\nconnecte en tant que \033[1m{client.user}\033[0m "
                  f"(id application {client.user.id})\n")
            if not client.guilds:
                print("  \033[33mLe bot n'est sur aucun serveur.\033[0m")
                print("  Invite-le d'abord avec cette URL :\n")
                print(f"  https://discord.com/api/oauth2/authorize"
                      f"?client_id={client.user.id}"
                      f"&permissions=277025508352&scope=bot%20applications.commands\n")
                return

            for g in client.guilds:
                print(f"  \033[1mServeur\033[0m  {g.name}")
                print(f"      DISCORD_GUILD_ID='{g.id}'\n")

                me = g.me
                print("      Salons texte ou le bot peut ecrire :")
                found = False
                for ch in g.text_channels:
                    p = ch.permissions_for(me)
                    if p.view_channel and p.send_messages:
                        flags = []
                        if not p.embed_links:
                            flags.append("SANS embed_links")
                        if not p.read_message_history:
                            flags.append("SANS read_message_history")
                        warn = f"   \033[33m({', '.join(flags)})\033[0m" if flags else ""
                        print(f"        #{ch.name:<22} DISCORD_CHANNEL_ID='{ch.id}'{warn}")
                        found = True
                if not found:
                    print("        \033[31maucun — verifie View Channel et Send Messages\033[0m")

                print("\n      Roles (candidats pour DISCORD_ALLOWED_ROLE) :")
                for r in sorted(g.roles, key=lambda r: -r.position):
                    if r.is_default() or r.managed:
                        continue
                    print(f"        {r.name:<22} DISCORD_ALLOWED_ROLE='{r.id}'"
                          f"   ({len(r.members)} membre(s))")
                print()
            code["v"] = 0
        finally:
            await client.close()

    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        print("\n\033[31mToken refuse par Discord.\033[0m "
              "Regenere-le dans l'onglet Bot (Reset Token).\n")
        return 2
    return code["v"]
