"""Gestion sécurisée des tokens Premium.

Les tokens ne sont jamais stockés en clair : seule leur empreinte SHA-256
est conservée. Cela évite qu'une sauvegarde de base ne donne accès aux plans
Premium de tous les utilisateurs.
"""
import hashlib
import secrets

from db import get_connection


def _hash(token):
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def creer_token(jours=None, label=None, max_uses=1):
    """Crée un token affiché une seule fois à l'administrateur."""
    token = "VP-" + secrets.token_urlsafe(18).upper()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO premium_tokens (token_hash, label, expires_at, max_uses) "
            "VALUES (?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END, ?)",
            (_hash(token), label, jours, f"+{int(jours)} days" if jours else None, max(1, int(max_uses))),
        )
    return token


def activer_token(chat_id, token, first_name=None, username=None):
    chat_id, token_hash = str(chat_id), _hash(token)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT max_uses, use_count FROM premium_tokens WHERE token_hash = ? "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at >= datetime('now'))",
            (token_hash,),
        )
        row = cursor.fetchone()
        if not row:
            return "invalid"
        cursor.execute("SELECT chat_id FROM used_tokens WHERE token = ?", (token_hash,))
        used = cursor.fetchone()
        if used and str(used[0]) != chat_id:
            return "already_used"
        if not used and row[1] >= row[0]:
            return "already_used"
        cursor.execute("INSERT OR IGNORE INTO used_tokens (token, chat_id) VALUES (?, ?)", (token_hash, chat_id))
        if not used:
            cursor.execute("UPDATE premium_tokens SET use_count = use_count + 1 WHERE token_hash = ?", (token_hash,))
        cursor.execute(
            "INSERT INTO users (chat_id, is_premium, token_used, is_linked, first_name, username) VALUES (?, 1, ?, 1, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET is_premium = 1, token_used = excluded.token_used, is_linked = 1, "
            "first_name = COALESCE(excluded.first_name, users.first_name), username = COALESCE(excluded.username, users.username)",
            (chat_id, token_hash, first_name, username),
        )
    return "ok"


def revoquer_token(token):
    with get_connection() as conn:
        return conn.execute("UPDATE premium_tokens SET revoked_at = datetime('now') WHERE token_hash = ?", (_hash(token),)).rowcount > 0
