from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup


LOGGER = logging.getLogger("shop-monitor.scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15.0
RETRY_STATUS_CODES = {403, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class ProductSnapshot:
    product_key: str
    title: str
    price: str | None
    image: str | None
    product_url: str
    in_stock: bool
    available_variants: tuple[str, ...] = ()
    hidden: bool = False


@dataclass(slots=True)
class FetchStats:
    requests: int = 0
    http_time_seconds: float = 0.0
    source: str = "unknown"


@dataclass(slots=True)
class ScrapeResult:
    products: list[ProductSnapshot]
    stats: FetchStats = field(default_factory=FetchStats)


def create_http_client() -> httpx.AsyncClient:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    return httpx.AsyncClient(
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        limits=limits,
    )


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = urlparse(f"https://{url.strip()}")
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", parsed.query, ""))


def is_valid_shopify_watch_url(url: str) -> bool:
    parsed = urlparse(normalize_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return (
        parsed.path == "/"
        or "/collections/" in parsed.path
        or "/products/" in parsed.path
    )


async def _fetch(client: httpx.AsyncClient, url: str, stats: FetchStats | None = None) -> httpx.Response:
    delay = 1.0
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            started = time.perf_counter()
            response = await client.get(url)
            elapsed = time.perf_counter() - started
            if stats is not None:
                stats.requests += 1
                stats.http_time_seconds += elapsed
            if response.status_code in RETRY_STATUS_CODES:
                LOGGER.warning("HTTP %s pour %s, tentative %s/3", response.status_code, url, attempt)
                if attempt < 3:
                    retry_after = _retry_after_seconds(response) or delay
                    await asyncio.sleep(retry_after)
                    delay *= 2
                    continue
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_error = exc
            LOGGER.warning("Erreur reseau pour %s, tentative %s/3: %s", url, attempt, exc)
            if attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Impossible de recuperer {url}")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return min(max(float(header), 1.0), 30.0)
    except ValueError:
        return None


def _shopify_json_candidates(url: str) -> list[str]:
    parsed = urlparse(normalize_url(url))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates: list[str] = []

    if "/collections/" in path:
        candidates.append(f"{origin}{path}/products.json?limit=250")
        candidates.append(f"{origin}/products.json?limit=250")
    elif "/products/" in path:
        handle = path.split("/products/", 1)[1].strip("/")
        if handle:
            candidates.append(f"{origin}/products/{handle}.js")
        candidates.append(f"{origin}/products.json?limit=250")
    else:
        candidates.append(f"{origin}/products.json?limit=250")

    return candidates


def _watched_product_handle(url: str) -> str | None:
    path = urlparse(normalize_url(url)).path.rstrip("/")
    if "/products/" not in path:
        return None
    return path.split("/products/", 1)[1].strip("/") or None


def _format_price(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace(",", ".")


def _price_number(value: str) -> Decimal:
    normalized = re.sub(r"[^0-9.]", "", value.replace(",", "."))
    try:
        return Decimal(normalized or "0")
    except InvalidOperation:
        return Decimal("0")


def _absolute_image(src: str | None, base_url: str) -> str | None:
    if not src:
        return None
    if src.startswith("//"):
        return f"https:{src}"
    return urljoin(base_url, src)


def _variant_label(variant: dict) -> str:
    parts = [
        str(variant.get(key) or "").strip()
        for key in ("title", "option1", "option2", "option3")
        if str(variant.get(key) or "").strip()
    ]
    if not parts:
        return str(variant.get("id") or "Variante")
    label = " / ".join(dict.fromkeys(parts))
    return "Default Title" if label == "Default Title / Default Title" else label


def _available_variants(variants: list[dict]) -> tuple[str, ...]:
    available: list[str] = []
    for variant in variants:
        if variant.get("available") is True or str(variant.get("available")).lower() == "true":
            available.append(_variant_label(variant))
            continue
        quantity = variant.get("inventory_quantity")
        try:
            if quantity is not None and int(quantity) > 0:
                available.append(_variant_label(variant))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(available))


def parse_products(payload: dict, base_url: str) -> list[ProductSnapshot]:
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    products: list[ProductSnapshot] = []
    items = payload.get("products")
    if items is None and "product" in payload:
        items = [payload["product"]]
    elif items is None and ("variants" in payload or "handle" in payload):
        items = [payload]

    for item in items or []:
        product_id = item.get("id")
        handle = item.get("handle") or str(product_id)
        title = unescape(str(item.get("title") or "Produit sans titre")).strip()
        variants = item.get("variants") or []
        images = item.get("images") or []

        prices = [_format_price(variant.get("price")) for variant in variants if variant.get("price") is not None]
        prices = [price for price in prices if price]
        price = min(prices, key=_price_number) if prices else _format_price(item.get("price"))

        available_variants = _available_variants(variants)
        in_stock = bool(available_variants) or _shopify_in_stock(variants)
        hidden = item.get("published_at") is None and "published_at" in item
        image = None
        if images:
            first_image = images[0]
            image = _absolute_image(first_image.get("src") if isinstance(first_image, dict) else str(first_image), origin)
        elif item.get("image"):
            item_image = item["image"]
            image = _absolute_image(item_image.get("src") if isinstance(item_image, dict) else str(item_image), origin)

        products.append(
            ProductSnapshot(
                product_key=str(product_id or handle),
                title=title,
                price=price,
                image=image,
                product_url=f"{origin}/products/{handle}",
                in_stock=in_stock,
                available_variants=available_variants,
                hidden=hidden,
            )
        )

    return products


_parse_shopify_products = parse_products


def _shopify_in_stock(variants: list[dict]) -> bool:
    if not variants:
        return False

    availability_values = [variant.get("available") for variant in variants if "available" in variant]
    if availability_values:
        return any(value is True or str(value).lower() == "true" for value in availability_values)

    for variant in variants:
        quantity = variant.get("inventory_quantity")
        try:
            if quantity is not None and int(quantity) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _extract_json_ld_products(soup: BeautifulSoup, base_url: str) -> list[ProductSnapshot]:
    products: list[ProductSnapshot] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue

        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            graph = node.get("@graph", []) if isinstance(node, dict) else []
            candidates = graph or [node]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                type_value = candidate.get("@type")
                types = type_value if isinstance(type_value, list) else [type_value]
                if "Product" not in types:
                    continue

                title = str(candidate.get("name") or "Produit sans titre").strip()
                product_url = urljoin(base_url, str(candidate.get("url") or base_url))
                image_value = candidate.get("image")
                image = image_value[0] if isinstance(image_value, list) and image_value else image_value
                offers = candidate.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}

                availability = str(offers.get("availability") or "").lower() if isinstance(offers, dict) else ""
                price = _format_price(offers.get("price")) if isinstance(offers, dict) else None

                products.append(
                    ProductSnapshot(
                        product_key=product_url,
                        title=title,
                        price=price,
                        image=_absolute_image(str(image), base_url) if image else None,
                        product_url=product_url,
                        in_stock="instock" in availability or "in_stock" in availability,
                        available_variants=(),
                        hidden=False,
                    )
                )
    return products


def _text_price(card: BeautifulSoup) -> str | None:
    selectors = [
        "[class*=price]",
        "[data-price]",
        ".money",
    ]
    for selector in selectors:
        node = card.select_one(selector)
        if not node:
            continue
        raw = node.get("data-price") or node.get_text(" ", strip=True)
        match = re.search(r"(\d+(?:[,.]\d{1,2})?)", str(raw))
        if match:
            return match.group(1).replace(",", ".")
    return None


def _parse_html_products(html: str, base_url: str) -> list[ProductSnapshot]:
    soup = BeautifulSoup(html, "html.parser")
    json_ld_products = _extract_json_ld_products(soup, base_url)
    if json_ld_products:
        return _dedupe_products(json_ld_products)

    cards = soup.select(
        "[class*=product-card], [class*=grid-product], [class*=product-item], "
        "li[class*=product], article[class*=product]"
    )
    products: list[ProductSnapshot] = []

    for card in cards:
        link = card.select_one('a[href*="/products/"]') or card.find("a", href=True)
        if not link:
            continue

        href = str(link.get("href"))
        product_url = urljoin(base_url, href)
        title_node = card.select_one("[class*=title], [class*=name], h2, h3")
        title = title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True)
        title = unescape(title).strip() or product_url.rstrip("/").split("/")[-1].replace("-", " ").title()

        image_node = card.select_one("img")
        image = None
        if image_node:
            image = (
                image_node.get("src")
                or image_node.get("data-src")
                or image_node.get("data-original")
                or image_node.get("srcset", "").split(" ")[0]
            )

        card_text = _ascii_text(card.get_text(" ", strip=True).lower())
        sold_out_words = ("sold out", "rupture", "epuise", "epuise", "indisponible", "out of stock")
        in_stock = not any(word in card_text for word in sold_out_words)

        products.append(
            ProductSnapshot(
                product_key=product_url,
                title=title,
                price=_text_price(card),
                image=_absolute_image(image, base_url),
                product_url=product_url,
                in_stock=in_stock,
                available_variants=(),
                hidden=False,
            )
        )

    return _dedupe_products(products)


def _ascii_text(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def _dedupe_products(products: list[ProductSnapshot]) -> list[ProductSnapshot]:
    seen: set[str] = set()
    result: list[ProductSnapshot] = []
    for product in products:
        if product.product_key in seen:
            continue
        seen.add(product.product_key)
        result.append(product)
    return result


async def fetch_shopify_json(
    client: httpx.AsyncClient,
    url: str,
    stats: FetchStats | None = None,
) -> list[ProductSnapshot]:
    watched_handle = _watched_product_handle(url)
    for candidate in _shopify_json_candidates(url):
        try:
            response = await _fetch(client, candidate, stats)
            payload = response.json()
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            LOGGER.info("Endpoint Shopify non utilisable pour %s: %s", candidate, exc)
            continue

        products = parse_products(payload, url)
        if watched_handle and candidate.endswith("/products.json?limit=250"):
            products = [
                product
                for product in products
                if product.product_url.rstrip("/").endswith(f"/products/{watched_handle}")
            ]
        if products:
            if stats is not None:
                stats.source = "shopify_json"
            LOGGER.info("%s produits trouves via Shopify JSON pour %s", len(products), url)
            return _dedupe_products(products)

    return []


async def get_products(url: str, client: httpx.AsyncClient | None = None) -> list[ProductSnapshot]:
    """Recupere les produits d'une URL. Modifier cette fonction pour ajouter un site specifique."""
    result = await scrape_products(url, client)
    return result.products


async def scrape_products(url: str, client: httpx.AsyncClient | None = None) -> ScrapeResult:
    normalized = normalize_url(url)
    stats = FetchStats()

    if client is None:
        async with create_http_client() as scoped_client:
            return await scrape_products(normalized, scoped_client)

    products = await fetch_shopify_json(client, normalized, stats)
    if products:
        return ScrapeResult(products, stats)

    response = await _fetch(client, normalized, stats)
    products = _parse_html_products(response.text, str(response.url))
    stats.source = "html"
    LOGGER.info("%s produits trouves via HTML pour %s", len(products), normalized)
    return ScrapeResult(products, stats)
