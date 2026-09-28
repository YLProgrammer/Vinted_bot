"""
Accès partagé à la base SQLite.

Un SEUL schéma est défini ici, utilisé à la fois par bot_centrale.py et
bot_sender.py, pour éviter le bug où les deux bots créaient des tables
"users" avec des colonnes différentes sur le même fichier vinted_bot.db.

Le mode WAL est activé pour que les deux bots (deux process séparés)
puissent lire/écrire en même temps sans erreur "database is locked".

Les nouvelles colonnes/tables (wishlist, veille par vendeur, parrainage,
statistiques de prix, purge) sont ajoutées automatiquement au démarrage si
elles n'existent pas encore : aucune migration manuelle n'est nécessaire,
la base existante continue de fonctionner telle quelle.
"""
import json
import sqlite3
import contextlib
import time

from config import DB_PATH, MONITOR_HOUR_START, MONITOR_HOUR_END, CAROUSEL_RETENTION_DAYS


@contextlib.contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _colonne_existe(conn, table, colonne):
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == colonne for row in cursor.fetchall())


def _ajouter_colonne_si_absente(conn, table, colonne, definition_sql):
    """ALTER TABLE ADD COLUMN idempotent : permet de faire évoluer le schéma
    sans casser les bases existantes des utilisateurs déjà en prod."""
    if not _colonne_existe(conn, table, colonne):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {colonne} {definition_sql}")
        return True
    return False


# Colonnes que le code attend, table par table. Une base créée par une
# ancienne version peut en manquer (CREATE TABLE IF NOT EXISTS ne modifie
# JAMAIS une table déjà existante) : on les ajoute donc toutes ici, de façon
# idempotente. Seuls des défauts constants sont autorisés par SQLite.
COLONNES_ATTENDUES = {
    "users": [
        ("first_name", "TEXT"), ("username", "TEXT"),
        ("is_linked", "INTEGER DEFAULT 0"), ("is_premium", "INTEGER DEFAULT 0"),
        ("token_used", "TEXT"),
        ("ignore_warning_20", "INTEGER DEFAULT 0"),
        ("ignore_warning_50", "INTEGER DEFAULT 0"),
        ("ignore_warning_100", "INTEGER DEFAULT 0"),
        ("consecutive_send_failures", "INTEGER DEFAULT 0"),
        ("premium_expires_at", "TIMESTAMP"),
        ("language", "TEXT DEFAULT 'fr'"),
        ("referred_by", "TEXT"),
        ("referral_credits", "INTEGER DEFAULT 0"),
    ],
    "searches": [
        ("query", "TEXT"), ("min_price", "TEXT"), ("max_price", "TEXT"),
        ("size", "TEXT"), ("size_label", "TEXT"), ("status", "TEXT"),
        ("limit_count", "INTEGER DEFAULT 20"), ("is_paused", "INTEGER DEFAULT 0"),
        ("exclude_keywords", "TEXT"),
    ],
    "seen_items": [("item_url", "TEXT")],
    "item_prices": [("item_url", "TEXT"), ("price_value", "REAL")],
}

# Tables dont les lignes sont datées (created_at) pour la purge automatique.
TABLES_AVEC_DATE = ["searches", "seen_items", "item_prices"]
DATE_SENTINELLE = "2000-01-01 00:00:00"


def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                is_linked INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                token_used TEXT,
                ignore_warning_20 INTEGER DEFAULT 0,
                ignore_warning_50 INTEGER DEFAULT 0,
                ignore_warning_100 INTEGER DEFAULT 0,
                consecutive_send_failures INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Un token ne peut être réclamé que par un seul chat_id (corrige le
        # bug où un même token restait utilisable à l'infini par tout le monde).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS used_tokens (
                token TEXT PRIMARY KEY,
                chat_id TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS premium_tokens (
                token_hash TEXT PRIMARY KEY,
                label TEXT,
                expires_at TIMESTAMP,
                max_uses INTEGER DEFAULT 1,
                use_count INTEGER DEFAULT 0,
                revoked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                query TEXT,
                min_price TEXT,
                max_price TEXT,
                size TEXT,
                size_label TEXT,
                status TEXT,
                limit_count INTEGER DEFAULT 20,
                is_paused INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                item_url TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS item_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                item_url TEXT,
                price_value REAL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                feedback_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Veille par vendeur ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seller_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                seller_id TEXT,
                seller_label TEXT,
                limit_count INTEGER DEFAULT 20,
                is_paused INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Wishlist (suivi d'une annonce précise) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                item_url TEXT,
                titre TEXT,
                prix_initial REAL,
                prix_dernier REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Moyenne de prix par recherche (pour la détection de vraies
        # bonnes affaires, calculée en ligne façon moyenne mobile) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_price_stats (
                search_id INTEGER PRIMARY KEY,
                avg_price REAL DEFAULT 0,
                sample_count INTEGER DEFAULT 0
            )
        """)

        # --- Réglages modifiables à chaud (ex: plage horaire via la commande dev) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # --- Carrousels d'alertes : la liste des articles est gardée ici pour
        # que les boutons ◀ ▶ puissent naviguer (même après un redémarrage) ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS carousels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                items_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Migrations additives sur les tables déjà existantes ---
        for table, colonnes in COLONNES_ATTENDUES.items():
            for colonne, definition in colonnes:
                _ajouter_colonne_si_absente(conn, table, colonne, definition)

        # created_at : SQLite interdit CURRENT_TIMESTAMP comme défaut sur un
        # ALTER TABLE, on met donc une date sentinelle constante, puis on
        # date les lignes HISTORIQUES à "maintenant" : elles vieilliront
        # normalement (purge à 30 jours) au lieu d'être supprimées dès le
        # premier lancement (bug corrigé : une date de 2000 les faisait
        # toutes partir immédiatement). Le code applicatif fixe created_at
        # explicitement à chaque nouvelle insertion.
        for table in TABLES_AVEC_DATE:
            _ajouter_colonne_si_absente(
                conn, table, "created_at", f"TIMESTAMP DEFAULT '{DATE_SENTINELLE}'"
            )
            conn.execute(
                f"UPDATE {table} SET created_at = datetime('now') "
                f"WHERE created_at IS NULL OR created_at = ?",
                (DATE_SENTINELLE,),
            )

        # Indexes essentiels : les scans tournent continuellement et ces
        # requêtes deviennent coûteuses dès que la base grandit.
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_searches_active ON searches(is_paused, chat_id)",
            "CREATE INDEX IF NOT EXISTS idx_seen_items_lookup ON seen_items(chat_id, item_url)",
            "CREATE INDEX IF NOT EXISTS idx_item_prices_lookup ON item_prices(chat_id, item_url)",
            "CREATE INDEX IF NOT EXISTS idx_wishlist_chat ON wishlist(chat_id)",
            "CREATE INDEX IF NOT EXISTS idx_seller_watches_chat ON seller_watches(chat_id)",
            "CREATE INDEX IF NOT EXISTS idx_carousels_chat_created ON carousels(chat_id, created_at)",
        ):
            cursor.execute(statement)


def upsert_user(chat_id, first_name=None, username=None, is_linked=None):
    """Crée l'utilisateur s'il n'existe pas encore, sinon met à jour son nom/pseudo."""
    chat_id = str(chat_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
        exists = cursor.fetchone()
        if exists:
            cursor.execute(
                "UPDATE users SET "
                "first_name = COALESCE(?, first_name), "
                "username = COALESCE(?, username), "
                "is_linked = COALESCE(?, is_linked) "
                "WHERE chat_id = ?",
                (first_name, username, is_linked, chat_id),
            )
        else:
            cursor.execute(
                "INSERT INTO users (chat_id, first_name, username, is_linked) VALUES (?, ?, ?, ?)",
                (chat_id, first_name, username, is_linked if is_linked is not None else 0),
            )


def supprimer_utilisateur_complet(chat_id):
    """Efface toutes les données opérationnelles d'un utilisateur."""
    chat_id = str(chat_id)
    with get_connection() as conn:
        for table in ("searches", "seen_items", "item_prices", "seller_watches", "wishlist", "carousels", "feedbacks"):
            conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))


def get_user(chat_id):
    chat_id = str(chat_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, first_name, username, is_linked, is_premium, "
            "ignore_warning_20, ignore_warning_50, ignore_warning_100, consecutive_send_failures, "
            "premium_expires_at, referred_by, referral_credits, language "
            "FROM users WHERE chat_id = ?",
            (chat_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        keys = [
            "chat_id", "first_name", "username", "is_linked", "is_premium",
            "ignore_warning_20", "ignore_warning_50", "ignore_warning_100",
            "consecutive_send_failures", "premium_expires_at", "referred_by",
            "referral_credits", "language",
        ]
        return dict(zip(keys, row))


# ---------------------------------------------------------------------------
# Premium (activation manuelle / dev, et parrainage à durée limitée)
# ---------------------------------------------------------------------------

def accorder_premium(chat_id, jours=None):
    """Passe un compte en Premium. Si `jours` est fourni, le Premium expire
    après ce nombre de jours (utilisé par le parrainage et la commande dev
    /addpremium quand une durée est précisée) ; sinon il est permanent
    (comme un token classique). Crée l'utilisateur s'il n'existe pas."""
    chat_id = str(chat_id)
    expires_at = None
    with get_connection() as conn:
        cursor = conn.cursor()
        if jours:
            cursor.execute("SELECT premium_expires_at FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            base = time.time()
            if row and row[0]:
                try:
                    base = max(base, time.mktime(time.strptime(row[0], "%Y-%m-%d %H:%M:%S")))
                except (ValueError, TypeError):
                    pass
            expires_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + jours * 86400))

        cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE users SET is_premium = 1, premium_expires_at = ? WHERE chat_id = ?",
                (expires_at, chat_id),
            )
        else:
            cursor.execute(
                "INSERT INTO users (chat_id, is_premium, is_linked, premium_expires_at) VALUES (?, 1, 1, ?)",
                (chat_id, expires_at),
            )
    return expires_at


def retirer_premium(chat_id):
    chat_id = str(chat_id)
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET is_premium = 0, premium_expires_at = NULL WHERE chat_id = ?",
            (chat_id,),
        )


def purger_premium_expire():
    """Repasse en Gratuit les comptes Premium dont la date d'expiration est
    dépassée (parrainage à durée limitée). Appelé périodiquement par le
    moniteur, plutôt que de vérifier l'expiration à chaque lecture."""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_premium = 0 "
            "WHERE is_premium = 1 AND premium_expires_at IS NOT NULL AND premium_expires_at < ?",
            (now_str,),
        )
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Parrainage
# ---------------------------------------------------------------------------

def credit_referral(chat_id, referrer_chat_id, bonus_days):
    """Crédite `bonus_days` de Premium au filleul ET au parrain, une seule
    fois par filleul (protégé par la colonne referred_by). Retourne False si
    ce filleul avait déjà été parrainé, ou s'il essaie de se parrainer lui-même."""
    chat_id, referrer_chat_id = str(chat_id), str(referrer_chat_id)
    if chat_id == referrer_chat_id:
        return False
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT referred_by FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return False  # déjà parrainé par quelqu'un
        cursor.execute(
            "UPDATE users SET referred_by = ? WHERE chat_id = ?",
            (referrer_chat_id, chat_id),
        )
        cursor.execute(
            "UPDATE users SET referral_credits = referral_credits + 1 WHERE chat_id = ?",
            (referrer_chat_id,),
        )
    accorder_premium(chat_id, jours=bonus_days)
    accorder_premium(referrer_chat_id, jours=bonus_days)
    return True


# ---------------------------------------------------------------------------
# Statistiques de prix par recherche (détection de vraies bonnes affaires)
# ---------------------------------------------------------------------------

def maj_stats_prix_recherche(search_id, nouveau_prix):
    """Moyenne mobile simple des prix vus pour cette recherche. Retourne
    (moyenne_avant_maj, nb_echantillons_avant_maj) pour que l'appelant puisse
    comparer le nouveau prix à la moyenne AVANT qu'elle ne soit mise à jour
    par ce même article."""
    if not search_id or nouveau_prix is None or nouveau_prix <= 0:
        return None, 0
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT avg_price, sample_count FROM search_price_stats WHERE search_id = ?", (search_id,))
        row = cursor.fetchone()
        if row:
            avg_avant, count_avant = row
        else:
            avg_avant, count_avant = 0.0, 0

        nouveau_count = count_avant + 1
        nouvelle_moyenne = ((avg_avant * count_avant) + nouveau_prix) / nouveau_count

        cursor.execute(
            "INSERT INTO search_price_stats (search_id, avg_price, sample_count) VALUES (?, ?, ?) "
            "ON CONFLICT(search_id) DO UPDATE SET avg_price = excluded.avg_price, sample_count = excluded.sample_count",
            (search_id, nouvelle_moyenne, nouveau_count),
        )
    return avg_avant, count_avant


# ---------------------------------------------------------------------------
# Veille par vendeur
# ---------------------------------------------------------------------------

def ajouter_veille_vendeur(chat_id, seller_id, seller_label, limit_count=20):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO seller_watches (chat_id, seller_id, seller_label, limit_count, is_paused) "
            "VALUES (?, ?, ?, ?, 0)",
            (str(chat_id), str(seller_id), seller_label, limit_count),
        )


def lister_veilles_vendeur(chat_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, seller_id, seller_label, limit_count FROM seller_watches WHERE chat_id = ?",
            (str(chat_id),),
        )
        return cursor.fetchall()


def supprimer_veille_vendeur(watch_id, chat_id):
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM seller_watches WHERE id = ? AND chat_id = ?", (watch_id, str(chat_id))
        )


def toutes_les_veilles_vendeur_actives():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, chat_id, seller_id, seller_label, limit_count FROM seller_watches WHERE is_paused = 0"
        )
        return cursor.fetchall()


# ---------------------------------------------------------------------------
# Wishlist
# ---------------------------------------------------------------------------

def ajouter_wishlist(chat_id, item_url, titre, prix_initial):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO wishlist (chat_id, item_url, titre, prix_initial, prix_dernier) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(chat_id), item_url, titre, prix_initial, prix_initial),
        )


def lister_wishlist(chat_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, item_url, titre, prix_initial, prix_dernier FROM wishlist WHERE chat_id = ?",
            (str(chat_id),),
        )
        return cursor.fetchall()


def supprimer_wishlist(item_id, chat_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM wishlist WHERE id = ? AND chat_id = ?", (item_id, str(chat_id)))


def toute_la_wishlist():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, chat_id, item_url, titre, prix_dernier FROM wishlist")
        return cursor.fetchall()


def maj_prix_wishlist(item_id, nouveau_prix):
    with get_connection() as conn:
        conn.execute("UPDATE wishlist SET prix_dernier = ? WHERE id = ?", (nouveau_prix, item_id))



# ---------------------------------------------------------------------------
# Carrousels d'alertes
# ---------------------------------------------------------------------------

def creer_carousel(chat_id, items):
    """Enregistre la liste d'articles d'un carrousel et retourne son id."""
    champs = ("titre", "prix", "lien", "image", "bonne_affaire")
    donnees = [{k: it.get(k) for k in champs} for it in items]
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO carousels (chat_id, items_json, created_at) VALUES (?, ?, datetime('now'))",
            (str(chat_id), json.dumps(donnees, ensure_ascii=False)),
        )
        return cursor.lastrowid


def get_carousel(carousel_id, chat_id):
    """Retourne la liste d'articles du carrousel, ou None s'il n'existe plus
    (expiré/purgé) ou n'appartient pas à ce chat."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT items_json FROM carousels WHERE id = ? AND chat_id = ?",
            (carousel_id, str(chat_id)),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Purge automatique
# ---------------------------------------------------------------------------

def purger_anciennes_donnees(retention_days):
    """Supprime les entrées seen_items / item_prices plus vieilles que
    `retention_days` jours, pour empêcher la DB de grossir indéfiniment.
    Retourne le nombre total de lignes supprimées."""
    with get_connection() as conn:
        cursor = conn.cursor()
        seuil = f"-{int(retention_days)} days"
        cursor.execute(f"DELETE FROM seen_items WHERE created_at < datetime('now', ?)", (seuil,))
        n1 = cursor.rowcount
        cursor.execute(f"DELETE FROM item_prices WHERE created_at < datetime('now', ?)", (seuil,))
        n2 = cursor.rowcount
        cursor.execute(
            "DELETE FROM carousels WHERE created_at < datetime('now', ?)",
            (f"-{int(CAROUSEL_RETENTION_DAYS)} days",),
        )
        n3 = cursor.rowcount
        return (n1 or 0) + (n2 or 0) + (n3 or 0)


# ---------------------------------------------------------------------------
# Réglages à chaud + plage horaire de veille
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_plage_horaire():
    """(début, fin) de la plage de veille. Valeur définie via la commande dev
    /plage si elle existe, sinon les valeurs par défaut du .env / config."""
    try:
        debut = int(get_setting("monitor_hour_start", MONITOR_HOUR_START))
        fin = int(get_setting("monitor_hour_end", MONITOR_HOUR_END))
    except (TypeError, ValueError):
        debut, fin = MONITOR_HOUR_START, MONITOR_HOUR_END
    return debut, fin


def set_plage_horaire(debut, fin):
    set_setting("monitor_hour_start", debut)
    set_setting("monitor_hour_end", fin)


def heure_dans_plage(heure, debut, fin):
    """(0, 24) = toujours actif. debut < fin : plage normale (ex: 8 -> 23).
    debut > fin : plage à cheval sur minuit (ex: 22 -> 6). La borne de fin
    est exclue (8 -> 23 s'arrête à 22h59)."""
    if debut <= 0 and fin >= 24:
        return True
    if debut < fin:
        return debut <= heure < fin
    return heure >= debut or heure < fin
