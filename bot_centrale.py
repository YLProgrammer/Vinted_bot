import time
import threading

from fastapi import FastAPI

from config import URL_TELEGRAM_CENTRAL, URL_TELEGRAM_DEV, DEV_CHAT_ID
from db import (
    init_db, get_connection, upsert_user, accorder_premium, retirer_premium,
    get_plage_horaire, set_plage_horaire, heure_dans_plage,
)
from telegram_utils import envoyer_message, envoyer_requete
from token_manager import activer_token
from logger_config import get_logger

logger = get_logger("bot_centrale")

app = FastAPI()
hub_states = {}


def _lang(chat_id):
    with get_connection() as conn:
        row = conn.execute("SELECT language FROM users WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return row[0] if row and row[0] in ("fr", "en") else "fr"


def _hub_keyboard(rows):
    return {"inline_keyboard": rows}


def _hub_step4(chat_id):
    en = _lang(chat_id) == "en"
    text = (
        "👑 *Premium access*\n\nYou can use VintedPulse for free with *one active search*. "
        "A Premium token unlocks multiple simultaneous searches.\n\nDo you have a Premium token? "
        "You can enter it now or continue with the free plan.\n\n"
        "👉 [Open VintedPulse Bot](https://t.me/VintedPulseBot) and send `/start` to create alerts."
        if en else
        "👑 *Accès Premium*\n\nTu peux utiliser VintedPulse gratuitement avec *une recherche active*. "
        "Un token Premium débloque plusieurs recherches simultanées.\n\nAs-tu un token Premium ? "
        "Tu peux le saisir maintenant ou continuer avec le plan gratuit.\n\n"
        "👉 [Ouvrir VintedPulse Bot](https://t.me/VintedPulseBot) puis tape `/start` pour créer tes alertes."
    )
    buttons = [[{"text": "🔑 Enter my token" if en else "🔑 Entrer mon token", "callback_data": "hub_token"}],
               [{"text": "🚀 Continue free" if en else "🚀 Continuer gratuitement", "url": "https://t.me/VintedPulseBot"}]]
    envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, text, reply_markup=_hub_keyboard(buttons))


def gerer_callback_hub(callback):
    chat_id = str(callback["message"]["chat"]["id"])
    data = callback["data"]
    envoyer_requete(URL_TELEGRAM_CENTRAL, "answerCallbackQuery", {"callback_query_id": callback["id"]})
    if data.startswith("hub_lang_"):
        language = data.rsplit("_", 1)[1]
        if language not in ("fr", "en"):
            return
        with get_connection() as conn:
            conn.execute("UPDATE users SET language = ? WHERE chat_id = ?", (language, chat_id))
        en = language == "en"
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id,
            "Do you already know how the bot works?" if en else "Tu connais déjà le principe du bot ?",
            reply_markup=_hub_keyboard([[{"text": "Yes" if en else "Oui", "callback_data": "hub_known"},
                                         {"text": "No" if en else "Non", "callback_data": "hub_unknown"}]]))
    elif data == "hub_known":
        _hub_step4(chat_id)
    elif data == "hub_unknown":
        en = _lang(chat_id) == "en"
        text = ("VintedPulse monitors Vinted for you and sends an alert as soon as a matching item appears. "
                "Create a search, choose your filters, then let the bot watch in the background.\n\n"
                "You can also pause alerts, follow sellers and track an item's price."
                if en else
                "VintedPulse surveille Vinted pour toi et t'envoie une alerte dès qu'un article correspondant apparaît. "
                "Crée une recherche, choisis tes filtres, puis laisse le bot surveiller en arrière-plan.\n\n"
                "Tu peux aussi mettre tes alertes en pause, suivre des vendeurs et suivre le prix d'une annonce.")
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, text, reply_markup=_hub_keyboard(
            [[{"text": "I understand" if en else "J'ai tout compris", "callback_data": "hub_understood"}],
             [{"text": "Explain in detail" if en else "M'expliquer en détail", "callback_data": "hub_detailed"}]]))
    elif data == "hub_detailed":
        en = _lang(chat_id) == "en"
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id,
            ("*How to use it in detail*\n\n1. Open the Sender bot.\n2. Start a new search and enter an item name.\n3. Set prices, excluded words, sizes and condition.\n4. Confirm: VintedPulse checks Vinted regularly and sends only new matching listings.\n\nThe free plan includes one active search; Premium allows several." if en else
             "*Comment l'utiliser en détail*\n\n1. Ouvre le bot Sender.\n2. Lance une nouvelle recherche et indique le nom de l'article.\n3. Renseigne les prix, mots exclus, tailles et états.\n4. Confirme : VintedPulse vérifie Vinted régulièrement et t'envoie uniquement les nouvelles annonces correspondantes.\n\nLe plan gratuit comprend une recherche active ; Premium en permet plusieurs."))
        _hub_step4(chat_id)
    elif data == "hub_understood":
        _hub_step4(chat_id)
    elif data == "hub_token":
        hub_states[chat_id] = "waiting_token"
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, "Send your Premium token." if _lang(chat_id) == "en" else "Envoie ton token Premium.")


def definir_commandes_central():
    commandes = [
        {"command": "start", "description": "Accueil & accès au bot d'alertes"},
        {"command": "activate", "description": "Activer un token Premium (/activate TOKEN)"},
        {"command": "status", "description": "Vérifier l'état de son abonnement"},
    ]
    envoyer_requete(URL_TELEGRAM_CENTRAL, "setMyCommands", {"commands": commandes})


def definir_commandes_dev():
    commandes = [
        {"command": "users", "description": "Voir la liste des comptes Telegram enregistrés"},
        {"command": "searches", "description": "Voir toutes les recherches en cours avec les pseudos"},
        {"command": "ban", "description": "Bannir un utilisateur (/ban chat_id)"},
        {"command": "stopsearch", "description": "Arrêter une recherche spécifique (/stopsearch id)"},
        {"command": "broadcast", "description": "Envoyer un message à tous (/broadcast texte)"},
        {"command": "addpremium", "description": "Offrir le Premium (/addpremium chat_id [jours])"},
        {"command": "removepremium", "description": "Retirer le Premium (/removepremium chat_id)"},
        {"command": "plage", "description": "Voir/définir la plage horaire de veille (/plage 8 23)"},
    ]
    envoyer_requete(URL_TELEGRAM_DEV, "setMyCommands", {"commands": commandes})


# --- GESTION DU BOT CENTRAL (CLIENTS) ---
def gerer_commandes_central(message_obj):
    chat_id = str(message_obj["chat"]["id"])
    text = message_obj.get("text", "").strip()

    user_info = message_obj.get("from", {})
    first_name = user_info.get("first_name", "Inconnu")
    username = user_info.get("username")

    if text == "/start":
        upsert_user(chat_id, first_name=first_name, username=username, is_linked=1)
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, "🌐 Choose your language:" if _lang(chat_id) == "en" else "🌐 Choisis ta langue :",
            reply_markup=_hub_keyboard([[{"text": "🇬🇧 EN", "callback_data": "hub_lang_en"}, {"text": "🇫🇷 FR", "callback_data": "hub_lang_fr"}]]))

    elif hub_states.get(chat_id) == "waiting_token":
        hub_states.pop(chat_id, None)
        resultat = activer_token(chat_id, text.strip(), first_name, username)
        en = _lang(chat_id) == "en"
        messages = {"ok": "🎉 Token accepted! Your account is now *Premium*." if en else "🎉 *Token accepté !* Ton compte est désormais **Premium**.",
                    "already_used": "❌ This token has already been used." if en else "❌ Ce token a déjà été activé par un autre compte.",
                    "invalid": "❌ Invalid token." if en else "❌ Token invalide."}
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, messages.get(resultat, messages["invalid"]))

    elif text.startswith("/activate"):
        parts = text.split(" ")
        if len(parts) < 2:
            envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, "⚠️ Utilisation : `/activate VOTRE_TOKEN`")
            return
        token_saisi = parts[1].strip()
        resultat = activer_token(chat_id, token_saisi, first_name, username)
        if resultat == "ok":
            envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, "🎉 *Token accepté !* Votre compte est désormais passé en mode **Premium**.")
        elif resultat == "already_used":
            envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, "❌ Ce token a déjà été activé par un autre compte.")
        else:
            envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, "❌ Token invalide.")

    elif text == "/status":
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_premium, premium_expires_at FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
        is_p = row[0] if row else 0
        expires_at = row[1] if row else None
        if is_p and expires_at:
            statut_txt = f"👑 Premium (jusqu'au {expires_at})"
        elif is_p:
            statut_txt = "👑 Premium (Illimité)"
        else:
            statut_txt = "🆓 Gratuit (1 recherche max)"
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, f"📊 *État de votre compte*\n\nStatut : `{statut_txt}`")

    else:
        envoyer_message(URL_TELEGRAM_CENTRAL, chat_id, "🤖 Tapez `/start` pour accéder au bot ou `/activate <token>` pour passer Premium.")


def _decrire_plage(debut, fin):
    if debut <= 0 and fin >= 24:
        return "🕒 Veille active *24h/24* (aucune restriction horaire)."
    actif = heure_dans_plage(time.localtime().tm_hour, debut, fin)
    etat = "✅ actuellement *dans* la plage" if actif else "😴 actuellement *hors* plage (veille en pause)"
    return f"🕒 Veille active de *{debut}h à {fin}h* (heure du serveur)\n{etat}"


# --- GESTION DU BOT DEV (ADMINISTRATION) ---
def gerer_commandes_dev(chat_id, text):
    chat_id = str(chat_id)
    if chat_id != str(DEV_CHAT_ID):
        envoyer_message(URL_TELEGRAM_DEV, chat_id, "⛔ Accès refusé. Ce bot est strictement réservé au développeur.")
        return

    if text == "/users":
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, is_premium, is_linked, first_name, username FROM users")
            users = cursor.fetchall()
        if not users:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "📋 Aucun utilisateur enregistré pour le moment.")
        else:
            msg = f"👥 *Liste des utilisateurs ({len(users)}) :*\n\n"
            for c_id, is_p, is_l, fname, uname in users:
                statut = "👑 Premium" if is_p else "🆓 Gratuit"
                pseudo_txt = f"@{uname}" if uname else "*(Pas de pseudo)*"
                msg += f"• **{fname}** ({pseudo_txt})\n  ID: `{c_id}` | {statut}\n\n"
            envoyer_message(URL_TELEGRAM_DEV, chat_id, msg)

    elif text == "/searches":
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.id, s.chat_id, s.query, s.min_price, s.max_price, s.is_paused, u.first_name, u.username
                FROM searches s
                LEFT JOIN users u ON s.chat_id = u.chat_id
            """)
            searches = cursor.fetchall()
        if not searches:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "📋 Aucune recherche active en arrière-plan.")
        else:
            msg = f"🔍 *Recherches en cours ({len(searches)}) :*\n\n"
            for s_id, c_id, q, min_p, max_p, paused, fname, uname in searches:
                user_label = f"{fname} (@{uname})" if uname else (fname or "Inconnu")
                p_status = "⏸️ (Pause)" if paused else "▶️"
                msg += f"• **[ID: {s_id}]** par **{user_label}** (`{c_id}`)\n  Recherche : `{q}` ({min_p or 0}€-{max_p or 'Max'}€) {p_status}\n\n"
            envoyer_message(URL_TELEGRAM_DEV, chat_id, msg)

    elif text.startswith("/ban"):
        parts = text.split(" ")
        if len(parts) < 2:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "⚠️ Utilisation : `/ban <chat_id>`")
        else:
            target_id = parts[1].strip()
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM users WHERE chat_id = ?", (target_id,))
                cursor.execute("DELETE FROM searches WHERE chat_id = ?", (target_id,))
            envoyer_message(URL_TELEGRAM_DEV, chat_id, f"🔨 Utilisateur `{target_id}` banni et ses recherches supprimées avec succès.")

    elif text.startswith("/stopsearch"):
        parts = text.split(" ")
        if len(parts) < 2:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "⚠️ Utilisation : `/stopsearch <search_id>`")
        else:
            search_id = parts[1].strip()
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM searches WHERE id = ?", (search_id,))
            envoyer_message(URL_TELEGRAM_DEV, chat_id, f"🛑 Recherche ID `{search_id}` supprimée de force.")

    elif text.startswith("/addpremium"):
        parts = text.split(" ")
        if len(parts) < 2:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "⚠️ Utilisation : `/addpremium <chat_id> [jours]` (sans jours = Premium illimité)")
        else:
            target_id = parts[1].strip()
            jours = None
            if len(parts) >= 3:
                try:
                    jours = int(parts[2].strip())
                except ValueError:
                    envoyer_message(URL_TELEGRAM_DEV, chat_id, "⚠️ Le nombre de jours doit être un entier.")
                    return
            expires_at = accorder_premium(target_id, jours=jours)
            duree_txt = f"jusqu'au `{expires_at}`" if expires_at else "de façon **illimitée**"
            envoyer_message(URL_TELEGRAM_DEV, chat_id, f"👑 Premium accordé à `{target_id}` {duree_txt}.")
            envoyer_message(
                URL_TELEGRAM_CENTRAL, target_id,
                "🎉 *Bonne nouvelle !* Le créateur vous a offert le statut **Premium**"
                + (f" pour {jours} jour(s)" if jours else " (illimité)") + " !",
            )

    elif text.startswith("/removepremium"):
        parts = text.split(" ")
        if len(parts) < 2:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "⚠️ Utilisation : `/removepremium <chat_id>`")
        else:
            target_id = parts[1].strip()
            retirer_premium(target_id)
            envoyer_message(URL_TELEGRAM_DEV, chat_id, f"🔻 Premium retiré à `{target_id}`.")

    elif text.startswith("/plage"):
        parts = text.split()
        usage = (
            "⚠️ Utilisation :\n• `/plage` : voir la plage actuelle\n"
            "• `/plage 8 23` : veille de 8h à 23h (fin exclue)\n"
            "• `/plage 22 6` : plage à cheval sur minuit\n"
            "• `/plage off` : veille 24h/24"
        )
        if len(parts) == 1:
            debut, fin = get_plage_horaire()
            envoyer_message(URL_TELEGRAM_DEV, chat_id, _decrire_plage(debut, fin))
        elif len(parts) == 2 and parts[1].lower() in ("off", "24h", "aucune"):
            set_plage_horaire(0, 24)
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "✅ " + _decrire_plage(0, 24))
        elif len(parts) == 3:
            try:
                debut, fin = int(parts[1]), int(parts[2])
            except ValueError:
                envoyer_message(URL_TELEGRAM_DEV, chat_id, usage)
                return
            if not (0 <= debut <= 23 and 0 <= fin <= 24) or debut == fin:
                envoyer_message(
                    URL_TELEGRAM_DEV, chat_id,
                    "⚠️ Heures invalides : début entre 0 et 23, fin entre 0 et 24, et différentes l'une de l'autre.\n\n" + usage,
                )
                return
            set_plage_horaire(debut, fin)
            envoyer_message(
                URL_TELEGRAM_DEV, chat_id,
                "✅ " + _decrire_plage(debut, fin) + "\n\nPris en compte au prochain cycle de veille (3 min max), sans redémarrage.",
            )
        else:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, usage)

    elif text.startswith("/broadcast"):
        message_to_send = text.replace("/broadcast", "").strip()
        if not message_to_send:
            envoyer_message(URL_TELEGRAM_DEV, chat_id, "⚠️ Écris un message après `/broadcast`.")
        else:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT chat_id FROM users WHERE is_linked = 1")
                all_users = cursor.fetchall()
            count = 0
            for (u_chat_id,) in all_users:
                if envoyer_message(URL_TELEGRAM_CENTRAL, u_chat_id, f"📢 *Annonce officielle :*\n\n{message_to_send}"):
                    count += 1
            envoyer_message(URL_TELEGRAM_DEV, chat_id, f"✅ Message diffusé avec succès à {count} utilisateurs !")

    else:
        envoyer_message(
            URL_TELEGRAM_DEV, chat_id,
            "🛠️ *Commandes Dev :*\n• `/users`\n• `/searches`\n• `/ban <chat_id>`\n• `/stopsearch <id>`\n"
            "• `/addpremium <chat_id> [jours]`\n• `/removepremium <chat_id>`\n• `/plage [début fin | off]`\n• `/broadcast <texte>`"
        )


# --- BOUCLES D'ÉCOUTE ---
def ecouter_telegram_central():
    offset = 0
    logger.info("🏢 Bot Central (@VintedPulseHub_bot) démarré...")
    while True:
        try:
            response = requests_get_updates(URL_TELEGRAM_CENTRAL, offset)
            data = response.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        gerer_callback_hub(update["callback_query"])
                    elif "message" in update:
                        gerer_commandes_central(update["message"])
        except Exception as e:
            logger.error(f"Erreur bot central : {e}")
            time.sleep(5)


def ecouter_telegram_dev():
    offset = 0
    logger.info("🛡️ Bot Dev Administrateur démarré...")
    while True:
        try:
            response = requests_get_updates(URL_TELEGRAM_DEV, offset)
            data = response.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "message" in update and "text" in update["message"]:
                        chat_id = update["message"]["chat"]["id"]
                        text = update["message"]["text"].strip()
                        gerer_commandes_dev(chat_id, text)
        except Exception as e:
            logger.error(f"Erreur bot dev : {e}")
            time.sleep(5)


def requests_get_updates(url_telegram, offset):
    import requests
    return requests.get(url_telegram + f"getUpdates?offset={offset}&timeout=30", timeout=35)


if __name__ == "__main__":
    init_db()
    definir_commandes_central()
    definir_commandes_dev()

    @app.get("/")
    def root():
        return {"status": "Central & Dev Bots Running"}

    threading.Thread(target=ecouter_telegram_central, daemon=True).start()
    threading.Thread(target=ecouter_telegram_dev, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
