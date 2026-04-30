from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import discord

from database import Database, Watch
from discord_logs import send_log
from scraper import is_valid_shopify_watch_url, normalize_url


MIN_INTERVAL_SECONDS = 10


def _is_admin(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Permission refusee.", ephemeral=True)


def _format_uptime(started_at: datetime) -> str:
    seconds = int((datetime.now(timezone.utc) - started_at).total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}j {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def _short_url(url: str, max_len: int = 70) -> str:
    return url if len(url) <= max_len else f"{url[: max_len - 3]}..."


def _parse_id(value: str) -> int | None:
    match = re.search(r"(\d{15,25})", value.strip())
    return int(match.group(1)) if match else None


def _resolve_text_channel(guild: discord.Guild, value: str) -> discord.TextChannel | None:
    channel_id = _parse_id(value)
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    return channel if isinstance(channel, discord.TextChannel) else None


def _resolve_role(guild: discord.Guild, value: str) -> discord.Role | None:
    if not value.strip():
        return None
    role_id = _parse_id(value)
    if role_id is None:
        return None
    return guild.get_role(role_id)


def build_dashboard_embed(bot: discord.Client, db: Database, monitor: Any, guild: discord.Guild) -> discord.Embed:
    watches = db.list_watches(guild.id)
    active = sum(1 for watch in watches if watch.enabled)
    products = db.count_products(guild.id)
    alerts = db.get_alerts_sent()
    max_error = max((watch.error_count for watch in watches), default=0)

    if max_error >= 5:
        status = "\U0001f534 erreur"
        color = discord.Color.red()
    elif max_error > 0:
        status = "\U0001f7e0 warning"
        color = discord.Color.orange()
    else:
        status = "\U0001f7e2 OK"
        color = discord.Color.green()

    error_watch = next((watch for watch in watches if watch.error_count == max_error and max_error > 0), None)
    last_error = f"{_short_url(error_watch.url, 90)} ({max_error})" if error_watch else "Aucune"

    embed = discord.Embed(
        title="\U0001f4ca Dashboard Shopify Monitor",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Statut global", value=status, inline=True)
    embed.add_field(name="Watches actives", value=f"{active}/{len(watches)}", inline=True)
    embed.add_field(name="Produits suivis", value=str(products), inline=True)
    embed.add_field(name="Alertes envoyees", value=str(alerts), inline=True)
    embed.add_field(name="Uptime", value=_format_uptime(monitor.runtime.started_at), inline=True)
    embed.add_field(name="Scan moyen", value=f"{monitor.runtime.average_scan_seconds:.2f}s", inline=True)
    embed.add_field(name="Derniere erreur", value=last_error, inline=False)
    embed.add_field(name="Dernier refresh", value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:R>", inline=True)
    embed.set_footer(text=str(bot.user) if bot.user else "Shopify Monitor")
    return embed


async def refresh_dashboard_message(bot: discord.Client, db: Database, monitor: Any, guild: discord.Guild) -> bool:
    settings = db.get_guild_settings(guild.id)
    if settings is None or settings.dashboard_channel_id is None or settings.dashboard_message_id is None:
        return False

    channel = guild.get_channel(settings.dashboard_channel_id)
    if not isinstance(channel, discord.TextChannel):
        db.clear_dashboard_message(guild.id)
        return False

    try:
        message = await channel.fetch_message(settings.dashboard_message_id)
        await message.edit(embed=build_dashboard_embed(bot, db, monitor, guild), view=DashboardView(bot, db, monitor))
        return True
    except discord.DiscordException:
        db.clear_dashboard_message(guild.id)
        return False


async def refresh_saved_dashboards(bot: discord.Client, db: Database, monitor: Any) -> None:
    for settings in db.list_dashboard_settings():
        guild = bot.get_guild(settings.guild_id)
        if guild is None:
            continue
        await refresh_dashboard_message(bot, db, monitor, guild)


async def send_dashboard(
    bot: discord.Client,
    db: Database,
    monitor: Any,
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Cette commande doit etre utilisee dans un serveur.", ephemeral=True)
        return
    if not _is_admin(interaction):
        await _deny(interaction)
        return

    message = await channel.send(
        embed=build_dashboard_embed(bot, db, monitor, interaction.guild),
        view=DashboardView(bot, db, monitor),
    )
    db.set_dashboard_message(interaction.guild.id, channel.id, message.id)
    await interaction.response.send_message(f"Dashboard envoye dans {channel.mention}.", ephemeral=True)
    await send_log(
        bot,
        "Dashboard configure.",
        "info",
        interaction.guild,
        db=db,
        action="Dashboard configure",
        user=interaction.user,
        force=True,
    )


class AddUrlModal(discord.ui.Modal, title="Ajouter une URL"):
    url = discord.ui.TextInput(label="URL Shopify", placeholder="https://site.fr/collections/drop", max_length=500)

    def __init__(self, bot: discord.Client, db: Database, monitor: Any) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.monitor = monitor

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _is_admin(interaction):
            await _deny(interaction)
            return

        normalized_url = normalize_url(str(self.url.value))
        if not is_valid_shopify_watch_url(normalized_url):
            await interaction.response.send_message("URL Shopify invalide.", ephemeral=True)
            return
        if self.db.get_watch_by_url(interaction.guild.id, normalized_url) is not None:
            await interaction.response.send_message("Cette URL est deja surveillee.", ephemeral=True)
            return
        max_watches = int(getattr(self.bot, "max_watches_per_guild", 20))
        if self.db.count_watches(interaction.guild.id) >= max_watches:
            await interaction.response.send_message(
                f"Limite atteinte: maximum {max_watches} watches par serveur.",
                ephemeral=True,
            )
            return

        view = AddUrlConfigView(self.bot, self.db, self.monitor, normalized_url)
        await interaction.response.send_message(
            embed=view.build_embed(interaction.guild),
            view=view,
            ephemeral=True,
        )


class AlertChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            custom_id="shopmon:add_url:channel",
            placeholder="Choisir le salon des alertes",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, AddUrlConfigView):
            return
        values = (interaction.data or {}).get("values", [])
        await view.set_channel(interaction, int(values[0]) if values else None)


class PingRoleSelect(discord.ui.RoleSelect):
    def __init__(self) -> None:
        super().__init__(
            custom_id="shopmon:add_url:role",
            placeholder="Choisir un role a ping (optionnel)",
            min_values=0,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, AddUrlConfigView):
            return
        values = (interaction.data or {}).get("values", [])
        await view.set_role(interaction, int(values[0]) if values else None)


class AddUrlConfigView(discord.ui.View):
    def __init__(self, bot: discord.Client, db: Database, monitor: Any, url: str) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.monitor = monitor
        self.url = url
        self.selected_channel: int | None = None
        self.selected_role: int | None = None
        self.selected_interval = 15
        self.add_item(AlertChannelSelect())
        self.add_item(PingRoleSelect())
        self._sync_interval_buttons()

    def _selected_channel(self, guild: discord.Guild) -> discord.TextChannel | discord.Thread | None:
        if self.selected_channel is None:
            return None
        channel = guild.get_channel(self.selected_channel)
        return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None

    def _selected_role(self, guild: discord.Guild) -> discord.Role | None:
        return guild.get_role(self.selected_role) if self.selected_role is not None else None

    def _config_lines(self, guild: discord.Guild) -> str:
        channel = self._selected_channel(guild)
        role = self._selected_role(guild)
        return (
            f"URL: <{self.url}>\n"
            f"Salon: {channel.mention if channel else 'a choisir'}\n"
            f"Intervalle: {self.selected_interval}s\n"
            f"Role ping: {role.mention if role else 'aucun'}"
        )

    def build_embed(self, guild: discord.Guild, status: str | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="Configurer la nouvelle URL",
            description=self._config_lines(guild),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Validation",
            value=status or "Choisis le salon, ajuste l'intervalle, puis confirme.",
            inline=False,
        )
        embed.set_footer(text="La watch sera creee seulement apres confirmation.")
        return embed

    async def _edit(self, interaction: discord.Interaction, status: str | None = None) -> None:
        if interaction.guild is None or not _is_admin(interaction):
            await _deny(interaction)
            return
        await interaction.response.edit_message(embed=self.build_embed(interaction.guild, status), view=self)

    async def set_channel(
        self,
        interaction: discord.Interaction,
        channel_id: int | None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id) if channel_id is not None else None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Choisis un salon texte.", ephemeral=True)
            return
        self.selected_channel = channel.id
        await self._edit(interaction)

    async def set_role(self, interaction: discord.Interaction, role_id: int | None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
            return
        if role_id is not None and interaction.guild.get_role(role_id) is None:
            await interaction.response.send_message("Role introuvable.", ephemeral=True)
            return
        self.selected_role = role_id
        await self._edit(interaction)

    async def _set_interval(self, interaction: discord.Interaction, seconds: int) -> None:
        self.selected_interval = max(seconds, MIN_INTERVAL_SECONDS)
        self._sync_interval_buttons()
        await self._edit(interaction)

    def _sync_interval_buttons(self) -> None:
        for item in self.children:
            if not isinstance(item, discord.ui.Button) or item.label not in {"10s", "15s", "30s"}:
                continue
            item.style = (
                discord.ButtonStyle.primary
                if item.label == f"{self.selected_interval}s"
                else discord.ButtonStyle.secondary
            )

    def _disable_items(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="10s", style=discord.ButtonStyle.secondary, custom_id="shopmon:add_url:interval:10", row=2)
    async def interval_10(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._set_interval(interaction, 10)

    @discord.ui.button(label="15s", style=discord.ButtonStyle.primary, custom_id="shopmon:add_url:interval:15", row=2)
    async def interval_15(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._set_interval(interaction, 15)

    @discord.ui.button(label="30s", style=discord.ButtonStyle.secondary, custom_id="shopmon:add_url:interval:30", row=2)
    async def interval_30(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._set_interval(interaction, 30)

    @discord.ui.button(label="Confirmer", emoji="\u2705", style=discord.ButtonStyle.success, custom_id="shopmon:add_url:confirm", row=3)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not _is_admin(interaction):
            await _deny(interaction)
            return
        if not self.selected_channel:
            await interaction.response.send_message("Choisis d'abord le salon des alertes.", ephemeral=True)
            return

        channel = self._selected_channel(interaction.guild)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Le salon choisi est introuvable.", ephemeral=True)
            return
        if self.db.get_watch_by_url(interaction.guild.id, self.url) is not None:
            self._disable_items()
            await interaction.response.edit_message(
                embed=self.build_embed(interaction.guild, "Cette URL est deja surveillee."),
                view=self,
            )
            return

        max_watches = int(getattr(self.bot, "max_watches_per_guild", 20))
        if self.db.count_watches(interaction.guild.id) >= max_watches:
            self._disable_items()
            await interaction.response.edit_message(
                embed=self.build_embed(interaction.guild, f"Limite atteinte: maximum {max_watches} watches."),
                view=self,
            )
            return

        self._disable_items()
        await interaction.response.edit_message(
            embed=self.build_embed(interaction.guild, "Premier scan en cours..."),
            view=self,
        )

        try:
            scrape_result = await self.monitor.fetch_products_once(self.url)
        except Exception as exc:
            await interaction.edit_original_response(
                embed=self.build_embed(interaction.guild, f"Premier scan impossible: `{exc}`"),
                view=None,
            )
            return
        if not scrape_result.products:
            await interaction.edit_original_response(
                embed=self.build_embed(interaction.guild, "Aucun produit detecte, watch non ajoutee."),
                view=None,
            )
            return

        watch = self.db.add_watch(
            interaction.guild.id,
            self.url,
            channel.id,
            self.selected_interval,
            self.selected_role,
        )
        self.db.upsert_products(watch.id, scrape_result.products)
        self.monitor.restart_watch(watch.id)
        await refresh_dashboard_message(self.bot, self.db, self.monitor, interaction.guild)

        status = (
            f"Watch activee. Produits sauvegardes: {len(scrape_result.products)}. "
            f"Source: {scrape_result.stats.source}."
        )
        await interaction.edit_original_response(embed=self.build_embed(interaction.guild, status), view=None)
        await send_log(
            self.bot,
            f"Watch ajoutee depuis le flow interactif avec {len(scrape_result.products)} produit(s).",
            "info",
            interaction.guild,
            db=self.db,
            action="Watch ajoutee",
            url=self.url,
            user=interaction.user,
        )

    @discord.ui.button(label="Annuler", emoji="\u274c", style=discord.ButtonStyle.danger, custom_id="shopmon:add_url:cancel", row=3)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not _is_admin(interaction):
            await _deny(interaction)
            return
        self._disable_items()
        await interaction.response.edit_message(
            embed=self.build_embed(interaction.guild, "Ajout annule. Aucune watch n'a ete creee."),
            view=None,
        )


class SettingsModal(discord.ui.Modal, title="Parametres watch"):
    url = discord.ui.TextInput(label="URL watch", max_length=500)
    alert_channel = discord.ui.TextInput(label="Nouveau salon alertes ID/mention", required=False, max_length=120)
    interval = discord.ui.TextInput(label="Nouvel intervalle secondes", required=False, max_length=5)
    role_ping = discord.ui.TextInput(label="Role ping ID/mention, none pour retirer", required=False, max_length=120)
    logs_channel = discord.ui.TextInput(label="Salon logs ID/mention optionnel", required=False, max_length=120)

    def __init__(self, bot: discord.Client, db: Database, monitor: Any) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.db = db
        self.monitor = monitor

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _is_admin(interaction):
            await _deny(interaction)
            return
        normalized_url = normalize_url(str(self.url.value))
        watch = self.db.get_watch_by_url(interaction.guild.id, normalized_url)
        if watch is None:
            await interaction.response.send_message("Watch introuvable.", ephemeral=True)
            return

        changes: list[str] = []
        if str(self.alert_channel.value).strip():
            channel = _resolve_text_channel(interaction.guild, str(self.alert_channel.value))
            if channel is None:
                await interaction.response.send_message("Salon d'alertes introuvable.", ephemeral=True)
                return
            self.db.set_channel(interaction.guild.id, normalized_url, channel.id)
            changes.append("salon alertes")

        if str(self.interval.value).strip():
            try:
                interval = max(int(str(self.interval.value).strip()), MIN_INTERVAL_SECONDS)
            except ValueError:
                await interaction.response.send_message("Intervalle invalide.", ephemeral=True)
                return
            self.db.set_interval(interaction.guild.id, normalized_url, interval)
            self.monitor.restart_watch(watch.id)
            changes.append("intervalle")

        role_value = str(self.role_ping.value).strip()
        if role_value:
            role_id = None if role_value.lower() in {"none", "aucun", "remove", "retirer"} else (_resolve_role(interaction.guild, role_value) or None)
            if role_id is None and role_value.lower() not in {"none", "aucun", "remove", "retirer"}:
                await interaction.response.send_message("Role ping introuvable.", ephemeral=True)
                return
            self.db.set_ping_role(interaction.guild.id, normalized_url, role_id.id if isinstance(role_id, discord.Role) else None)
            changes.append("role ping")

        if str(self.logs_channel.value).strip():
            logs_channel = _resolve_text_channel(interaction.guild, str(self.logs_channel.value))
            if logs_channel is None:
                await interaction.response.send_message("Salon logs introuvable.", ephemeral=True)
                return
            self.db.set_logs_channel(interaction.guild.id, logs_channel.id)
            changes.append("salon logs")

        await refresh_dashboard_message(self.bot, self.db, self.monitor, interaction.guild)
        await interaction.response.send_message(
            "Parametres mis a jour: " + (", ".join(changes) if changes else "aucun changement"),
            ephemeral=True,
        )


class WatchSelect(discord.ui.Select):
    def __init__(self, watches: list[Watch], action: str, bot: discord.Client, db: Database, monitor: Any) -> None:
        options = [
            discord.SelectOption(
                label=f"#{watch.id} {'active' if watch.enabled else 'pause'}",
                description=_short_url(watch.url, 90),
                value=str(watch.id),
            )
            for watch in watches[:25]
        ]
        super().__init__(placeholder="Choisir une watch", min_values=1, max_values=1, options=options)
        self.action = action
        self.bot = bot
        self.db = db
        self.monitor = monitor

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not _is_admin(interaction):
            await _deny(interaction)
            return
        watch = self.db.get_watch(int(self.values[0]))
        if watch is None or watch.guild_id != interaction.guild.id:
            await interaction.response.send_message("Watch introuvable.", ephemeral=True)
            return

        if self.action == "pause":
            self.db.set_enabled(interaction.guild.id, watch.url, False)
            self.monitor.stop_watch(watch.id)
            await refresh_dashboard_message(self.bot, self.db, self.monitor, interaction.guild)
            await interaction.response.send_message(f"Watch pausee: `{watch.url}`", ephemeral=True)
            return

        if self.action == "resume":
            self.db.set_enabled(interaction.guild.id, watch.url, True)
            self.monitor.restart_watch(watch.id)
            await refresh_dashboard_message(self.bot, self.db, self.monitor, interaction.guild)
            await interaction.response.send_message(f"Watch reprise: `{watch.url}`", ephemeral=True)
            return

        if self.action == "delete":
            await interaction.response.send_message(
                f"Supprimer `{watch.url}` ?",
                view=ConfirmDeleteView(self.bot, self.db, self.monitor, watch.id),
                ephemeral=True,
            )


class WatchSelectView(discord.ui.View):
    def __init__(self, watches: list[Watch], action: str, bot: discord.Client, db: Database, monitor: Any) -> None:
        super().__init__(timeout=120)
        self.add_item(WatchSelect(watches, action, bot, db, monitor))


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, bot: discord.Client, db: Database, monitor: Any, watch_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.db = db
        self.monitor = monitor
        self.watch_id = watch_id

    @discord.ui.button(label="Oui", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not _is_admin(interaction):
            await _deny(interaction)
            return
        watch = self.db.get_watch(self.watch_id)
        if watch is None or watch.guild_id != interaction.guild.id:
            await interaction.response.send_message("Watch introuvable.", ephemeral=True)
            return
        self.monitor.stop_watch(watch.id)
        self.db.delete_watch(interaction.guild.id, watch.url)
        await refresh_dashboard_message(self.bot, self.db, self.monitor, interaction.guild)
        await interaction.response.edit_message(content=f"Watch supprimee: `{watch.url}`", view=None)

    @discord.ui.button(label="Non", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _is_admin(interaction):
            await _deny(interaction)
            return
        await interaction.response.edit_message(content="Suppression annulee.", view=None)


class DashboardView(discord.ui.View):
    def __init__(self, bot: discord.Client, db: Database, monitor: Any) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.monitor = monitor

    async def _admin_or_deny(self, interaction: discord.Interaction) -> bool:
        if _is_admin(interaction):
            return True
        await _deny(interaction)
        return False

    async def _send_select(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Serveur introuvable.", ephemeral=True)
            return
        watches = self.db.list_watches(interaction.guild.id)
        if not watches:
            await interaction.response.send_message("Aucune watch disponible.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choisis une watch:",
            view=WatchSelectView(watches, action, self.bot, self.db, self.monitor),
            ephemeral=True,
        )

    @discord.ui.button(label="Ajouter URL", emoji="\u2795", style=discord.ButtonStyle.success, custom_id="shopmon:dashboard:add_url")
    async def add_url(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._admin_or_deny(interaction):
            await interaction.response.send_modal(AddUrlModal(self.bot, self.db, self.monitor))

    @discord.ui.button(label="Liste URLs", emoji="\U0001f4cb", style=discord.ButtonStyle.primary, custom_id="shopmon:dashboard:list_urls")
    async def list_urls(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self._admin_or_deny(interaction):
            return
        watches = self.db.list_watches(interaction.guild.id)
        if not watches:
            await interaction.response.send_message("Aucune URL surveillee.", ephemeral=True)
            return
        lines = [
            f"`#{watch.id}` {'ON' if watch.enabled else 'PAUSE'} | {watch.interval_seconds}s | err={watch.error_count} | <#{watch.channel_id}>\n{_short_url(watch.url, 95)}"
            for watch in watches[:15]
        ]
        embed = discord.Embed(title="URLs surveillees", description="\n\n".join(lines), color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Pause", emoji="\u23f8", style=discord.ButtonStyle.secondary, custom_id="shopmon:dashboard:pause")
    async def pause(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._admin_or_deny(interaction):
            await self._send_select(interaction, "pause")

    @discord.ui.button(label="Resume", emoji="\u25b6\ufe0f", style=discord.ButtonStyle.secondary, custom_id="shopmon:dashboard:resume")
    async def resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._admin_or_deny(interaction):
            await self._send_select(interaction, "resume")

    @discord.ui.button(label="Supprimer URL", emoji="\U0001f5d1", style=discord.ButtonStyle.danger, custom_id="shopmon:dashboard:delete")
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._admin_or_deny(interaction):
            await self._send_select(interaction, "delete")

    @discord.ui.button(label="Parametres", emoji="\u2699\ufe0f", style=discord.ButtonStyle.secondary, custom_id="shopmon:dashboard:settings")
    async def settings(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self._admin_or_deny(interaction):
            await interaction.response.send_modal(SettingsModal(self.bot, self.db, self.monitor))

    @discord.ui.button(label="Stats", emoji="\U0001f4ca", style=discord.ButtonStyle.primary, custom_id="shopmon:dashboard:stats")
    async def stats(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self._admin_or_deny(interaction):
            return
        await interaction.response.send_message(embed=build_dashboard_embed(self.bot, self.db, self.monitor, interaction.guild), ephemeral=True)

    @discord.ui.button(label="Refresh", emoji="\U0001f504", style=discord.ButtonStyle.success, custom_id="shopmon:dashboard:refresh")
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self._admin_or_deny(interaction):
            return
        embed = build_dashboard_embed(self.bot, self.db, self.monitor, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)
