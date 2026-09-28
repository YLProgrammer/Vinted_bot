"""
Gestion des tokens Premium.

Correctif par rapport à l'ancienne version : un token n'est désormais
consommable qu'une seule fois. Avant, /activate vérifiait juste que le
token figurait dans tokens.txt, sans jamais le marquer comme "pris" :
n'importe qui connaissant un token valide pouvait donc l'utiliser, même
après qu'un autre utilisateur l'ait déjà activé.
"""
from config import TOKENS_FILE
from db import get_connection


def charger_tokens_valides():
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        tokens_defaut = ["123456", "654321", "999888"]
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(tokens_defaut))
        return tokens_defaut


def activer_token(chat_id, token, first_name=None, username=None):
    """
    Tente d'activer un token Premium pour ce chat_id.
    Retourne "ok", "invalid" (token inconnu) ou "already_used" (déjà pris par
    un autre compte).
    """
    chat_id = str(chat_id)
    tokens_valides = charger_tokens_valides()
    if token not in tokens_valides:
        return "invalid"

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM used_tokens WHERE token = ?", (token,))
        row = cursor.fetchone()
        if row and str(row[0]) != chat_id:
            return "already_used"

        cursor.execute(
            "INSERT OR IGNORE INTO used_tokens (token, chat_id) VALUES (?, ?)",
            (token, chat_id),
        )
        cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
        exists = cursor.fetchone()
        if exists:
            cursor.execute(
                "UPDATE users SET is_premium = 1, token_used = ?, is_linked = 1, "
                "first_name = COALESCE(?, first_name), username = COALESCE(?, username) "
                "WHERE chat_id = ?",
                (token, first_name, username, chat_id),
            )
        else:
            cursor.execute(
                "INSERT INTO users (chat_id, is_premium, token_used, is_linked, first_name, username) "
                "VALUES (?, 1, ?, 1, ?, ?)",
                (chat_id, token, first_name, username),
            )
    return "ok"
