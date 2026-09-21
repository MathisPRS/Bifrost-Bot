"""Point d'entree du bot. `python -m bifrost`"""

from __future__ import annotations

import logging
import sys

from .config import load
from .discordui.bot import Bifrost


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    conf = load()
    if conf.discord is None:
        print("Configuration Discord incomplete dans secrets.env "
              "(DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_CHANNEL_ID, "
              "DISCORD_ALLOWED_ROLE)", file=sys.stderr)
        return 2
    if not conf.enabled_games:
        print("Aucun jeu actif dans config.yaml", file=sys.stderr)
        return 2

    bot = Bifrost(conf, conf.discord.token, conf.discord.guild_id,
                  conf.discord.channel_id, conf.discord.role_id)
    bot.go()
    return 0


if __name__ == "__main__":
    sys.exit(main())
