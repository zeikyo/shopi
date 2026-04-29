from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Watch:
    id: int
    guild_id: int
    url: str
    channel_id: int
    interval_seconds: int
    enabled: bool
    created_at: str
    last_checked_at: str | None
    ping_role_id: int | None
    error_count: int


@dataclass(slots=True)
class StoredProduct:
    id: int
    watch_id: int
    product_key: str
    title: str
    price: str | None
    image: str | None
    product_url: str
    in_stock: bool
    last_seen: str
    last_change_type: str | None
    last_alerted_at: str | None
    available_variants: str | None
    hidden: bool


@dataclass(slots=True)
class GuildSettings:
    guild_id: int
    logs_channel_id: int | None
    dashboard_channel_id: int | None
    dashboard_message_id: int | None
    created_at: str
    updated_at: str


class Database:
    def __init__(self, path: str | Path = "data.db") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    ping_role_id INTEGER,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(guild_id, url)
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER NOT NULL,
                    product_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price TEXT,
                    image TEXT,
                    product_url TEXT NOT NULL,
                    in_stock INTEGER NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_change_type TEXT,
                    last_alerted_at TEXT,
                    available_variants TEXT,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(watch_id, product_key),
                    FOREIGN KEY(watch_id) REFERENCES watches(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS bot_stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    logs_channel_id INTEGER,
                    dashboard_channel_id INTEGER,
                    dashboard_message_id INTEGER,
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_products_watch_id ON products(watch_id);
                CREATE INDEX IF NOT EXISTS idx_products_product_key ON products(product_key);
                CREATE INDEX IF NOT EXISTS idx_watches_guild_url ON watches(guild_id, url);
                """
            )
            self._ensure_column("watches", "ping_role_id", "INTEGER")
            self._ensure_column("watches", "error_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("products", "last_change_type", "TEXT")
            self._ensure_column("products", "last_alerted_at", "TEXT")
            self._ensure_column("products", "available_variants", "TEXT")
            self._ensure_column("products", "hidden", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("guild_settings", "dashboard_channel_id", "INTEGER")
            self._ensure_column("guild_settings", "dashboard_message_id", "INTEGER")
            self._ensure_column("guild_settings", "created_at", "TEXT")
            self._ensure_column("guild_settings", "updated_at", "TEXT")
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _watch_from_row(row: sqlite3.Row | None) -> Watch | None:
        if row is None:
            return None
        return Watch(
            id=row["id"],
            guild_id=row["guild_id"],
            url=row["url"],
            channel_id=row["channel_id"],
            interval_seconds=row["interval_seconds"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_checked_at=row["last_checked_at"],
            ping_role_id=row["ping_role_id"],
            error_count=row["error_count"],
        )

    @staticmethod
    def _product_from_row(row: sqlite3.Row) -> StoredProduct:
        return StoredProduct(
            id=row["id"],
            watch_id=row["watch_id"],
            product_key=row["product_key"],
            title=row["title"],
            price=row["price"],
            image=row["image"],
            product_url=row["product_url"],
            in_stock=bool(row["in_stock"]),
            last_seen=row["last_seen"],
            last_change_type=row["last_change_type"],
            last_alerted_at=row["last_alerted_at"],
            available_variants=row["available_variants"],
            hidden=bool(row["hidden"]),
        )

    def add_watch(
        self,
        guild_id: int,
        url: str,
        channel_id: int,
        interval_seconds: int,
        ping_role_id: int | None = None,
    ) -> Watch:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO watches
                    (guild_id, url, channel_id, interval_seconds, enabled, created_at, ping_role_id)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(guild_id, url) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    interval_seconds = excluded.interval_seconds,
                    enabled = 1,
                    ping_role_id = excluded.ping_role_id
                """,
                (guild_id, url, channel_id, interval_seconds, now, ping_role_id),
            )
            self._conn.commit()
            watch = self.get_watch_by_url(guild_id, url)
            if watch is None:
                raise RuntimeError("Impossible de relire la surveillance apres insertion.")
            return watch

    def delete_watch(self, guild_id: int, url: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM watches WHERE guild_id = ? AND url = ?",
                (guild_id, url),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_watch(self, watch_id: int) -> Watch | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
            return self._watch_from_row(row)

    def get_watch_by_url(self, guild_id: int, url: str) -> Watch | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watches WHERE guild_id = ? AND url = ?",
                (guild_id, url),
            ).fetchone()
            return self._watch_from_row(row)

    def list_watches(self, guild_id: int) -> list[Watch]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watches WHERE guild_id = ? ORDER BY created_at DESC",
                (guild_id,),
            ).fetchall()
            return [self._watch_from_row(row) for row in rows if row is not None]

    def list_enabled_watches(self) -> list[Watch]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watches WHERE enabled = 1 ORDER BY id"
            ).fetchall()
            return [self._watch_from_row(row) for row in rows if row is not None]

    def list_guild_ids(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT guild_id FROM watches
                UNION
                SELECT guild_id FROM guild_settings WHERE logs_channel_id IS NOT NULL
                ORDER BY guild_id
                """
            ).fetchall()
            return [int(row["guild_id"]) for row in rows]

    @staticmethod
    def _settings_from_row(row: sqlite3.Row | None) -> GuildSettings | None:
        if row is None:
            return None
        return GuildSettings(
            guild_id=int(row["guild_id"]),
            logs_channel_id=int(row["logs_channel_id"]) if row["logs_channel_id"] is not None else None,
            dashboard_channel_id=int(row["dashboard_channel_id"]) if row["dashboard_channel_id"] is not None else None,
            dashboard_message_id=int(row["dashboard_message_id"]) if row["dashboard_message_id"] is not None else None,
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def get_guild_settings(self, guild_id: int) -> GuildSettings | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            return self._settings_from_row(row)

    def list_dashboard_settings(self) -> list[GuildSettings]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM guild_settings
                WHERE dashboard_channel_id IS NOT NULL
                  AND dashboard_message_id IS NOT NULL
                """
            ).fetchall()
            return [settings for row in rows if (settings := self._settings_from_row(row)) is not None]

    def set_logs_channel(self, guild_id: int, channel_id: int | None) -> None:
        now = utc_now()
        with self._lock:
            if channel_id is None:
                self._conn.execute("UPDATE guild_settings SET logs_channel_id = NULL, updated_at = ? WHERE guild_id = ?", (now, guild_id))
                self._conn.commit()
                return
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, logs_channel_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    logs_channel_id = excluded.logs_channel_id,
                    updated_at = excluded.updated_at
                """,
                (guild_id, channel_id, now, now),
            )
            self._conn.commit()

    def get_logs_channel_id(self, guild_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT logs_channel_id FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if row is None or row["logs_channel_id"] is None:
                return None
            return int(row["logs_channel_id"])

    def set_dashboard_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings
                    (guild_id, dashboard_channel_id, dashboard_message_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    dashboard_channel_id = excluded.dashboard_channel_id,
                    dashboard_message_id = excluded.dashboard_message_id,
                    updated_at = excluded.updated_at
                """,
                (guild_id, channel_id, message_id, now, now),
            )
            self._conn.commit()

    def clear_dashboard_message(self, guild_id: int) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE guild_settings
                SET dashboard_channel_id = NULL,
                    dashboard_message_id = NULL,
                    updated_at = ?
                WHERE guild_id = ?
                """,
                (now, guild_id),
            )
            self._conn.commit()

    def set_channel(self, guild_id: int, url: str, channel_id: int) -> Watch | None:
        with self._lock:
            self._conn.execute(
                "UPDATE watches SET channel_id = ? WHERE guild_id = ? AND url = ?",
                (channel_id, guild_id, url),
            )
            self._conn.commit()
            return self.get_watch_by_url(guild_id, url)

    def set_interval(self, guild_id: int, url: str, interval_seconds: int) -> Watch | None:
        with self._lock:
            self._conn.execute(
                "UPDATE watches SET interval_seconds = ? WHERE guild_id = ? AND url = ?",
                (interval_seconds, guild_id, url),
            )
            self._conn.commit()
            return self.get_watch_by_url(guild_id, url)

    def set_enabled(self, guild_id: int, url: str, enabled: bool) -> Watch | None:
        with self._lock:
            self._conn.execute(
                "UPDATE watches SET enabled = ?, error_count = CASE WHEN ? = 1 THEN 0 ELSE error_count END WHERE guild_id = ? AND url = ?",
                (1 if enabled else 0, 1 if enabled else 0, guild_id, url),
            )
            self._conn.commit()
            return self.get_watch_by_url(guild_id, url)

    def increment_error_count(self, watch_id: int) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE watches SET error_count = error_count + 1 WHERE id = ?",
                (watch_id,),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT error_count FROM watches WHERE id = ?", (watch_id,)).fetchone()
            return int(row["error_count"]) if row else 0

    def reset_error_count(self, watch_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE watches SET error_count = 0 WHERE id = ?", (watch_id,))
            self._conn.commit()

    def auto_pause_watch(self, watch_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE watches SET enabled = 0 WHERE id = ?", (watch_id,))
            self._conn.commit()

    def set_ping_role(self, guild_id: int, url: str, role_id: int | None) -> Watch | None:
        with self._lock:
            self._conn.execute(
                "UPDATE watches SET ping_role_id = ? WHERE guild_id = ? AND url = ?",
                (role_id, guild_id, url),
            )
            self._conn.commit()
            return self.get_watch_by_url(guild_id, url)

    def get_products_for_watch(self, watch_id: int) -> dict[str, StoredProduct]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM products WHERE watch_id = ?",
                (watch_id,),
            ).fetchall()
            return {row["product_key"]: self._product_from_row(row) for row in rows}

    def count_watches(self, guild_id: int, enabled: bool | None = None) -> int:
        with self._lock:
            if enabled is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS total FROM watches WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS total FROM watches WHERE guild_id = ? AND enabled = ?",
                    (guild_id, 1 if enabled else 0),
                ).fetchone()
            return int(row["total"])

    def count_products(self, guild_id: int | None = None) -> int:
        with self._lock:
            if guild_id is None:
                row = self._conn.execute("SELECT COUNT(*) AS total FROM products").fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM products p
                    JOIN watches w ON w.id = p.watch_id
                    WHERE w.guild_id = ?
                    """,
                    (guild_id,),
                ).fetchone()
            return int(row["total"])

    def increment_alerts_sent(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bot_stats (key, value) VALUES ('alerts_sent', ?)
                ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
                """,
                (count,),
            )
            self._conn.commit()

    def get_alerts_sent(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM bot_stats WHERE key = 'alerts_sent'"
            ).fetchone()
            return int(row["value"]) if row else 0

    def upsert_products(
        self,
        watch_id: int,
        products: Iterable[object],
        change_types: dict[str, str] | None = None,
    ) -> None:
        now = utc_now()
        change_types = change_types or {}
        with self._lock:
            existing = self.get_products_for_watch(watch_id)
            for product in products:
                change_type = change_types.get(product.product_key)
                available_variants = ", ".join(product.available_variants) if product.available_variants else None
                old = existing.get(product.product_key)
                if (
                    old is not None
                    and change_type is None
                    and old.title == product.title
                    and old.price == product.price
                    and old.image == product.image
                    and old.product_url == product.product_url
                    and old.in_stock == product.in_stock
                    and old.available_variants == available_variants
                    and old.hidden == product.hidden
                ):
                    continue
                self._conn.execute(
                    """
                    INSERT INTO products
                        (
                            watch_id, product_key, title, price, image, product_url,
                            in_stock, last_seen, last_change_type, last_alerted_at,
                            available_variants, hidden
                        )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(watch_id, product_key) DO UPDATE SET
                        title = excluded.title,
                        price = excluded.price,
                        image = excluded.image,
                        product_url = excluded.product_url,
                        in_stock = excluded.in_stock,
                        last_seen = excluded.last_seen,
                        last_change_type = COALESCE(excluded.last_change_type, products.last_change_type),
                        last_alerted_at = COALESCE(excluded.last_alerted_at, products.last_alerted_at),
                        available_variants = excluded.available_variants,
                        hidden = excluded.hidden
                    """,
                    (
                        watch_id,
                        product.product_key,
                        product.title,
                        product.price,
                        product.image,
                        product.product_url,
                        1 if product.in_stock else 0,
                        now,
                        change_type,
                        now if change_type else None,
                        available_variants,
                        1 if product.hidden else 0,
                    ),
                )
            self._conn.execute(
                "UPDATE watches SET last_checked_at = ? WHERE id = ?",
                (now, watch_id),
            )
            self._conn.commit()
