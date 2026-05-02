from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands

from alerts import build_product_embed, send_product_alert
from dashboard import AddUrlModal, send_dashboard
from database import Database
from discord_logs import send_log
from scraper import ProductSnapshot, normalize_url


LOGGER = logging.getLogger("shop-monitor.commands")
MIN_INTERVAL_SECONDS = 10
SIMULATED_EVENT_TYPES = ("new", "restock", "out_of_stock", "price_change")


async def _require_admin(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if permissions and permissions.administrator:
        return True

    await interaction.response.send_message(
        "Commande refusee: il faut la permission Administrateur pour utiliser ce bot.",
        ephemeral=True,
    )
    return False


def _guild_id(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise app_commands.AppCommandError("Cette commande doit etre utilisee dans un serveur Discord.")
    return interaction.guild_id


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


def setup_commands(
    bot: discord.Client,
    db: Database,
    monitor: Any,
    default_interval: int,
    max_watches_per_guild: int,
) -> None:
    tree = bot.tree

    @tree.command(name="set_logs_channel", description="Configure le salon des logs importants du bot.")
    @app_commands.describe(salon="Salon ou envoyer les logs importants")
    async def set_logs_channel(interaction: discord.Interaction, salon: discord.TextChannel) -> None:
        if not await _require_admin(interaction):
            return
        guild_id = _guild_id(interaction)
        db.set_logs_channel(guild_id, salon.id)
        await interaction.response.send_message(f"Salon logs configure: {salon.mention}", ephemeral=True)
        await send_log(
            bot,
            "Le salon de logs Discord a ete configure.",
            "info",
            interaction.guild,
            db=db,
            action="Salon logs configure",
            user=interaction.user,
            force=True,
        )

    @tree.command(name="dashboard", description="Envoie le dashboard interactif Shopify Monitor.")
    @app_commands.describe(salon="Salon ou envoyer le dashboard")
    async def dashboard(interaction: discord.Interaction, salon: discord.TextChannel) -> None:
        await send_dashboard(bot, db, monitor, interaction, salon)

    @tree.command(name="add_url", description="Ouvre le flow interactif pour ajouter une URL Shopify.")
    async def add_url(interaction: discord.Interaction) -> None:
        if not await _require_admin(interaction):
            return
        _guild_id(interaction)
        await interaction.response.send_modal(AddUrlModal(bot, db, monitor))

    @tree.command(name="remove_url", description="Supprime une URL surveillee.")
    async def remove_url(interaction: discord.Interaction, url: str) -> None:
        if not await _require_admin(interaction):
            return
        normalized_url = normalize_url(url)
        watch = db.get_watch_by_url(_guild_id(interaction), normalized_url)
        if watch is None:
            await interaction.response.send_message("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return

        monitor.stop_watch(watch.id)
        db.delete_watch(_guild_id(interaction), normalized_url)
        await interaction.response.send_message(f"Surveillance supprimee: {normalized_url}", ephemeral=True)
        await send_log(
            bot,
            "Watch supprimee.",
            "warning",
            interaction.guild,
            db=db,
            action="Watch supprimee",
            url=normalized_url,
            user=interaction.user,
        )

    @tree.command(name="list_urls", description="Affiche les URLs surveillees sur ce serveur.")
    async def list_urls(interaction: discord.Interaction) -> None:
        if not await _require_admin(interaction):
            return

        watches = db.list_watches(_guild_id(interaction))
        if not watches:
            await interaction.response.send_message("Aucune URL surveillee sur ce serveur.", ephemeral=True)
            return

        lines = []
        for watch in watches:
            channel = interaction.guild.get_channel(watch.channel_id) if interaction.guild else None
            channel_label = channel.mention if isinstance(channel, discord.TextChannel) else f"`{watch.channel_id}`"
            role = interaction.guild.get_role(watch.ping_role_id) if interaction.guild and watch.ping_role_id else None
            role_label = role.mention if role else "aucun ping"
            status = "active" if watch.enabled else "pause"
            errors = f", erreurs: {watch.error_count}" if watch.error_count else ""
            lines.append(f"- `{status}` {watch.url} -> {channel_label}, {watch.interval_seconds}s, {role_label}{errors}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="set_channel", description="Change le salon d'alertes d'une URL surveillee.")
    @app_commands.describe(url="URL surveillee", salon="Nouveau salon d'alertes")
    async def set_channel(interaction: discord.Interaction, url: str, salon: discord.TextChannel) -> None:
        if not await _require_admin(interaction):
            return
        normalized_url = normalize_url(url)
        watch = db.set_channel(_guild_id(interaction), normalized_url, salon.id)
        if watch is None:
            await interaction.response.send_message("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return
        await interaction.response.send_message(f"Salon mis a jour pour {normalized_url}: {salon.mention}", ephemeral=True)

    @tree.command(name="set_ping_role", description="Configure le role a ping pour une URL surveillee.")
    @app_commands.describe(url="URL surveillee", role="Role a ping, laisser vide pour retirer le ping")
    async def set_ping_role(
        interaction: discord.Interaction,
        url: str,
        role: Optional[discord.Role] = None,
    ) -> None:
        if not await _require_admin(interaction):
            return
        normalized_url = normalize_url(url)
        watch = db.set_ping_role(_guild_id(interaction), normalized_url, role.id if role else None)
        if watch is None:
            await interaction.response.send_message("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return
        label = role.mention if role else "aucun role"
        await interaction.response.send_message(f"Role ping mis a jour pour {normalized_url}: {label}", ephemeral=True)

    @tree.command(name="set_interval", description="Change l'intervalle de verification d'une URL.")
    @app_commands.describe(url="URL surveillee", secondes="Nouvel intervalle en secondes")
    async def set_interval(interaction: discord.Interaction, url: str, secondes: int) -> None:
        if not await _require_admin(interaction):
            return
        secondes = max(secondes, MIN_INTERVAL_SECONDS)
        normalized_url = normalize_url(url)
        watch = db.set_interval(_guild_id(interaction), normalized_url, secondes)
        if watch is None:
            await interaction.response.send_message("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return
        monitor.restart_watch(watch.id)
        await interaction.response.send_message(f"Intervalle mis a jour pour {normalized_url}: {secondes}s", ephemeral=True)

    @tree.command(name="pause", description="Met une surveillance en pause.")
    async def pause(interaction: discord.Interaction, url: str) -> None:
        if not await _require_admin(interaction):
            return
        normalized_url = normalize_url(url)
        watch = db.set_enabled(_guild_id(interaction), normalized_url, False)
        if watch is None:
            await interaction.response.send_message("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return
        monitor.stop_watch(watch.id)
        await interaction.response.send_message(f"Surveillance en pause: {normalized_url}", ephemeral=True)
        await send_log(
            bot,
            "Watch mise en pause manuellement.",
            "warning",
            interaction.guild,
            db=db,
            action="Watch pausee",
            url=normalized_url,
            user=interaction.user,
        )

    @tree.command(name="resume", description="Reprend une surveillance en pause.")
    async def resume(interaction: discord.Interaction, url: str) -> None:
        if not await _require_admin(interaction):
            return
        normalized_url = normalize_url(url)
        watch = db.set_enabled(_guild_id(interaction), normalized_url, True)
        if watch is None:
            await interaction.response.send_message("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return
        monitor.restart_watch(watch.id)
        await interaction.response.send_message(f"Surveillance reprise: {normalized_url}", ephemeral=True)
        await send_log(
            bot,
            "Watch reprise, compteur d'erreurs remis a zero.",
            "info",
            interaction.guild,
            db=db,
            action="Watch reprise",
            url=normalized_url,
            user=interaction.user,
        )

    @tree.command(name="stats", description="Affiche les statistiques internes du bot.")
    async def stats(interaction: discord.Interaction) -> None:
        if not await _require_admin(interaction):
            return
        guild_id = _guild_id(interaction)
        active_watches = db.count_watches(guild_id, enabled=True)
        total_watches = db.count_watches(guild_id)
        total_products = db.count_products(guild_id)
        persisted_alerts = db.get_alerts_sent()
        runtime = monitor.runtime

        embed = discord.Embed(title="Stats monitor", color=discord.Color.blurple())
        embed.add_field(name="Watches actives", value=f"{active_watches}/{total_watches}", inline=True)
        embed.add_field(name="Produits suivis", value=str(total_products), inline=True)
        embed.add_field(name="Alertes envoyees", value=str(persisted_alerts), inline=True)
        embed.add_field(name="Uptime", value=_format_uptime(runtime.started_at), inline=True)
        embed.add_field(name="Scans termines", value=str(runtime.scans_completed), inline=True)
        embed.add_field(name="Requetes HTTP", value=str(runtime.http_requests), inline=True)
        embed.add_field(name="Scan moyen", value=f"{runtime.average_scan_seconds:.2f}s", inline=True)
        embed.add_field(name="Dernier scan", value=f"{runtime.last_scan_seconds:.2f}s", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="ping", description="Affiche la latence Discord et le temps moyen de scan.")
    async def ping(interaction: discord.Interaction) -> None:
        if not await _require_admin(interaction):
            return
        latency_ms = bot.latency * 1000
        avg_scan = monitor.runtime.average_scan_seconds
        await interaction.response.send_message(
            f"Pong: Discord {latency_ms:.0f}ms | scan moyen {avg_scan:.2f}s",
            ephemeral=True,
        )

    @tree.command(name="analyse_carte", description="Analyse le centrage d'une carte Pokemon depuis une photo.")
    @app_commands.describe(image="Photo de la carte Pokemon a analyser")
    async def analyse_carte(
        interaction: discord.Interaction,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        if image is None:
            await interaction.response.send_message("Aucune image envoyee.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        with tempfile.TemporaryDirectory(prefix="pokemon_card_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            analysis_error_type: type[Exception] = Exception
            try:
                import cv2
                from pokemon_card_analysis import (
                    CardAnalysisError,
                    analyze_card_image,
                    download_attachment,
                )

                analysis_error_type = CardAnalysisError
                image_path = await download_attachment(image, tmp_path)
                analysis = await asyncio.to_thread(analyze_card_image, image_path)
                output_path = tmp_path / "analyse_carte.png"
                if not cv2.imwrite(str(output_path), analysis.annotated_image):
                    raise CardAnalysisError("Impossible de generer l'image annotee.")
            except ImportError:
                LOGGER.exception("Dependances OpenCV manquantes")
                await interaction.followup.send(
                    "Analyse impossible: dependances manquantes. Installez `opencv-python-headless` et `numpy`.",
                    ephemeral=True,
                )
                return
            except analysis_error_type as exc:
                await interaction.followup.send(f"Analyse impossible: {exc}", ephemeral=True)
                return
            except Exception:
                LOGGER.exception("Erreur inattendue pendant l'analyse de carte")
                await interaction.followup.send(
                    "Erreur inattendue pendant l'analyse de la carte.",
                    ephemeral=True,
                )
                return

            centering = analysis.centering
            message = (
                "**Analyse du centrage**\n"
                f"Gauche/Droite : {centering.left_percent:.1f}% / {centering.right_percent:.1f}%\n"
                f"Haut/Bas : {centering.top_percent:.1f}% / {centering.bottom_percent:.1f}%\n"
                f"Estimation : {analysis.grade_estimate}\n"
                "Estimation uniquement, non garantie."
            )
            await interaction.followup.send(
                content=message,
                file=discord.File(str(output_path), filename="analyse_carte.png"),
            )

    @tree.command(name="test_alert", description="Envoie une fausse alerte de test dans le salon configure.")
    async def test_alert(interaction: discord.Interaction, url: str) -> None:
        if not await _require_admin(interaction):
            return
        normalized_url = normalize_url(url)
        watch = db.get_watch_by_url(_guild_id(interaction), normalized_url)
        if watch is None:
            await interaction.response.send_message("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(watch.channel_id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Le salon configure est introuvable.", ephemeral=True)
            return

        product = ProductSnapshot(
            product_key="test-alert",
            title="Produit de test",
            price="19.99",
            image=None,
            product_url=normalized_url,
            in_stock=True,
        )
        content = f"<@&{watch.ping_role_id}>" if watch.ping_role_id else None
        allowed_mentions = discord.AllowedMentions(everyone=False, users=False, roles=True) if watch.ping_role_id else None
        await channel.send(
            content=content,
            embed=build_product_embed("test", product, normalized_url),
            allowed_mentions=allowed_mentions,
        )
        await interaction.response.send_message(f"Alerte de test envoyee dans {channel.mention}.", ephemeral=True)

    @tree.command(name="simulate_event", description="Simule une alerte produit reelle sans modifier la base.")
    @app_commands.describe(url="URL surveillee", type="Type d'evenement a simuler")
    @app_commands.choices(
        type=[
            app_commands.Choice(name="new", value="new"),
            app_commands.Choice(name="restock", value="restock"),
            app_commands.Choice(name="out_of_stock", value="out_of_stock"),
            app_commands.Choice(name="price_change", value="price_change"),
        ]
    )
    async def simulate_event(
        interaction: discord.Interaction,
        url: str,
        type: app_commands.Choice[str],
    ) -> None:
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        normalized_url = normalize_url(url)
        watch = db.get_watch_by_url(_guild_id(interaction), normalized_url)
        if watch is None:
            await interaction.followup.send("Cette URL n'est pas surveillee sur ce serveur.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(watch.channel_id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Le salon configure est introuvable.", ephemeral=True)
            return

        try:
            scrape_result = await monitor.fetch_products_once(normalized_url)
        except Exception as exc:
            LOGGER.exception("Simulation impossible pour %s", normalized_url)
            await interaction.followup.send(f"Impossible de recuperer un produit reel: `{exc}`", ephemeral=True)
            return

        product = next((item for item in scrape_result.products if item.product_url), None)
        if product is None:
            await interaction.followup.send("Aucun produit reel detecte sur cette URL.", ephemeral=True)
            return

        old_price = None
        if type.value == "price_change":
            old_price = product.price or "Ancien prix"

        await send_product_alert(
            channel,
            type.value,
            product,
            normalized_url,
            old_price,
            watch.ping_role_id,
        )
        await interaction.followup.send(
            f"Simulation `{type.value}` envoyee dans {channel.mention} pour `{product.title}`.\n"
            "La base SQLite et Shopify n'ont pas ete modifies.",
            ephemeral=True,
        )
