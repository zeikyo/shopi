from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import discord
from discord import app_commands
from dotenv import load_dotenv

from alerts import send_product_alert
from commands import setup_commands
from dashboard import DashboardView, refresh_saved_dashboards
from database import Database, StoredProduct, Watch
from discord_logs import send_log
from scraper import ProductSnapshot, ScrapeResult, create_http_client, normalize_url, scrape_products


MIN_INTERVAL_SECONDS = 10
CONFIRMATION_DELAY_SECONDS = 3
EVENT_COOLDOWN_SECONDS = 300
ERRORS_BEFORE_AUTO_PAUSE = 5
JITTER_SECONDS = 2.0
SCRAPE_CACHE_SECONDS = 2.0


class LoggerNameFilter(logging.Filter):
    def __init__(self, prefix: str) -> None:
        super().__init__()
        self.prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.prefix)


def configure_logging() -> None:
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    bot_handler = logging.FileHandler("bot.log", encoding="utf-8")
    bot_handler.setFormatter(formatter)
    bot_handler.setLevel(logging.INFO)
    root.addHandler(bot_handler)

    monitor_handler = logging.FileHandler("monitor.log", encoding="utf-8")
    monitor_handler.setFormatter(formatter)
    monitor_handler.setLevel(logging.INFO)
    monitor_handler.addFilter(LoggerNameFilter("shop-monitor.monitor"))
    root.addHandler(monitor_handler)

    error_handler = logging.FileHandler("error.log", encoding="utf-8")
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.WARNING)
    root.addHandler(error_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    root.addHandler(console_handler)


@dataclass(slots=True)
class ProductEvent:
    event_type: str
    product: ProductSnapshot
    old_price: str | None = None


@dataclass(slots=True)
class RuntimeStats:
    started_at: datetime
    scans_completed: int = 0
    alerts_sent: int = 0
    http_requests: int = 0
    last_scan_seconds: float = 0.0
    scan_seconds: deque[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.scan_seconds = deque(maxlen=100)

    @property
    def average_scan_seconds(self) -> float:
        if not self.scan_seconds:
            return 0.0
        return sum(self.scan_seconds) / len(self.scan_seconds)


def detect_changes(
    products: list[ProductSnapshot],
    existing: dict[str, StoredProduct],
) -> list[ProductEvent]:
    events: list[ProductEvent] = []
    for product in products:
        old = existing.get(product.product_key)
        if old is None:
            events.append(ProductEvent("new", product))
            continue

        if not old.in_stock and product.in_stock:
            events.append(ProductEvent("restock", product))
        elif old.in_stock and not product.in_stock:
            events.append(ProductEvent("out_of_stock", product))

        if old.price != product.price and product.price is not None:
            events.append(ProductEvent("price_change", product, old.price))

    return events


class MonitorManager:
    def __init__(self, bot: discord.Client, db: Database, debug_mode: bool = False) -> None:
        self.bot = bot
        self.db = db
        self.debug_mode = debug_mode
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.logger = logging.getLogger("shop-monitor.monitor")
        self.http_client = create_http_client()
        self.runtime = RuntimeStats(started_at=datetime.now(timezone.utc))
        self._cache: dict[str, tuple[float, ScrapeResult]] = {}
        self._url_locks: dict[str, asyncio.Lock] = {}
        self._domain_locks: dict[str, asyncio.Semaphore] = {}

    def start_all(self) -> None:
        for watch in self.db.list_enabled_watches():
            self.start_watch(watch.id)
        self.logger.info("%s surveillance(s) active(s) chargee(s)", len(self.tasks))

    def start_watch(self, watch_id: int) -> None:
        if watch_id in self.tasks and not self.tasks[watch_id].done():
            return
        self.tasks[watch_id] = asyncio.create_task(self._watch_loop(watch_id), name=f"watch-{watch_id}")

    def stop_watch(self, watch_id: int) -> None:
        task = self.tasks.pop(watch_id, None)
        if task and not task.done():
            task.cancel()

    def restart_watch(self, watch_id: int) -> None:
        self.stop_watch(watch_id)
        self.start_watch(watch_id)

    async def close(self) -> None:
        for watch_id in list(self.tasks):
            self.stop_watch(watch_id)
        await self.http_client.aclose()

    async def fetch_products_once(self, url: str) -> ScrapeResult:
        normalized = normalize_url(url)
        now = time.monotonic()
        cached = self._cache.get(normalized)
        if cached and now - cached[0] <= SCRAPE_CACHE_SECONDS:
            return cached[1]

        lock = self._url_locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            cached = self._cache.get(normalized)
            now = time.monotonic()
            if cached and now - cached[0] <= SCRAPE_CACHE_SECONDS:
                return cached[1]

            domain = urlparse(normalized).netloc
            domain_lock = self._domain_locks.setdefault(domain, asyncio.Semaphore(2))
            async with domain_lock:
                result = await scrape_products(normalized, self.http_client)

            self._cache[normalized] = (time.monotonic(), result)
            self.runtime.http_requests += result.stats.requests
            return result

    async def _watch_loop(self, watch_id: int) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(random.uniform(0, JITTER_SECONDS))
        while not self.bot.is_closed():
            watch = self.db.get_watch(watch_id)
            if watch is None or not watch.enabled:
                self.logger.info("Arret de la surveillance %s", watch_id)
                return

            started = time.perf_counter()
            try:
                await self.scan_watch(watch)
                self.db.reset_error_count(watch.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                error_count = self.db.increment_error_count(watch.id)
                self.logger.exception(
                    "Erreur pendant le scan de %s (%s/%s)",
                    watch.url,
                    error_count,
                    ERRORS_BEFORE_AUTO_PAUSE,
                )
                await self._send_watch_log(
                    watch,
                    f"Erreur critique pendant le scan ({error_count}/{ERRORS_BEFORE_AUTO_PAUSE}).",
                    "error" if error_count >= ERRORS_BEFORE_AUTO_PAUSE else "warning",
                    "Erreur watch",
                )
                if error_count >= ERRORS_BEFORE_AUTO_PAUSE:
                    self.db.auto_pause_watch(watch.id)
                    self.stop_watch(watch.id)
                    self.logger.error("Watch auto-pausee apres %s erreurs: %s", error_count, watch.url)
                    await self._send_watch_log(
                        watch,
                        f"Watch auto-pausee apres {error_count} erreurs consecutives.",
                        "error",
                        "Auto-pause",
                    )
                    return

            elapsed = time.perf_counter() - started
            interval = max(watch.interval_seconds, MIN_INTERVAL_SECONDS)
            jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
            await asyncio.sleep(max(interval + jitter - elapsed, 1))

    async def scan_watch(self, watch: Watch) -> None:
        started = time.perf_counter()
        self.logger.info("Scan %s", watch.url)
        result = await self.fetch_products_once(watch.url)
        products = result.products
        existing = self.db.get_products_for_watch(watch.id)
        products = self._add_missing_products_as_out_of_stock(products, existing)
        candidates = detect_changes(products, existing)

        if candidates:
            events, products = await self._confirm_events(watch, existing, candidates)
        else:
            events = []

        events = [event for event in events if not self._is_event_on_cooldown(event, existing)]
        await self._send_events(watch, events)

        self.db.upsert_products(
            watch.id,
            products,
            {event.product.product_key: event.event_type for event in events},
        )

        elapsed = time.perf_counter() - started
        self.runtime.scans_completed += 1
        self.runtime.last_scan_seconds = elapsed
        self.runtime.scan_seconds.append(elapsed)
        self.logger.info(
            "Scan reussi url=%s source=%s produits=%s events=%s http_requests=%s http_time=%.3fs scan_time=%.3fs",
            watch.url,
            result.stats.source,
            len(products),
            len(events),
            result.stats.requests,
            result.stats.http_time_seconds,
            elapsed,
        )
        if self.debug_mode and (events or result.stats.source != "shopify_json"):
            await self._send_watch_log(
                watch,
                f"Scan debug: source={result.stats.source}, produits={len(products)}, events={len(events)}, temps={elapsed:.2f}s.",
                "info",
                "Scan debug",
            )

    async def _send_events(self, watch: Watch, events: list[ProductEvent]) -> None:
        if not events:
            return

        channel = self.bot.get_channel(watch.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(watch.channel_id)
            except discord.DiscordException:
                channel = None

        if not isinstance(channel, discord.abc.Messageable):
            self.logger.warning("Salon introuvable pour la surveillance %s", watch.url)
            return

        for event in events:
            await send_product_alert(
                channel,
                event.event_type,
                event.product,
                watch.url,
                event.old_price,
                watch.ping_role_id,
            )

        self.runtime.alerts_sent += len(events)
        self.db.increment_alerts_sent(len(events))
        self.logger.info("%s alerte(s) envoyee(s) pour %s", len(events), watch.url)

    async def _send_watch_log(self, watch: Watch, message: str, level: str, action: str) -> None:
        guild = self.bot.get_guild(watch.guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(watch.guild_id)
            except discord.DiscordException:
                return
        await send_log(
            self.bot,
            message,
            level,
            guild,
            db=self.db,
            action=action,
            url=watch.url,
        )

    @staticmethod
    def _add_missing_products_as_out_of_stock(
        products: list[ProductSnapshot],
        existing: dict[str, StoredProduct],
    ) -> list[ProductSnapshot]:
        result = list(products)
        seen_keys = {product.product_key for product in result}

        for old in existing.values():
            if old.product_key in seen_keys or not old.in_stock:
                continue
            variants = tuple(
                variant.strip()
                for variant in (old.available_variants or "").split(",")
                if variant.strip()
            )
            result.append(
                ProductSnapshot(
                    product_key=old.product_key,
                    title=old.title,
                    price=old.price,
                    image=old.image,
                    product_url=old.product_url,
                    in_stock=False,
                    available_variants=variants,
                    hidden=old.hidden,
                )
            )
        return result

    async def _confirm_events(
        self,
        watch: Watch,
        existing: dict[str, StoredProduct],
        candidates: list[ProductEvent],
    ) -> tuple[list[ProductEvent], list[ProductSnapshot]]:
        candidate_signatures = {(event.product.product_key, event.event_type) for event in candidates}
        self.logger.info(
            "%s changement(s) candidat(s) detecte(s) pour %s, double-check dans %ss",
            len(candidates),
            watch.url,
            CONFIRMATION_DELAY_SECONDS,
        )
        await asyncio.sleep(CONFIRMATION_DELAY_SECONDS)

        confirmed_result = await self.fetch_products_once(watch.url)
        confirmed_products = self._add_missing_products_as_out_of_stock(confirmed_result.products, existing)
        confirmed_events = detect_changes(confirmed_products, existing)
        stable_events = [
            event
            for event in confirmed_events
            if (event.product.product_key, event.event_type) in candidate_signatures
        ]
        self.logger.info(
            "%s/%s changement(s) confirme(s) pour %s",
            len(stable_events),
            len(candidates),
            watch.url,
        )
        return stable_events, confirmed_products

    @staticmethod
    def _is_event_on_cooldown(
        event: ProductEvent,
        existing: dict[str, StoredProduct],
    ) -> bool:
        old = existing.get(event.product.product_key)
        if old is None or old.last_change_type != event.event_type or not old.last_alerted_at:
            return False

        try:
            last_alerted_at = datetime.fromisoformat(old.last_alerted_at)
        except ValueError:
            return False
        if last_alerted_at.tzinfo is None:
            last_alerted_at = last_alerted_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last_alerted_at < timedelta(seconds=EVENT_COOLDOWN_SECONDS)


class ShopMonitorBot(discord.Client):
    def __init__(self, db: Database, default_interval: int, max_watches_per_guild: int, debug_mode: bool) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = db
        self.default_interval = max(default_interval, MIN_INTERVAL_SECONDS)
        self.max_watches_per_guild = max_watches_per_guild
        self.debug_mode = debug_mode
        self.monitor = MonitorManager(self, db, debug_mode)
        self._started_monitor = False

    async def setup_hook(self) -> None:
        self.db.init()
        self.add_view(DashboardView(self, self.db, self.monitor))
        setup_commands(self, self.db, self.monitor, self.default_interval, self.max_watches_per_guild)
        synced = await self.tree.sync()
        logging.getLogger("shop-monitor.bot").info("%s commande(s) slash synchronisee(s)", len(synced))
        for guild_id in self.db.list_guild_ids():
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                guild_synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                logging.getLogger("shop-monitor.bot").warning(
                    "Sync slash ignoree pour guild inaccessible %s",
                    guild_id,
                )
                continue
            logging.getLogger("shop-monitor.bot").info(
                "%s commande(s) slash synchronisee(s) pour guild %s",
                len(guild_synced),
                guild_id,
            )

    async def on_ready(self) -> None:
        logging.getLogger("shop-monitor.bot").info("Connecte en tant que %s", self.user)
        if not self._started_monitor:
            self.monitor.start_all()
            self._started_monitor = True
            await refresh_saved_dashboards(self, self.db, self.monitor)
            for guild in self.guilds:
                await send_log(
                    self,
                    f"Bot demarre. Watches actives chargees: {len(self.monitor.tasks)}.",
                    "info",
                    guild,
                    db=self.db,
                    action="Demarrage bot",
                    force=True,
                )

    async def close(self) -> None:
        await self.monitor.close()
        self.db.close()
        await super().close()


def main() -> None:
    load_dotenv()
    configure_logging()

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN est manquant. Copiez .env.example vers .env et ajoutez le token.")

    default_interval = int(os.getenv("DEFAULT_INTERVAL", "15"))
    max_watches_per_guild = int(os.getenv("MAX_WATCHES_PER_GUILD", "20"))
    debug_mode = os.getenv("DEBUG_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
    db = Database("data.db")
    bot = ShopMonitorBot(db, default_interval, max_watches_per_guild, debug_mode)
    bot.run(token)


if __name__ == "__main__":
    main()
