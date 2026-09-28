"""
Configuration centralisée du projet VintedPulse.

Toutes les valeurs sensibles (tokens Telegram, chat_id dev) sont chargées
depuis un fichier .env (voir .env.example) et ne doivent JAMAIS être codées
en dur dans les fichiers .py, ni committées sur un dépôt Git.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Variable d'environnement '{name}' manquante. "
            f"Copie .env.example vers .env et remplis les valeurs avant de relancer."
        )
    return value


# --- Tokens des 3 bots Telegram ---
CENTRAL_BOT_TOKEN = _require("CENTRAL_BOT_TOKEN")
SENDER_BOT_TOKEN = _require("SENDER_BOT_TOKEN")
DEV_BOT_TOKEN = _require("DEV_BOT_TOKEN")
DEV_CHAT_ID = _require("DEV_CHAT_ID")

URL_TELEGRAM_CENTRAL = f"https://api.telegram.org/bot{CENTRAL_BOT_TOKEN}/"
URL_TELEGRAM_SENDER = f"https://api.telegram.org/bot{SENDER_BOT_TOKEN}/"
URL_TELEGRAM_DEV = f"https://api.telegram.org/bot{DEV_BOT_TOKEN}/"

# Nom d'utilisateur du bot d'alertes (sans @), utilisé pour générer les liens
# d'invitation/parrainage. Ajoute SENDER_BOT_USERNAME dans .env si besoin de
# le changer (par défaut : VintedPulseBot).
SENDER_BOT_USERNAME = os.environ.get("SENDER_BOT_USERNAME", "VintedPulseBot")

# --- Base de données & tokens Premium ---
DB_PATH = os.environ.get("DB_PATH", "vinted_bot.db")
TOKENS_FILE = os.environ.get("TOKENS_FILE", "tokens.txt")

# --- Paramètres fonctionnels ---
FREE_PLAN_MAX_SEARCHES = 1
MONITOR_CYCLE_SECONDS = 180          # pause entre deux cycles complets de veille
DELAY_BETWEEN_SEARCHES = 5           # petit délai aléatoire max avant chaque recherche (anti-rafale)
MAX_SEND_FAILURES_BEFORE_AUTOPAUSE = 5   # nb d'échecs Telegram consécutifs avant mise en pause auto
VINTED_SESSION_TTL_SECONDS = 300     # durée de vie du cookie/csrf Vinted en cache
MONITOR_MAX_WORKERS = 3              # nb de recherches traitées en parallèle

# --- Logs (fichier avec rotation, voir logger_config.py) ---
LOG_FILE = os.environ.get("LOG_FILE", "vintedpulse.log")

# --- Purge automatique de la DB ---
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))
PURGE_INTERVAL_SECONDS = 24 * 3600   # une purge par jour suffit

# --- Monitoring / heartbeat externe (ex: healthchecks.io) ---
# Optionnel : si vide, aucun ping n'est envoyé. Colle l'URL de ping fournie
# par ton service de monitoring dans .env sous HEALTHCHECK_URL.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

# --- Plage horaire de veille (heure locale du serveur) ---
# En dehors de cette plage, le moniteur ne scanne pas (utile pour ne pas
# notifier en pleine nuit). Mets 0 et 24 pour désactiver la restriction.
MONITOR_HOUR_START = int(os.environ.get("MONITOR_HOUR_START", "8"))
MONITOR_HOUR_END = int(os.environ.get("MONITOR_HOUR_END", "23"))

# --- Détection de "vraie" bonne affaire ---
# Un article est signalé comme "vraie bonne affaire" s'il coûte moins de
# REAL_DEAL_THRESHOLD * (prix moyen observé pour cette recherche), à partir
# du moment où on a au moins REAL_DEAL_MIN_SAMPLES prix en mémoire pour
# calculer une moyenne fiable.
REAL_DEAL_THRESHOLD = float(os.environ.get("REAL_DEAL_THRESHOLD", "0.7"))
REAL_DEAL_MIN_SAMPLES = int(os.environ.get("REAL_DEAL_MIN_SAMPLES", "5"))

# --- Parrainage ---
REFERRAL_BONUS_DAYS = int(os.environ.get("REFERRAL_BONUS_DAYS", "3"))

# --- Wishlist ---
# Intervalle (en cycles de veille) entre deux vérifications de prix de la
# wishlist, pour ne pas surcharger Vinted avec une requête par item à chaque
# cycle de 3 min. Avec la valeur par défaut (1), la wishlist est revérifiée
# à chaque cycle, comme les recherches classiques.
WISHLIST_CHECK_EVERY_N_CYCLES = int(os.environ.get("WISHLIST_CHECK_EVERY_N_CYCLES", "1"))

# --- Carrousel d'alertes (navigation ◀ ▶ entre les articles) ---
# Les articles d'un carrousel sont gardés en base pour que les boutons
# fonctionnent ; passé ce délai, un vieux carrousel affiche "expiré".
CAROUSEL_RETENTION_DAYS = int(os.environ.get("CAROUSEL_RETENTION_DAYS", "3"))
