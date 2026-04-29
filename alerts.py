from __future__ import annotations

from datetime import datetime, timezone

import discord


EVENT_LABELS = {
    "new": "\U0001f195 NOUVEAU PRODUIT",
    "restock": "\U0001f525 RESTOCK",
    "out_of_stock": "\u274c RUPTURE",
    "price_change": "\U0001f4b0 PRIX MODIFIE",
    "test": "\U0001f9ea ALERTE DE TEST",
}

EVENT_DESCRIPTIONS = {
    "new": "Nouveau produit detecte sur la watch.",
    "restock": "Produit de nouveau disponible.",
    "out_of_stock": "Produit epuise.",
    "price_change": "Prix modifie.",
    "test": "Alerte de test.",
}

EVENT_COLORS = {
    "new": discord.Color.blue(),
    "restock": discord.Color.green(),
    "out_of_stock": discord.Color.red(),
    "price_change": discord.Color.orange(),
    "test": discord.Color.blurple(),
}


def _stock_label(in_stock: bool) -> str:
    return "En stock" if in_stock else "Rupture"


def _event_description(event_type: str, product: object, old_price: str | None) -> str:
    if event_type == "price_change" and old_price is not None:
        return f"Prix passe de {old_price} a {getattr(product, 'price', None) or 'Non detecte'}."
    return EVENT_DESCRIPTIONS.get(event_type, "Alerte produit.")


def _format_variants(variants: tuple[str, ...]) -> str:
    clean = [variant for variant in variants if variant and variant != "Default Title"]
    if not clean:
        return "Disponible"
    displayed = clean[:10]
    suffix = f"\n+{len(clean) - len(displayed)} autre(s)" if len(clean) > len(displayed) else ""
    return "\n".join(f"- {variant}" for variant in displayed) + suffix


def build_product_embed(
    event_type: str,
    product: object,
    watch_url: str,
    old_price: str | None = None,
) -> discord.Embed:
    label = EVENT_LABELS.get(event_type, "Alerte produit")
    color = EVENT_COLORS.get(event_type, discord.Color.blurple())

    embed = discord.Embed(
        title=f"{label} - {product.title}",
        url=str(product.product_url),
        description=_event_description(event_type, product, old_price),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Prix actuel", value=str(product.price or "Non detecte"), inline=True)
    embed.add_field(name="Stock", value=_stock_label(bool(product.in_stock)), inline=True)
    embed.add_field(name="Bouton", value=f"[Voir produit]({product.product_url})", inline=False)

    variants = getattr(product, "available_variants", ())
    if variants:
        embed.add_field(name="Variantes disponibles", value=_format_variants(variants), inline=False)

    if old_price is not None:
        embed.add_field(name="Ancien prix", value=str(old_price), inline=True)

    if getattr(product, "image", None):
        embed.set_thumbnail(url=str(product.image))

    embed.set_footer(text=f"URL surveillee: {watch_url}")
    return embed


async def send_alert(
    channel: discord.abc.Messageable,
    event_type: str,
    product: object,
    watch_url: str,
    old_price: str | None = None,
    ping_role_id: int | None = None,
) -> None:
    content = f"<@&{ping_role_id}>" if ping_role_id else None
    allowed_mentions = discord.AllowedMentions(everyone=False, users=False, roles=True) if ping_role_id else None
    await channel.send(
        content=content,
        embed=build_product_embed(event_type, product, watch_url, old_price),
        allowed_mentions=allowed_mentions,
    )


send_product_alert = send_alert
