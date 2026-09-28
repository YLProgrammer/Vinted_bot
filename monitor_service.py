"""Background monitoring service, kept independent from Telegram UI handlers."""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from config import (
    DATA_RETENTION_DAYS, DELAY_BETWEEN_SEARCHES, HEALTHCHECK_URL,
    MAX_SEND_FAILURES_BEFORE_AUTOPAUSE, MONITOR_CYCLE_SECONDS,
    MONITOR_MAX_WORKERS, PURGE_INTERVAL_SECONDS, REAL_DEAL_MIN_SAMPLES,
    REAL_DEAL_THRESHOLD,
)
from db import (
    get_connection, get_plage_horaire, heure_dans_plage,
    maj_prix_wishlist, maj_stats_prix_recherche, purger_anciennes_donnees,
    purger_premium_expire, toute_la_wishlist, toutes_les_veilles_vendeur_actives,
)
from logger_config import get_logger
from vinted_scraper import chercher_items_vendeur, chercher_vinted, obtenir_prix_item

logger = get_logger("monitor")


def parse_price(value):
    try:
        return float(str(value).replace("€", "").replace(" ", "").replace(",", "."))
    except (ValueError, AttributeError):
        return 0.0


def filter_excluded_keywords(items, exclude_keywords):
    if not exclude_keywords or not items:
        return items
    words = [word.strip().lower() for word in exclude_keywords.split(",") if word.strip()]
    if not words:
        return items
    return [item for item in items if not any(word in item["titre"].lower() for word in words)]


def filter_duplicates_and_price_drops(chat_id, items):
    """Persist new listings and return ``(new_items, price_drops)`` atomically."""
    if not items:
        return [], []
    new_items, price_drops = [], []
    with get_connection() as conn:
        cursor = conn.cursor()
        for item in items:
            price = parse_price(item["prix"])
            seen = cursor.execute(
                "SELECT id FROM seen_items WHERE chat_id = ? AND item_url = ?", (chat_id, item["lien"])
            ).fetchone()
            previous = cursor.execute(
                "SELECT price_value FROM item_prices WHERE chat_id = ? AND item_url = ?", (chat_id, item["lien"])
            ).fetchone()
            if not seen:
                cursor.execute(
                    "INSERT INTO seen_items (chat_id, item_url, created_at) VALUES (?, ?, datetime('now'))",
                    (chat_id, item["lien"]),
                )
                if not previous:
                    cursor.execute(
                        "INSERT INTO item_prices (chat_id, item_url, price_value, created_at) VALUES (?, ?, ?, datetime('now'))",
                        (chat_id, item["lien"], price),
                    )
                new_items.append(item)
            elif previous and price > 0 and previous[0] > 0 and price < previous[0]:
                price_drops.append({
                    "titre": item["titre"], "ancien_prix": previous[0], "nouveau_prix": item["prix"],
                    "lien": item["lien"], "image": item.get("image"),
                })
                cursor.execute(
                    "UPDATE item_prices SET price_value = ? WHERE chat_id = ? AND item_url = ?",
                    (price, chat_id, item["lien"]),
                )
    return new_items, price_drops


class MonitorService:
    """Runs scans while the Telegram module remains responsible for presentation."""

    def __init__(self, send_alert, send_message, send_photo):
        self.send_alert = send_alert
        self.send_message = send_message
        self.send_photo = send_photo
        self._last_purge_ts = 0

    def _record_delivery(self, chat_id, succeeded):
        with get_connection() as conn:
            if succeeded:
                conn.execute("UPDATE users SET consecutive_send_failures = 0 WHERE chat_id = ?", (chat_id,))
                return
            conn.execute(
                "UPDATE users SET consecutive_send_failures = consecutive_send_failures + 1 WHERE chat_id = ?", (chat_id,)
            )
            failures = conn.execute(
                "SELECT consecutive_send_failures FROM users WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if failures and failures[0] >= MAX_SEND_FAILURES_BEFORE_AUTOPAUSE:
                conn.execute("UPDATE searches SET is_paused = 1 WHERE chat_id = ?", (chat_id,))
                logger.warning("User %s is unreachable; searches were paused.", chat_id)

    def _process_search(self, row):
        search_id, chat_id, query, min_price, max_price, size, status, limit_count, exclude_keywords = row
        time.sleep(random.uniform(0, DELAY_BETWEEN_SEARCHES))
        results = filter_excluded_keywords(
            chercher_vinted(query, min_price, max_price, size, status, limit_count), exclude_keywords
        )
        new_items, drops = filter_duplicates_and_price_drops(chat_id, results)
        for item in new_items:
            price = parse_price(item["prix"])
            average, count = maj_stats_prix_recherche(search_id, price) if price else (None, 0)
            item["bonne_affaire"] = bool(
                price and average and count >= REAL_DEAL_MIN_SAMPLES and price < REAL_DEAL_THRESHOLD * average
            )
        if new_items:
            self._record_delivery(chat_id, self.send_alert(chat_id, new_items))
        for drop in drops:
            text = (
                f"📉 *BAISSE DE PRIX DÉTECTÉE !*\n\n🚨 **{drop['titre']}**\n"
                f"💰 Ancien : {drop['ancien_prix']}€ ➡️ **Nouveau : {drop['nouveau_prix']}**\n"
                f"[👉 Saisir l'affaire]({drop['lien']})"
            )
            succeeded = self.send_photo(chat_id, drop["image"], text) if drop.get("image") else self.send_message(chat_id, text)
            self._record_delivery(chat_id, succeeded)

    def _process_seller_watch(self, row):
        _, chat_id, seller_id, seller_label, limit_count = row
        time.sleep(random.uniform(0, DELAY_BETWEEN_SEARCHES))
        new_items, _ = filter_duplicates_and_price_drops(chat_id, chercher_items_vendeur(seller_id, limit_count))
        if new_items:
            for item in new_items:
                item["titre"] = f"[👤 {seller_label}] {item['titre']}"
            self._record_delivery(chat_id, self.send_alert(chat_id, new_items))

    def _check_wishlist(self):
        for item_id, chat_id, item_url, title, last_price in toute_la_wishlist():
            try:
                current_title, current_price = obtenir_prix_item(item_url)
            except Exception:
                logger.exception("Wishlist check failed for item %s", item_id)
                continue
            if current_price is None:
                continue
            if last_price and current_price < last_price:
                succeeded = self.send_message(
                    chat_id, f"❤️📉 *Baisse de prix sur ta wishlist !*\n\n**{title or current_title}**\n"
                    f"💰 {last_price}€ ➡️ **{current_price}€**\n[👉 Voir l'annonce]({item_url})",
                )
                self._record_delivery(chat_id, succeeded)
            if current_price != last_price:
                maj_prix_wishlist(item_id, current_price)

    @staticmethod
    def _in_monitoring_hours():
        start, end = get_plage_horaire()
        return heure_dans_plage(time.localtime().tm_hour, start, end)

    @staticmethod
    def _ping_heartbeat():
        if not HEALTHCHECK_URL:
            return
        try:
            requests.get(HEALTHCHECK_URL, timeout=10)
        except requests.RequestException:
            logger.warning("Heartbeat ping failed", exc_info=True)

    def run_cycle(self):
        purger_premium_expire()
        if time.time() - self._last_purge_ts > PURGE_INTERVAL_SECONDS:
            removed = purger_anciennes_donnees(DATA_RETENTION_DAYS)
            if removed:
                logger.info("Automatic purge removed %s row(s).", removed)
            self._last_purge_ts = time.time()
        if not self._in_monitoring_hours():
            start, end = get_plage_horaire()
            logger.info("Monitoring skipped outside configured hours (%sh-%sh).", start, end)
            self._ping_heartbeat()
            return
        with get_connection() as conn:
            searches = conn.execute(
                "SELECT id, chat_id, query, min_price, max_price, size, status, limit_count, exclude_keywords "
                "FROM searches WHERE is_paused = 0"
            ).fetchall()
        for rows, handler in ((searches, self._process_search), (toutes_les_veilles_vendeur_actives(), self._process_seller_watch)):
            if rows:
                with ThreadPoolExecutor(max_workers=MONITOR_MAX_WORKERS) as executor:
                    futures = [executor.submit(handler, row) for row in rows]
                    for future in futures:
                        try:
                            future.result()
                        except Exception:
                            logger.exception("A monitoring job failed; other jobs will continue.")
        self._check_wishlist()
        self._ping_heartbeat()

    def run_forever(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                self.run_cycle()
            except Exception:
                logger.exception("Monitoring cycle failed.")
            stop_event.wait(MONITOR_CYCLE_SECONDS)
