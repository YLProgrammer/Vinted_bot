"""
Fonctions d'envoi Telegram partagées par les deux bots.

Corrige plusieurs problèmes de l'ancienne version :
- aucun message n'était vérifié : un échec (utilisateur ayant bloqué le bot,
  rate limit 429, message > 4096 caractères) passait totalement inaperçu.
- ici, chaque fonction renvoie True/False et journalise l'échec.
- les messages trop longs sont automatiquement découpés en plusieurs envois.
"""
import time
import requests

from logger_config import get_logger

logger = get_logger("telegram_utils")

TELEGRAM_MAX_LEN = 4000  # marge de sécurité sous la limite officielle de 4096


def _decouper_message(text):
    if len(text) <= TELEGRAM_MAX_LEN:
        return [text]
    morceaux = []
    reste = text
    while reste:
        if len(reste) <= TELEGRAM_MAX_LEN:
            morceaux.append(reste)
            break
        coupe = reste.rfind("\n", 0, TELEGRAM_MAX_LEN)
        if coupe == -1:
            coupe = TELEGRAM_MAX_LEN
        morceaux.append(reste[:coupe])
        reste = reste[coupe:]
    return morceaux


def envoyer_requete(url_telegram, methode, payload, max_retries=2):
    """POST générique vers l'API Telegram, avec retry automatique sur rate-limit (429)."""
    for _ in range(max_retries + 1):
        try:
            resp = requests.post(url_telegram + methode, json=payload, timeout=15)
            if resp.status_code == 429:
                retry_after = 3
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
                except Exception:
                    pass
                time.sleep(retry_after)
                continue
            if resp.status_code != 200:
                logger.warning(f"{methode} a échoué ({resp.status_code}) : {resp.text[:200]}")
            return resp
        except requests.exceptions.RequestException as e:
            logger.error(f"Erreur réseau sur {methode} : {e}")
            time.sleep(1)
    return None


def envoyer_message(url_telegram, chat_id, text, reply_markup=None, parse_mode="Markdown", sans_apercu_liens=False):
    """Retourne True si (tous les morceaux du) message sont bien partis, False sinon."""
    succes = True
    for morceau in _decouper_message(text):
        payload = {"chat_id": chat_id, "text": morceau, "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if sans_apercu_liens:
            payload["link_preview_options"] = {"is_disabled": True}
        resp = envoyer_requete(url_telegram, "sendMessage", payload)
        if resp is None or resp.status_code != 200:
            succes = False
    return succes


def envoyer_photo(url_telegram, chat_id, photo_url, caption, parse_mode="Markdown", reply_markup=None):
    payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = envoyer_requete(url_telegram, "sendPhoto", payload)
    return resp is not None and resp.status_code == 200


def editer_photo(url_telegram, chat_id, message_id, photo_url, caption, reply_markup=None, parse_mode="Markdown"):
    """Remplace la photo + la légende + les boutons d'un message déjà envoyé
    (c'est ce qui fait tourner le carrousel). Retourne True si c'est bon.
    Un 'message is not modified' (double-clic sur la même flèche) est un
    non-événement, pas une erreur."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "media": {"type": "photo", "media": photo_url, "caption": caption, "parse_mode": parse_mode},
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = envoyer_requete(url_telegram, "editMessageMedia", payload)
    if resp is None:
        return False
    if resp.status_code == 200:
        return True
    return "message is not modified" in (resp.text or "")
