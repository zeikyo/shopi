from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import discord


LEVEL_COLORS = {
    "info": discord.Color.blue(),
    "warning": discord.Color.orange(),
    "error": discord.Color.red(),
}

_LAST_SENT: dict[tuple[int, str, str], float] = {}
LOG_COOLDOWN_SECONDS = 10.0


async def send_log(
    bot: discord.Client,
    message: str,
    level: str = "info",
    guild: discord.Guild | None = None,
    *,
    db: Any | None = None,
    action: str = "Log",
    url: str | None = None,
    user: discord.abc.User | None = None,
    force: bool = False,
) -> None:
    if guild is None or db is None:
        return

    channel_id = db.get_logs_channel_id(guild.id)
    if channel_id is None:
        return

    key = (guild.id, level, f"{action}:{url or ''}:{message[:80]}")
    now = time.monotonic()
    if not force and now - _LAST_SENT.get(key, 0.0) < LOG_COOLDOWN_SECONDS:
        return
    _LAST_SENT[key] = now

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException:
            return

    if not isinstance(channel, discord.abc.Messageable):
        return

    embed = discord.Embed(
        title="\U0001f4dd LOG BOT",
        color=LEVEL_COLORS.get(level, discord.Color.blue()),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Action", value=action, inline=True)
    embed.add_field(name="Serveur", value=guild.name, inline=True)
    if user is not None:
        embed.add_field(name="Utilisateur", value=f"{user} ({user.id})", inline=False)
    if url is not None:
        embed.add_field(name="URL", value=url, inline=False)
    embed.add_field(name="Message", value=message[:1024], inline=False)
    bot_name = str(bot.user) if bot.user else "Bot"
    embed.set_footer(text=bot_name)

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.DiscordException:
        return
