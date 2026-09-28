import re
import time
import random
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI

from config import (
    URL_TELEGRAM_SENDER, URL_TELEGRAM_DEV, DEV_CHAT_ID, SENDER_BOT_USERNAME,
    FREE_PLAN_MAX_SEARCHES, MONITOR_CYCLE_SECONDS, DELAY_BETWEEN_SEARCHES,
    MAX_SEND_FAILURES_BEFORE_AUTOPAUSE, MONITOR_MAX_WORKERS,
    HEALTHCHECK_URL, DATA_RETENTION_DAYS, PURGE_INTERVAL_SECONDS,
    REAL_DEAL_THRESHOLD, REAL_DEAL_MIN_SAMPLES, REFERRAL_BONUS_DAYS,
)
from db import (
    init_db, get_connection, upsert_user, get_user,
    credit_referral, purger_premium_expire, maj_stats_prix_recherche,
    ajouter_veille_vendeur, lister_veilles_vendeur, supprimer_veille_vendeur,
    toutes_les_veilles_vendeur_actives,
    ajouter_wishlist, lister_wishlist, supprimer_wishlist, toute_la_wishlist, maj_prix_wishlist,
    purger_anciennes_donnees, get_plage_horaire, heure_dans_plage,
    creer_carousel, get_carousel,
)
from telegram_utils import (
    envoyer_message as _envoyer_message_api,
    envoyer_photo as _envoyer_photo_api,
    editer_photo as _editer_photo_api,
    envoyer_requete,
)
from vinted_scraper import chercher_vinted, chercher_items_vendeur, resoudre_vendeur, obtenir_prix_item
from logger_config import get_logger

logger = get_logger("bot_sender")

app = FastAPI()
user_states = {}


# Wrappers liés au token du bot sender, pour garder le reste du fichier simple
# (tous les appels ci-dessous gardent la même signature que dans l'ancienne version).
def envoyer_message(chat_id, text, reply_markup=None, parse_mode="Markdown", sans_apercu_liens=False):
    return _envoyer_message_api(
        URL_TELEGRAM_SENDER, chat_id, text, reply_markup=reply_markup,
        parse_mode=parse_mode, sans_apercu_liens=sans_apercu_liens,
    )


def envoyer_photo(chat_id, photo_url, caption, parse_mode="Markdown", reply_markup=None):
    return _envoyer_photo_api(
        URL_TELEGRAM_SENDER, chat_id, photo_url, caption, parse_mode=parse_mode, reply_markup=reply_markup,
    )


def editer_photo(chat_id, message_id, photo_url, caption, reply_markup=None, parse_mode="Markdown"):
    return _editer_photo_api(
        URL_TELEGRAM_SENDER, chat_id, message_id, photo_url, caption,
        reply_markup=reply_markup, parse_mode=parse_mode,
    )


# --- Clavier persistant (visible en permanence sous la zone de texte) ---
def clavier_principal():
    return {
        "keyboard": [
            ["🔍 Nouvelle recherche", "📋 Mes recherches"],
            ["⏸️ Pause", "▶️ Reprendre"],
            ["❤️ Wishlist", "🕵️ Vendeurs suivis"],
            ["📊 Stats", "🎁 Parrainage"],
            ["💡 Feedback", "🛠️ Aide"],
        ],
        "resize_keyboard": True,
    }


REPLY_KEYBOARD_MAP = {
    "🔍 Nouvelle recherche": "/newsearch",
    "📋 Mes recherches": "/list",
    "⏸️ Pause": "/pause",
    "▶️ Reprendre": "/resume",
    "❤️ Wishlist": "/wishlist",
    "🕵️ Vendeurs suivis": "/sellers",
    "📊 Stats": "/stats",
    "🎁 Parrainage": "/parrain",
    "💡 Feedback": "/feedback",
    "🛠️ Aide": "/help",
}


def definir_commandes_sender():
    commandes = [
        {"command": "help", "description": "Afficher l'aide complète"},
        {"command": "start", "description": "Démarrer et activer son compte VintedPulse"},
        {"command": "newsearch", "description": "Créer une nouvelle alerte Vinted pas à pas"},
        {"command": "list", "description": "Afficher vos recherches actives"},
        {"command": "delete", "description": "Supprimer une recherche spécifique"},
        {"command": "stop", "description": "🛑 Stopper et supprimer toutes les recherches"},
        {"command": "cancel", "description": "Annuler la création de recherche en cours"},
        {"command": "disconnect", "description": "Dissocier ton compte"},
        {"command": "feedback", "description": "Suggérer une idée ou signaler un bug"},
        {"command": "pause", "description": "Mettre en pause la veille des recherches"},
        {"command": "resume", "description": "Reprendre la veille des recherches"},
        {"command": "stats", "description": "Voir vos statistiques d'utilisation"},
        {"command": "watch", "description": "Ajouter une annonce à la wishlist (/watch lien)"},
        {"command": "wishlist", "description": "Voir/gérer votre wishlist"},
        {"command": "trackseller", "description": "Suivre un vendeur (/trackseller pseudo_ou_lien)"},
        {"command": "sellers", "description": "Voir/gérer les vendeurs suivis"},
        {"command": "parrain", "description": "Obtenir votre lien de parrainage"},
    ]
    envoyer_requete(URL_TELEGRAM_SENDER, "setMyCommands", {"commands": commandes})


@app.get("/")
def root():
    return {"status": "running", "mode": "VintedPulse Sender Bot SaaS"}


def _parse_prix(prix_str):
    try:
        return float(str(prix_str).replace("€", "").replace(" ", "").replace(",", "."))
    except (ValueError, AttributeError):
        return 0.0


def _filtrer_mots_exclus(items, exclude_keywords):
    if not exclude_keywords or not items:
        return items
    mots = [m.strip().lower() for m in exclude_keywords.split(",") if m.strip()]
    if not mots:
        return items
    return [it for it in items if not any(m in it["titre"].lower() for m in mots)]


def filtrer_doublons_et_baisses(chat_id, items):
    if not items:
        return [], []
    nouveaux, baisses = [], []
    with get_connection() as conn:
        cursor = conn.cursor()
        for item in items:
            prix_clean = _parse_prix(item["prix"])

            cursor.execute("SELECT id FROM seen_items WHERE chat_id = ? AND item_url = ?", (chat_id, item["lien"]))
            deja_vu = cursor.fetchone()
            cursor.execute("SELECT price_value FROM item_prices WHERE chat_id = ? AND item_url = ?", (chat_id, item["lien"]))
            row_price = cursor.fetchone()

            if not deja_vu:
                cursor.execute(
                    "INSERT INTO seen_items (chat_id, item_url, created_at) VALUES (?, ?, datetime('now'))",
                    (chat_id, item["lien"]),
                )
                if not row_price:
                    cursor.execute(
                        "INSERT INTO item_prices (chat_id, item_url, price_value, created_at) VALUES (?, ?, ?, datetime('now'))",
                        (chat_id, item["lien"], prix_clean),
                    )
                nouveaux.append(item)
            elif row_price:
                old_price = row_price[0]
                if prix_clean > 0 and old_price > 0 and prix_clean < old_price:
                    baisses.append({
                        "titre": item["titre"], "ancien_prix": old_price,
                        "nouveau_prix": item["prix"], "lien": item["lien"], "image": item["image"],
                    })
                    cursor.execute(
                        "UPDATE item_prices SET price_value = ? WHERE chat_id = ? AND item_url = ?",
                        (prix_clean, chat_id, item["lien"]),
                    )
    return nouveaux, baisses


def _signaler_echec_envoi(chat_id):
    """Compte les échecs Telegram consécutifs ; au-delà du seuil, met les recherches en pause
    automatiquement (utilisateur qui a probablement bloqué le bot) au lieu de continuer à
    scraper Vinted pour rien indéfiniment."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET consecutive_send_failures = consecutive_send_failures + 1 WHERE chat_id = ?", (chat_id,))
        cursor.execute("SELECT consecutive_send_failures FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row[0] >= MAX_SEND_FAILURES_BEFORE_AUTOPAUSE:
            cursor.execute("UPDATE searches SET is_paused = 1 WHERE chat_id = ?", (chat_id,))
            logger.warning(f"Utilisateur {chat_id} injoignable : recherches mises en pause automatiquement.")


def _signaler_succes_envoi(chat_id):
    with get_connection() as conn:
        conn.execute("UPDATE users SET consecutive_send_failures = 0 WHERE chat_id = ?", (chat_id,))


def _apres_envoi(chat_id, ok):
    if ok:
        _signaler_succes_envoi(chat_id)
    else:
        _signaler_echec_envoi(chat_id)


def _echapper_markdown(texte):
    """Échappe les caractères qui cassent le Markdown de Telegram (_ * ` [).
    Sans ça, un titre d'annonce contenant par exemple "Nike_TN" ou "[neuf]"
    fait refuser tout le message par Telegram (erreur "can't parse entities")."""
    return re.sub(r"([_*`\[])", r"\\\1", str(texte))


# --- Carrousel : un seul message, on navigue entre les articles avec ◀ ▶ ---
def _legende_carousel(item, index, total):
    badge = "🔥 *VRAIE BONNE AFFAIRE DÉTECTÉE* 🔥\n" if item.get("bonne_affaire") else ""
    return (
        f"{badge}🚨 *{_echapper_markdown(item['titre'])}*\n"
        f"💰 {_echapper_markdown(item['prix'])}\n\n"
        f"📦 Article {index + 1}/{total}"
    )


def _clavier_carousel(carousel_id, item, index, total):
    """◀ / ▶ tournent en boucle (après le dernier article on revient au premier)."""
    precedent = (index - 1) % total
    suivant = (index + 1) % total
    lignes = [[
        {"text": "◀️", "callback_data": f"car_{carousel_id}_{precedent}"},
        {"text": f"{index + 1}/{total}", "callback_data": "car_noop"},
        {"text": "▶️", "callback_data": f"car_{carousel_id}_{suivant}"},
    ]]
    lien = item.get("lien") or ""
    if lien.startswith("http"):  # Telegram refuse tout le message si l'URL d'un bouton est invalide
        lignes.append([{"text": "👉 Voir l'annonce", "url": lien}])
    return {"inline_keyboard": lignes}


def envoyer_carousel(chat_id, items):
    """Envoie le carrousel (tous les items doivent avoir une image).
    Si la photo du 1er article est refusée par Telegram, on essaie les suivants
    (jusqu'à 3) plutôt que d'abandonner tout le carrousel. Retourne True/False."""
    total = len(items)
    carousel_id = creer_carousel(chat_id, items)
    for index in range(min(3, total)):
        item = items[index]
        ok = envoyer_photo(
            chat_id, item["image"], _legende_carousel(item, index, total),
            reply_markup=_clavier_carousel(carousel_id, item, index, total),
        )
        if ok:
            return True
    return False


def _naviguer_carousel(callback, chat_id, data_callback):
    query_id = callback["id"]
    parts = data_callback.split("_")

    def repondre(texte=None):
        payload = {"callback_query_id": query_id}
        if texte:
            payload.update({"text": texte, "show_alert": True})
        envoyer_requete(URL_TELEGRAM_SENDER, "answerCallbackQuery", payload)

    if len(parts) != 3 or parts[1] == "noop":  # bouton compteur "2/7" : rien à faire
        repondre()
        return
    try:
        carousel_id, cible = int(parts[1]), int(parts[2])
    except ValueError:
        repondre()
        return

    items = get_carousel(carousel_id, chat_id)
    if not items:
        repondre("⌛ Ce carrousel a expiré. Les prochaines alertes en auront un nouveau !")
        return
    repondre()

    message_id = callback["message"]["message_id"]
    total = len(items)
    # Si la photo cible est refusée par Telegram, on passe à la suivante (max 3 essais).
    for decalage in range(min(3, total)):
        index = (cible + decalage) % total
        item = items[index]
        if not item.get("image"):
            continue
        if editer_photo(
            chat_id, message_id, item["image"],
            _legende_carousel(item, index, total),
            reply_markup=_clavier_carousel(carousel_id, item, index, total),
        ):
            return
    logger.warning(f"Carrousel {carousel_id} : impossible d'afficher l'article {cible} pour {chat_id}")


def _envoyer_liste_texte(chat_id, items, entete=None):
    """Liste texte (repli quand il n'y a pas de photo ou que le carrousel échoue)."""
    lignes = [entete or f"🚨 *{len(items)} nouveaux articles trouvés !*"]
    for item in items:
        badge = "🔥 " if item.get("bonne_affaire") else ""
        lignes.append(
            f"\n{badge}*{_echapper_markdown(item['titre'])}*\n"
            f"💰 {item['prix']} — [👉 Voir l'annonce]({item['lien']})"
        )
    # Sans aperçu de lien : Telegram n'en affichait qu'un seul (celui du 1er
    # lien), ce qui donnait l'impression qu'un seul article avait une photo.
    return envoyer_message(chat_id, "\n".join(lignes), sans_apercu_liens=True)


def envoyer_alerte_telegram(chat_id, items):
    """Envoie les nouveaux articles trouvés.
    - 1 article  -> message riche avec sa photo.
    - 2+ articles avec photo -> UN carrousel (◀ ▶) : un seul message, pas de spam.
    - articles sans photo, ou carrousel refusé par Telegram -> liste texte (repli),
      pour ne jamais perdre une alerte."""
    if not items:
        return

    if len(items) == 1:
        item = items[0]
        badge = "🔥 *VRAIE BONNE AFFAIRE DÉTECTÉE* 🔥\n" if item.get("bonne_affaire") else ""
        if item.get("image"):
            caption = (
                f"{badge}🚨 *{_echapper_markdown(item['titre'])}*\n"
                f"💰 Prix : {item['prix']}\n[👉 Voir l'annonce]({item['lien']})"
            )
            ok = envoyer_photo(chat_id, item["image"], caption)
        else:
            message = f"{badge}🚨 *{_echapper_markdown(item['titre'])}* - 💰 {item['prix']}\n[👉 Voir l'annonce]({item['lien']})"
            ok = envoyer_message(chat_id, message)
        _apres_envoi(chat_id, ok)
        return

    avec_image = [i for i in items if i.get("image")]
    sans_image = [i for i in items if not i.get("image")]

    resultats = []
    a_lister, entete = sans_image, None
    if len(avec_image) >= 2:
        if envoyer_carousel(chat_id, avec_image):
            resultats.append(True)
            if sans_image:
                entete = f"📷 *{len(sans_image)} article(s) sans photo :*"
        else:
            a_lister = items  # repli : le carrousel a échoué, on liste tout en texte
    elif avec_image:
        a_lister = items      # une seule photo : inutile d'en faire un carrousel

    if a_lister:
        resultats.append(_envoyer_liste_texte(chat_id, a_lister, entete))
    _apres_envoi(chat_id, all(resultats))


def _traiter_une_recherche(row):
    search_id, chat_id, query, min_price, max_price, size, status, limit_count, exclude_keywords = row
    time.sleep(random.uniform(0, DELAY_BETWEEN_SEARCHES))  # évite une rafale de requêtes simultanées vers Vinted

    resultats = chercher_vinted(query, min_price, max_price, size, status, limit_count)
    resultats = _filtrer_mots_exclus(resultats, exclude_keywords)
    nouveaux, baisses = filtrer_doublons_et_baisses(chat_id, resultats)

    # Détection de "vraie" bonne affaire : compare le prix de chaque nouvel
    # article à la moyenne mobile des prix vus pour CETTE recherche (et pas
    # juste "moins cher qu'avant" comme la détection de baisse ci-dessous).
    for item in nouveaux:
        prix_float = _parse_prix(item["prix"])
        avg_avant, count_avant = (None, 0)
        if prix_float:
            avg_avant, count_avant = maj_stats_prix_recherche(search_id, prix_float)
        item["bonne_affaire"] = bool(
            prix_float and avg_avant and count_avant >= REAL_DEAL_MIN_SAMPLES
            and prix_float < REAL_DEAL_THRESHOLD * avg_avant
        )

    if nouveaux:
        envoyer_alerte_telegram(chat_id, nouveaux)

    for b in baisses:
        msg = (
            f"📉 *BAISSE DE PRIX DÉTECTÉE !*\n\n🚨 **{b['titre']}**\n"
            f"💰 Ancien : {b['ancien_prix']}€ ➡️ **Nouveau : {b['nouveau_prix']}**\n"
            f"[👉 Saisir l'affaire]({b['lien']})"
        )
        if b.get("image"):
            envoyer_photo(chat_id, b["image"], msg)
        else:
            envoyer_message(chat_id, msg)


def _traiter_une_veille_vendeur(row):
    """⚠️ Repose sur des endpoints Vinted non testés en conditions réelles
    (voir vinted_scraper.py) : à valider avant de compter dessus en prod."""
    watch_id, chat_id, seller_id, seller_label, limit_count = row
    time.sleep(random.uniform(0, DELAY_BETWEEN_SEARCHES))

    resultats = chercher_items_vendeur(seller_id, limit_count)
    nouveaux, _ = filtrer_doublons_et_baisses(chat_id, resultats)
    if nouveaux:
        for item in nouveaux:
            item["titre"] = f"[👤 {seller_label}] {item['titre']}"
        envoyer_alerte_telegram(chat_id, nouveaux)


def _verifier_wishlist():
    """⚠️ Repose sur obtenir_prix_item(), non testé en conditions réelles."""
    for item_id, chat_id, item_url, titre, prix_dernier in toute_la_wishlist():
        try:
            titre_actuel, prix_actuel = obtenir_prix_item(item_url)
        except Exception as e:
            logger.error(f"Erreur vérif wishlist item {item_id} : {e}")
            continue
        if prix_actuel is None:
            continue
        if prix_dernier and prix_actuel < prix_dernier:
            envoyer_message(
                chat_id,
                f"❤️📉 *Baisse de prix sur ta wishlist !*\n\n**{titre or titre_actuel}**\n"
                f"💰 {prix_dernier}€ ➡️ **{prix_actuel}€**\n[👉 Voir l'annonce]({item_url})",
            )
        if prix_actuel != prix_dernier:
            maj_prix_wishlist(item_id, prix_actuel)


def _pinger_heartbeat():
    """Ping périodique vers un service externe (ex: healthchecks.io) : si le
    bot plante ou reste bloqué, une alerte part automatiquement au bout du
    délai configuré côté service, au lieu de le découvrir au hasard."""
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL, timeout=10)
    except Exception as e:
        logger.warning(f"Heartbeat : échec du ping ({e})")


def _dans_la_plage_horaire():
    """La plage est relue à chaque cycle depuis la base : la commande dev
    /plage la modifie à chaud, sans redémarrer le bot."""
    debut, fin = get_plage_horaire()
    return heure_dans_plage(time.localtime().tm_hour, debut, fin)


_last_purge_ts = 0


def background_monitor():
    global _last_purge_ts
    while True:
        try:
            purger_premium_expire()

            if time.time() - _last_purge_ts > PURGE_INTERVAL_SECONDS:
                n = purger_anciennes_donnees(DATA_RETENTION_DAYS)
                if n:
                    logger.info(f"Purge auto : {n} ligne(s) supprimée(s) (> {DATA_RETENTION_DAYS} jours).")
                _last_purge_ts = time.time()

            if _dans_la_plage_horaire():
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, chat_id, query, min_price, max_price, size, status, limit_count, exclude_keywords "
                        "FROM searches WHERE is_paused = 0"
                    )
                    surveillances = cursor.fetchall()

                # Quelques recherches traitées en parallèle : la veille reste réactive
                # même avec plusieurs dizaines d'utilisateurs, sans surcharger Vinted.
                with ThreadPoolExecutor(max_workers=MONITOR_MAX_WORKERS) as executor:
                    list(executor.map(_traiter_une_recherche, surveillances))

                veilles_vendeur = toutes_les_veilles_vendeur_actives()
                if veilles_vendeur:
                    with ThreadPoolExecutor(max_workers=MONITOR_MAX_WORKERS) as executor:
                        list(executor.map(_traiter_une_veille_vendeur, veilles_vendeur))

                _verifier_wishlist()
            else:
                debut, fin = get_plage_horaire()
                logger.info(f"Hors plage horaire de veille ({debut}h-{fin}h) : cycle sauté.")

            _pinger_heartbeat()

        except Exception as e:
            logger.error(f"Erreur moniteur : {e}")
        time.sleep(MONITOR_CYCLE_SECONDS)


def _envoyer_apercu_immediat(chat_id, query, min_p, max_p, size, status, limit_count, exclude_keywords):
    """Lance un premier scan tout de suite après la création d'une recherche
    et montre 2-3 résultats en exemple, pour que l'utilisateur voie
    immédiatement que ça marche au lieu d'attendre le prochain cycle."""
    try:
        apercu_limit = min(limit_count, 3)
        resultats = chercher_vinted(query, min_p, max_p, size, status, apercu_limit)
        resultats = _filtrer_mots_exclus(resultats, exclude_keywords)
        nouveaux, _ = filtrer_doublons_et_baisses(chat_id, resultats)
        if nouveaux:
            envoyer_message(chat_id, f"🔍 *Aperçu immédiat* — {len(nouveaux)} exemple(s) de ce que tu vas recevoir :")
            envoyer_alerte_telegram(chat_id, nouveaux)
        else:
            envoyer_message(chat_id, "🔍 Aperçu immédiat : rien à montrer pour l'instant, mais la veille continue en arrière-plan.")
    except Exception as e:
        logger.error(f"Erreur aperçu immédiat pour {chat_id} : {e}")


# ---------------------------------------------------------------------------
# Assistant /newsearch : étapes avec bouton ⬅️ Retour, sélection multiple
# (tailles et états) et récapitulatif final.
# ---------------------------------------------------------------------------
# Les choix multiples sont stockés en base sous forme d'IDs séparés par une
# virgule (ex: "209,210") dans les colonnes TEXT existantes `size` / `status` :
# aucune migration nécessaire, et les anciennes recherches (1 seul ID) restent valides.
ETATS_VINTED = [
    ("🏷️ Neuf avec étiquette", "6"),
    ("📦 Neuf sans étiquette", "1"),
    ("💎 Très bon état", "2"),
    ("👕 Bon état", "3"),
]
TAILLES_VETEMENT = {
    "femme": [("XS", "36"), ("S", "208"), ("M", "209"), ("L", "210"), ("XL", "211"), ("XXL", "32")],
    "homme": [("XS", "1200"), ("S", "1201"), ("M", "1202"), ("L", "1203"), ("XL", "1204"), ("XXL", "1205")],
    "enfant": [("XS", "600"), ("S", "601"), ("M", "602"), ("L", "603"), ("XL", "604"), ("XXL", "605")],
}
POINTURES = [(str(n), str(75 + i)) for i, n in enumerate(range(38, 46))]  # 38 -> 75 ... 45 -> 82

# Étapes où l'utilisateur doit cliquer sur un bouton (un texte tapé à ce moment-là est ignoré).
ETAPES_A_BOUTONS = {
    "waiting_category", "waiting_gender", "waiting_clothing_size", "waiting_shoe_size",
    "waiting_status", "waiting_warning", "waiting_recap",
}


def _libelle_etats(status):
    """'6,2' -> '🏷️ Neuf avec étiquette, 💎 Très bon état' (None -> 'Tous')."""
    if not status:
        return "Tous"
    noms = {id_: label for label, id_ in ETATS_VINTED}
    return ", ".join(noms.get(i, i) for i in str(status).split(","))


def _avec_retour(rows, step):
    """Ajoute la ligne ⬅️ Retour sous un clavier inline (sauf à la toute 1re étape)."""
    rows = list(rows)
    if step != "waiting_query":
        rows.append([{"text": "⬅️ Retour", "callback_data": f"back_{step}"}])
    return {"inline_keyboard": rows}


def _clavier_multi(options, selection, prefixe, step, par_ligne=3, libelle_ok="✔️ Valider"):
    """Boutons à cocher : chaque clic ajoute/retire un ✅, puis 'Valider'."""
    boutons = [
        {"text": ("✅ " if id_ in selection else "") + label, "callback_data": f"{prefixe}_{id_}"}
        for label, id_ in options
    ]
    rows = [boutons[i:i + par_ligne] for i in range(0, len(boutons), par_ligne)]
    rows.append([{"text": libelle_ok, "callback_data": f"{prefixe}ok"}])
    return _avec_retour(rows, step)


def _options_taille(state):
    if state.get("step") == "waiting_shoe_size":
        return POINTURES
    return TAILLES_VETEMENT.get(state.get("gender", "femme"), TAILLES_VETEMENT["femme"])


def _clavier_tailles(state):
    return _clavier_multi(_options_taille(state), state.get("size_sel", []), "tsz", state["step"], par_ligne=3)


def _clavier_etats(state):
    sel = state.get("status_sel", [])
    ok = "✔️ Valider" if sel else "✔️ Valider (tous les états)"
    return _clavier_multi(ETATS_VINTED, sel, "tst", "waiting_status", par_ligne=1, libelle_ok=ok)


def _maj_boutons(chat_id, message_id, reply_markup):
    """Met à jour (ou retire, avec un clavier vide) les boutons d'un message déjà envoyé."""
    envoyer_requete(
        URL_TELEGRAM_SENDER, "editMessageReplyMarkup",
        {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
    )


def _marquer_etape(chat_id, step):
    """Change d'étape en mémorisant l'étape quittée (pour que ⬅️ Retour puisse y revenir)."""
    state = user_states.get(chat_id)
    if not state:
        return None
    actuel = state.get("step")
    if actuel and actuel != step:
        state.setdefault("history", []).append(actuel)
    state["step"] = step
    return state


def aller_a(chat_id, step):
    if _marquer_etape(chat_id, step):
        poser_etape(chat_id, step)


def retour(chat_id):
    """Revient à l'étape précédente et la ré-affiche. Les choix déjà faits sont conservés
    (cases cochées, etc.) : l'utilisateur corrige juste ce qu'il veut."""
    state = user_states.get(chat_id)
    if not state:
        return
    hist = state.setdefault("history", [])
    while hist and hist[-1] == "waiting_warning":  # l'écran d'avertissement n'est pas une vraie étape
        hist.pop()
    if not hist:
        return
    state["step"] = hist.pop()
    poser_etape(chat_id, state["step"])


def poser_etape(chat_id, step):
    """Affiche la question correspondant à une étape (utilisé en avançant ET en revenant en arrière)."""
    state = user_states.get(chat_id) or {}
    if step == "waiting_query":
        envoyer_message(chat_id, "🔍 Quel article souhaites-tu rechercher ? (ex: `nike tn`, `carhartt`)")
    elif step == "waiting_min_price":
        envoyer_message(chat_id, "💰 Entre le *Prix Min* (ex: `10` ou `0` pour ignorer).", reply_markup=_avec_retour([], step))
    elif step == "waiting_max_price":
        envoyer_message(chat_id, "💰 Entre le *Prix Max* (ex: `50` ou `none` pour ignorer) :", reply_markup=_avec_retour([], step))
    elif step == "waiting_exclude":
        envoyer_message(
            chat_id,
            "🚫 Des mots-clés à *exclure* des résultats ? (séparés par une virgule, ex: `réplique, copie`)\n"
            "Tape `aucun` si tu ne veux rien exclure :",
            reply_markup=_avec_retour([], step),
        )
    elif step == "waiting_category":
        kb = [
            [{"text": "👕 Vêtements", "callback_data": "cat_vetement"}],
            [{"text": "👟 Chaussures", "callback_data": "cat_chaussure"}],
            [{"text": "🔄 Aucune / Toutes tailles", "callback_data": "cat_none"}],
        ]
        envoyer_message(chat_id, "📂 Quel type d'article recherches-tu ?", reply_markup=_avec_retour(kb, step))
    elif step == "waiting_gender":
        kb = [
            [{"text": "👩 Femme", "callback_data": "gender_femme"}, {"text": "👨 Homme", "callback_data": "gender_homme"}],
            [{"text": "👶 Enfant", "callback_data": "gender_enfant"}],
        ]
        envoyer_message(chat_id, "👥 S'agit-il d'un vestiaire *Femme*, *Homme* ou *Enfant* ?", reply_markup=_avec_retour(kb, step))
    elif step == "waiting_clothing_size":
        envoyer_message(
            chat_id, "👕 Choisis *une ou plusieurs* tailles, puis appuie sur *Valider* :",
            reply_markup=_clavier_tailles(state),
        )
    elif step == "waiting_shoe_size":
        envoyer_message(
            chat_id, "👟 Choisis *une ou plusieurs* pointures, puis appuie sur *Valider* :",
            reply_markup=_clavier_tailles(state),
        )
    elif step == "waiting_status":
        envoyer_message(
            chat_id,
            "✨ Choisis *un ou plusieurs* états selon les standards Vinted, puis appuie sur *Valider*.\n"
            "_Aucun état coché = tous les états._",
            reply_markup=_clavier_etats(state),
        )
    elif step == "waiting_limit":
        envoyer_message(
            chat_id,
            "📦 Combien de *nouveaux articles max* veux-tu récupérer par analyse ? (recommandé : moins de 20)",
            reply_markup=_avec_retour([], step),
        )
    elif step == "waiting_recap":
        afficher_recap(chat_id)


def finaliser_creation_recherche(chat_id):
    state = user_states.get(chat_id)
    if not state:
        return
    q, min_p, max_p = state["query"], state["min_price"], state["max_price"]
    size, size_label = state["size"], state.get("size_label", "Toutes")
    status, limit_count = state["status"], state["limit_count"]
    exclude_keywords = state.get("exclude_keywords")

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_premium FROM users WHERE chat_id = ?", (chat_id,))
        res_user = cursor.fetchone()
        is_premium = res_user[0] if res_user else 0

        cursor.execute("SELECT COUNT(*) FROM searches WHERE chat_id = ?", (chat_id,))
        nb_recherches = cursor.fetchone()[0]

        if nb_recherches >= FREE_PLAN_MAX_SEARCHES and not is_premium:
            envoyer_message(
                chat_id,
                "❌ Limite atteinte (1 recherche gratuite). Faites un `/delete` ou un `/stop` pour en lancer "
                "une autre, ou activez votre token sur le [Hub Central](https://t.me/VintedPulseHub_bot) !",
            )
            del user_states[chat_id]
            return

        cursor.execute(
            "INSERT INTO searches (chat_id, query, min_price, max_price, size, size_label, status, limit_count, is_paused, exclude_keywords) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (chat_id, q, min_p, max_p, size, size_label, status, limit_count, exclude_keywords),
        )

    exclude_txt = f"🚫 Exclu : `{exclude_keywords}`\n" if exclude_keywords else ""
    resume_msg = (
        f"✅ *Recherche enregistrée avec succès !*\n\n"
        f"🔍 Article : `{q}`\n"
        f"💰 Prix : `{min_p or '0'}€ - {max_p or 'Max'}€`\n"
        f"📏 Taille : `{size_label}`\n"
        f"✨ État : `{_libelle_etats(status)}`\n"
        f"{exclude_txt}"
        f"📦 Échantillon : `{limit_count} articles`\n\n"
        f"⚡ Le bot surveille le marché en arrière-plan."
    )
    envoyer_message(chat_id, resume_msg)
    del user_states[chat_id]

    _envoyer_apercu_immediat(chat_id, q, min_p, max_p, size, status, limit_count, exclude_keywords)


def afficher_recap(chat_id):
    """Étape de confirmation avant l'enregistrement définitif."""
    state = user_states.get(chat_id)
    if not state:
        return
    q, min_p, max_p = state["query"], state["min_price"], state["max_price"]
    size_label, limit_count = state.get("size_label", "Toutes"), state["limit_count"]
    exclude_keywords = state.get("exclude_keywords")

    exclude_txt = f"🚫 Exclu : `{exclude_keywords}`\n" if exclude_keywords else ""
    recap = (
        f"📋 *Récapitulatif de ta recherche*\n\n"
        f"🔍 Article : `{q}`\n"
        f"💰 Prix : `{min_p or '0'}€ - {max_p or 'Max'}€`\n"
        f"📏 Taille : `{size_label}`\n"
        f"✨ État : `{_libelle_etats(state.get('status'))}`\n"
        f"{exclude_txt}"
        f"📦 Échantillon : `{limit_count} articles`\n\n"
        f"Tout est bon ?"
    )
    kb = [
        [{"text": "✅ Confirmer", "callback_data": "confirm_search"}],
        [{"text": "🔄 Recommencer", "callback_data": "restart_search"}],
    ]
    envoyer_message(chat_id, recap, reply_markup=_avec_retour(kb, "waiting_recap"))


def verifier_et_avertir_limite(chat_id, limit_val):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ignore_warning_20, ignore_warning_50, ignore_warning_100 FROM users WHERE chat_id = ?",
            (chat_id,),
        )
        row = cursor.fetchone()

    ign_20 = row[0] if row else 0
    ign_50 = row[1] if row else 0
    ign_100 = row[2] if row else 0

    state = user_states.get(chat_id)
    if state:
        state["limit_count"] = limit_val

    niveaux = [
        (100, ign_100, "🚨 Faire quand même",
         "⚠️ **TRÈS GROS WARNING (≥ 100)** ⚠️\n\nDemander plus de 100 articles d'un coup va générer un flux "
         "massif de messages et risque de saturer ton Telegram ou de déclencher des protections anti-bot "
         "Vinted. Es-tu sûr ?"),
        (50, ign_50, "⚠️ Faire quand même",
         "⚠️ **Gros warning (≥ 50)** ⚠️\n\nPlus de 50 articles par analyse risque d'envoyer beaucoup de "
         "notifications d'un coup. Veux-tu continuer ?"),
        (20, ign_20, "✅ Faire quand même",
         "💡 **Petit warning (≥ 20)**\n\nAu-delà de 20 articles par recherche, le bot va commencer à envoyer "
         "pas mal de messages. Es-tu d'accord ?"),
    ]
    for seuil, ignore, libelle_proceed, texte in niveaux:
        if limit_val >= seuil and not ignore:
            kb = [
                [{"text": libelle_proceed, "callback_data": f"warn_proceed_{seuil}"}],
                [{"text": "✏️ Changer le nombre", "callback_data": "warn_change"}],
                [{"text": "🔕 Ne plus afficher ce message", "callback_data": f"warn_ignore_{seuil}"}],
            ]
            _marquer_etape(chat_id, "waiting_warning")
            envoyer_message(chat_id, texte, reply_markup=_avec_retour(kb, "waiting_warning"))
            return

    aller_a(chat_id, "waiting_recap")


def _gerer_callback_assistant(callback, chat_id, data_callback):
    """⬅️ Retour et sélections multiples (taille / état). Répond lui-même au clic
    pour pouvoir afficher une petite alerte (ex: 'choisis au moins une taille')."""
    query_id = callback["id"]
    message_id = callback["message"]["message_id"]

    def repondre(texte=None):
        payload = {"callback_query_id": query_id}
        if texte:
            payload.update({"text": texte, "show_alert": True})
        envoyer_requete(URL_TELEGRAM_SENDER, "answerCallbackQuery", payload)

    state = user_states.get(chat_id)
    if not state:
        repondre("⌛ Cette création de recherche n'est plus en cours. Tape /newsearch pour en relancer une.")
        return
    step = state.get("step")

    # --- ⬅️ Retour ---
    if data_callback.startswith("back_"):
        if data_callback[len("back_"):] != step:  # bouton d'un ancien message
            repondre("↩️ Ce bouton n'est plus actif.")
            return
        repondre()
        _maj_boutons(chat_id, message_id, {"inline_keyboard": []})  # on fige l'ancien message
        retour(chat_id)
        return

    # --- Tailles (sélection multiple) ---
    if data_callback.startswith("tsz"):
        if step not in ("waiting_clothing_size", "waiting_shoe_size"):
            repondre()
            return
        options = _options_taille(state)
        selection = state.setdefault("size_sel", [])
        if data_callback == "tszok":
            if not selection:
                repondre("Choisis au moins une taille (ou fais ⬅️ Retour pour ne pas filtrer par taille).")
                return
            gardes = [(label, id_) for label, id_ in options if id_ in selection]
            state["size"] = ",".join(id_ for _, id_ in gardes)
            state["size_label"] = ", ".join(label for label, _ in gardes)
            repondre()
            _maj_boutons(chat_id, message_id, {"inline_keyboard": []})
            aller_a(chat_id, "waiting_status")
            return
        id_ = data_callback.split("_", 1)[1]
        if id_ not in [i for _, i in options]:
            repondre()
            return
        if id_ in selection:
            selection.remove(id_)
        else:
            selection.append(id_)
        repondre()
        _maj_boutons(chat_id, message_id, _clavier_tailles(state))
        return

    # --- États (sélection multiple) ---
    if data_callback.startswith("tst"):
        if step != "waiting_status":
            repondre()
            return
        selection = state.setdefault("status_sel", [])
        if data_callback == "tstok":
            gardes = [id_ for _, id_ in ETATS_VINTED if id_ in selection]
            state["status"] = ",".join(gardes) if gardes else None
            repondre()
            _maj_boutons(chat_id, message_id, {"inline_keyboard": []})
            aller_a(chat_id, "waiting_limit")
            return
        id_ = data_callback.split("_", 1)[1]
        if id_ not in [i for _, i in ETATS_VINTED]:
            repondre()
            return
        if id_ in selection:
            selection.remove(id_)
        else:
            selection.append(id_)
        repondre()
        _maj_boutons(chat_id, message_id, _clavier_etats(state))
        return

    repondre()


def gerer_callback_query(callback):
    chat_id = str(callback["message"]["chat"]["id"])
    data_callback = callback["data"]
    query_id = callback["id"]

    # Le carrousel répond lui-même au clic (pour pouvoir afficher "expiré" en pop-up).
    if data_callback.startswith("car_"):
        _naviguer_carousel(callback, chat_id, data_callback)
        return

    # Idem pour ⬅️ Retour et les sélections multiples de l'assistant /newsearch.
    if data_callback.startswith(("back_", "tsz", "tst")):
        _gerer_callback_assistant(callback, chat_id, data_callback)
        return

    envoyer_requete(URL_TELEGRAM_SENDER, "answerCallbackQuery", {"callback_query_id": query_id})

    if data_callback.startswith("del_"):
        search_id = data_callback.split("_")[1]
        with get_connection() as conn:
            conn.execute("DELETE FROM searches WHERE id = ? AND chat_id = ?", (search_id, chat_id))
        envoyer_message(chat_id, "🗑️ Recherche supprimée avec succès !")

    elif data_callback.startswith("wldel_"):
        item_id = data_callback.split("_")[1]
        supprimer_wishlist(item_id, chat_id)
        envoyer_message(chat_id, "🗑️ Article retiré de la wishlist.")

    elif data_callback.startswith("svdel_"):
        watch_id = data_callback.split("_")[1]
        supprimer_veille_vendeur(watch_id, chat_id)
        envoyer_message(chat_id, "🗑️ Veille vendeur supprimée.")

    elif data_callback.startswith("cat_"):
        state = user_states.get(chat_id)
        if not state or state.get("step") != "waiting_category":
            return
        cat_val = data_callback.split("_")[1]
        state["size_sel"] = []  # nouvelle catégorie = nouvelle sélection de tailles
        if cat_val == "none":
            state["size"] = None
            state["size_label"] = "Toutes"
            aller_a(chat_id, "waiting_status")
        elif cat_val == "vetement":
            aller_a(chat_id, "waiting_gender")
        elif cat_val == "chaussure":
            aller_a(chat_id, "waiting_shoe_size")

    elif data_callback.startswith("gender_"):
        state = user_states.get(chat_id)
        if not state or state.get("step") != "waiting_gender":
            return
        gender = data_callback.split("_")[1]
        if state.get("gender") != gender:
            state["size_sel"] = []  # les IDs de taille dépendent du vestiaire
        state["gender"] = gender
        aller_a(chat_id, "waiting_clothing_size")

    elif data_callback == "confirm_search":
        state = user_states.get(chat_id)
        if state and state.get("step") == "waiting_recap":
            finaliser_creation_recherche(chat_id)

    elif data_callback == "restart_search":
        user_states[chat_id] = {"step": "waiting_query", "history": []}
        envoyer_message(chat_id, "🔄 Pas de souci, recommençons.\n\n🔍 Quel article souhaites-tu rechercher ?")

    elif data_callback.startswith("warn_"):
        state = user_states.get(chat_id)
        if not state or state.get("step") != "waiting_warning":
            return
        action = data_callback.split("_")[1]
        if action == "change":
            retour(chat_id)
        elif action == "proceed":
            aller_a(chat_id, "waiting_recap")
        elif action == "ignore":
            level = data_callback.split("_")[2]
            if level in ("20", "50", "100"):  # whitelist : évite d'injecter un nom de colonne arbitraire
                with get_connection() as conn:
                    conn.execute(f"UPDATE users SET ignore_warning_{level} = 1 WHERE chat_id = ?", (chat_id,))
            aller_a(chat_id, "waiting_recap")


def gerer_assistant_recherche(chat_id, text, state):
    step = state["step"]
    if text.lower() in ["/cancel", "/stop", "annuler"]:
        del user_states[chat_id]
        envoyer_message(chat_id, "❌ Action annulée.")
        return

    if step == "waiting_feedback":
        if len(text) < 5:
            envoyer_message(chat_id, "⚠️ Ton message est un peu court. Décris ton idée ou ton retour un peu plus précisément :")
            return
        with get_connection() as conn:
            conn.execute("INSERT INTO feedbacks (chat_id, feedback_text) VALUES (?, ?)", (chat_id, text))

        notification_msg = f"💡 *Nouveau Feedback Reçu !*\n\n👤 *De (Chat ID)* : `{chat_id}`\n💬 *Message* :\n{text}"
        _envoyer_message_api(URL_TELEGRAM_DEV, DEV_CHAT_ID, notification_msg)

        del user_states[chat_id]
        envoyer_message(chat_id, "🙏 *Merci beaucoup !* Ton retour a bien été transmis au créateur.")
        return

    if step == "waiting_query":
        if len(text) < 2:
            envoyer_message(chat_id, "⚠️ Nom trop court. Réessaie :")
            return
        state["query"] = text
        aller_a(chat_id, "waiting_min_price")

    elif step == "waiting_min_price":
        if text.lower() in ["0", "none", ""]:
            state["min_price"] = None
        else:
            try:
                val = float(text.replace(",", "."))
                if val < 0:
                    raise ValueError()
                state["min_price"] = str(val)
            except ValueError:
                envoyer_message(chat_id, "⚠️ Prix invalide. Réessaie (ex: 10 ou 0) :", reply_markup=_avec_retour([], step))
                return
        aller_a(chat_id, "waiting_max_price")

    elif step == "waiting_max_price":
        if text.lower() in ["none", ""]:
            state["max_price"] = None
        else:
            try:
                val = float(text.replace(",", "."))
                min_p = float(state["min_price"]) if state.get("min_price") else 0
                if val <= min_p:
                    envoyer_message(chat_id, "⚠️ Le prix max doit être supérieur au min. Réessaie :", reply_markup=_avec_retour([], step))
                    return
                state["max_price"] = str(val)
            except ValueError:
                envoyer_message(chat_id, "⚠️ Prix invalide. Réessaie :", reply_markup=_avec_retour([], step))
                return
        aller_a(chat_id, "waiting_exclude")

    elif step == "waiting_exclude":
        if text.strip().lower() in ["aucun", "non", "none", "-", "n/a"]:
            state["exclude_keywords"] = None
        else:
            state["exclude_keywords"] = text.strip()
        aller_a(chat_id, "waiting_category")

    elif step == "waiting_limit":
        try:
            limit_val = int(text.strip())
            if limit_val <= 0:
                raise ValueError()
        except ValueError:
            envoyer_message(chat_id, "⚠️ Nombre invalide. Entre un entier positif (ex: 20) :", reply_markup=_avec_retour([], step))
            return
        verifier_et_avertir_limite(chat_id, limit_val)

    elif step in ETAPES_A_BOUTONS:
        envoyer_message(chat_id, "👆 Utilise les boutons du message ci-dessus (ou ⬅️ Retour pour corriger une étape).")


def gerer_commandes_texte(chat_id, text, message_from=None):
    chat_id = str(chat_id)

    # Boutons du clavier persistant -> équivalents des commandes slash.
    if text in REPLY_KEYBOARD_MAP:
        text = REPLY_KEYBOARD_MAP[text]

    if text.startswith("/start"):
        first_name = message_from.get("first_name", "Inconnu") if message_from else "Inconnu"
        username = message_from.get("username") if message_from else None
        etait_deja_connu = get_user(chat_id) is not None
        upsert_user(chat_id, first_name=first_name, username=username, is_linked=1)

        parts = text.split(" ", 1)
        payload = parts[1].strip() if len(parts) > 1 else None
        if payload and payload.startswith("ref_") and not etait_deja_connu:
            referrer_id = payload[len("ref_"):].strip()
            if referrer_id and referrer_id.isdigit():
                credite = credit_referral(chat_id, referrer_id, REFERRAL_BONUS_DAYS)
                if credite:
                    envoyer_message(chat_id, f"🎁 Tu as été parrainé ! +{REFERRAL_BONUS_DAYS} jour(s) de Premium offerts.")
                    envoyer_message(referrer_id, f"🎉 Un ami a rejoint VintedPulse grâce à ton lien ! +{REFERRAL_BONUS_DAYS} jour(s) de Premium offerts.")

        envoyer_message(
            chat_id,
            "🤖 *Bienvenue sur VintedPulse Bot !*\n\nTon compte est actif et connecté. Tape `/newsearch` pour créer ta première alerte de veille.",
            reply_markup=clavier_principal(),
        )
        return

    if text == "/newsearch":
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_premium FROM users WHERE chat_id = ?", (chat_id,))
            user_row = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) FROM searches WHERE chat_id = ?", (chat_id,))
            nb_recherches = cursor.fetchone()[0]

        if not user_row:
            first_name = message_from.get("first_name") if message_from else None
            username = message_from.get("username") if message_from else None
            upsert_user(chat_id, first_name=first_name, username=username, is_linked=1)
            is_premium = 0
        else:
            is_premium = user_row[0]

        if nb_recherches >= FREE_PLAN_MAX_SEARCHES and not is_premium:
            envoyer_message(
                chat_id,
                "❌ Vous avez le droit à seulement une recherche en simultané en étant en plan Gratuit. "
                "Veuillez faire un `/delete` ou un `/stop` pour en lancer une autre, "
                "ou entrez votre token sur le [Hub Central](https://t.me/VintedPulseHub_bot) pour passer Premium !",
            )
            return

        user_states[chat_id] = {"step": "waiting_query", "history": []}
        envoyer_message(chat_id, "🔍 *Nouvelle Recherche Vinted*\n\nQuel article souhaites-tu rechercher ? (ex: `nike tn`, `carhartt`)")
        return

    if text in ["/disconnect", "/unlink"]:
        with get_connection() as conn:
            conn.execute("UPDATE users SET is_linked = 0 WHERE chat_id = ?", (chat_id,))
        envoyer_message(chat_id, "🔌 *Compte dissocié avec succès !*")
        return

    if text in ["/delete", "/supprimer"]:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, query FROM searches WHERE chat_id = ?", (chat_id,))
            searches = cursor.fetchall()
        if not searches:
            envoyer_message(chat_id, "📋 Aucune recherche active.")
        else:
            inline_keyboard = [[{"text": f"❌ Supprimer : {q_text}", "callback_data": f"del_{s_id}"}] for s_id, q_text in searches]
            envoyer_message(chat_id, "👇 Sélectionne la recherche à supprimer :", reply_markup={"inline_keyboard": inline_keyboard})
        return

    if text == "/stop":
        with get_connection() as conn:
            conn.execute("DELETE FROM searches WHERE chat_id = ?", (chat_id,))
        envoyer_message(chat_id, "🛑 **Toutes vos recherches ont été arrêtées et supprimées d'un coup !**")
        return

    if text == "/cancel":
        if chat_id in user_states:
            del user_states[chat_id]
            envoyer_message(chat_id, "❌ Création de recherche annulée.")
        else:
            envoyer_message(chat_id, "ℹ️ Aucune action en cours à annuler.")
        return

    if text in ["/list", "/mes-recherches"]:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT query, min_price, max_price, size_label, limit_count, exclude_keywords, is_paused, status "
                "FROM searches WHERE chat_id = ?",
                (chat_id,),
            )
            queries = cursor.fetchall()
        if queries:
            msg = "📋 *Vos recherches actives :*\n"
            for q in queries:
                label_taille = q[3] if q[3] else "Toutes"
                excl_txt = f" | 🚫 {q[5]}" if q[5] else ""
                pause_txt = " ⏸️" if q[6] else ""
                etat_txt = f" | État: {_libelle_etats(q[7])}" if q[7] else ""
                msg += f"• `{q[0]}` (Prix: {q[1] or '0'}€-{q[2] or 'Max'}€ | Taille: {label_taille}{etat_txt} | Échantillon: {q[4]}{excl_txt}){pause_txt}\n"
        else:
            msg = "Aucune recherche active."
        envoyer_message(chat_id, msg)
        return

    if text == "/pause":
        with get_connection() as conn:
            conn.execute("UPDATE searches SET is_paused = 1 WHERE chat_id = ?", (chat_id,))
        envoyer_message(chat_id, "⏸️ Recherches mises en pause.")
        return

    if text in ["/resume", "/unpause"]:
        with get_connection() as conn:
            conn.execute("UPDATE searches SET is_paused = 0 WHERE chat_id = ?", (chat_id,))
        envoyer_message(chat_id, "▶️ Recherches relancées !")
        return

    if text == "/stats":
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM searches WHERE chat_id = ?", (chat_id,))
            total_searches = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM seen_items WHERE chat_id = ?", (chat_id,))
            total_seen = cursor.fetchone()[0]
            cursor.execute("SELECT is_premium, premium_expires_at, referral_credits FROM users WHERE chat_id = ?", (chat_id,))
            u = cursor.fetchone()
        is_p = u[0] if u else 0
        expires_at = u[1] if u else None
        parrainages = u[2] if u else 0
        if is_p and expires_at:
            statut_txt = f"👑 Premium (jusqu'au {expires_at})"
        elif is_p:
            statut_txt = "👑 Premium"
        else:
            statut_txt = "🆓 Gratuit"
        stats_msg = (
            f"📊 *Vos Statistiques*\n\nStatut : `{statut_txt}`\n🔍 Recherches : `{total_searches}`\n"
            f"📦 Articles vus : `{total_seen}`\n🎁 Amis parrainés : `{parrainages}`"
        )
        envoyer_message(chat_id, stats_msg)
        return

    if text == "/parrain":
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referral_credits FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
        parrainages = row[0] if row else 0
        lien = f"https://t.me/{SENDER_BOT_USERNAME}?start=ref_{chat_id}"
        envoyer_message(
            chat_id,
            f"🎁 *Programme de parrainage*\n\nInvite un ami avec ce lien : il obtient **{REFERRAL_BONUS_DAYS} jours de Premium**, "
            f"et toi aussi dès qu'il démarre le bot !\n\n🔗 {lien}\n\n👥 Amis déjà parrainés : `{parrainages}`",
        )
        return

    if text.startswith("/watch"):
        parts = text.split(" ", 1)
        if len(parts) < 2 or "vinted" not in parts[1].lower():
            envoyer_message(chat_id, "⚠️ Utilisation : `/watch <lien de l'annonce Vinted>`")
            return
        item_url = parts[1].strip()
        titre, prix = obtenir_prix_item(item_url)
        if prix is None:
            envoyer_message(
                chat_id,
                "❌ Impossible de récupérer le prix de cette annonce pour le moment "
                "(fonctionnalité expérimentale, vérifie le lien ou réessaie plus tard).",
            )
            return
        ajouter_wishlist(chat_id, item_url, titre, prix)
        envoyer_message(chat_id, f"❤️ Ajouté à ta wishlist : **{titre}** à `{prix}€`. Tu seras alerté si le prix baisse.")
        return

    if text == "/wishlist":
        items = lister_wishlist(chat_id)
        if not items:
            envoyer_message(chat_id, "❤️ Ta wishlist est vide. Utilise `/watch <lien>` pour ajouter une annonce.")
        else:
            msg = "❤️ *Ta wishlist :*\n\n" + "\n".join(f"• `{titre}` — {prix}€" for _, _, titre, _, prix in items)
            envoyer_message(chat_id, msg)
            kb = [[{"text": f"🗑️ {titre[:30]}", "callback_data": f"wldel_{item_id}"}] for item_id, _, titre, _, _ in items]
            envoyer_message(chat_id, "👇 Retirer un article :", reply_markup={"inline_keyboard": kb})
        return

    if text.startswith("/trackseller"):
        parts = text.split(" ", 1)
        if len(parts) < 2:
            envoyer_message(chat_id, "⚠️ Utilisation : `/trackseller <pseudo ou lien du profil Vinted>`")
            return
        identifiant = parts[1].strip()
        seller_id, label = resoudre_vendeur(identifiant)
        if not seller_id:
            envoyer_message(
                chat_id,
                "❌ Vendeur introuvable (fonctionnalité expérimentale). "
                "Essaie avec le lien complet du profil (ex: `https://www.vinted.fr/member/12345678-pseudo`).",
            )
            return
        ajouter_veille_vendeur(chat_id, seller_id, label or identifiant)
        # Baseline silencieuse : on marque les annonces déjà en ligne comme vues,
        # pour n'être alerté que des NOUVELLES annonces (et pas de tout le
        # catalogue existant au premier cycle).
        try:
            filtrer_doublons_et_baisses(chat_id, chercher_items_vendeur(seller_id, 96))
        except Exception as e:
            logger.error(f"Baseline vendeur {seller_id} impossible : {e}")
        envoyer_message(chat_id, f"🕵️ Veille activée sur le vendeur **{label or identifiant}** ! Tu seras alerté de ses nouvelles annonces.")
        return

    if text == "/sellers":
        watches = lister_veilles_vendeur(chat_id)
        if not watches:
            envoyer_message(chat_id, "🕵️ Aucun vendeur suivi pour l'instant. Utilise `/trackseller <pseudo>`.")
        else:
            msg = "🕵️ *Vendeurs suivis :*\n\n" + "\n".join(f"• `{label}`" for _, _, label, _ in watches)
            envoyer_message(chat_id, msg)
            kb = [[{"text": f"🗑️ {label[:30]}", "callback_data": f"svdel_{w_id}"}] for w_id, _, label, _ in watches]
            envoyer_message(chat_id, "👇 Retirer une veille :", reply_markup={"inline_keyboard": kb})
        return

    if text == "/feedback":
        user_states[chat_id] = {"step": "waiting_feedback"}
        envoyer_message(chat_id, "💡 *Boîte à idées*\n\nÉcris ta suggestion ou ton bug ici :")
        return

    if text in ["/help", "/aide"]:
        aide_msg = (
            "🛠️ *Aide - VintedPulse*\n\n"
            "• `/newsearch` : Créer une alerte pas à pas.\n"
            "• `/list` : Vos recherches.\n"
            "• `/delete` : Supprimer une recherche.\n"
            "• `/stop` : Tout stopper d'un coup.\n"
            "• `/cancel` : Annuler la création en cours.\n"
            "• `/pause` / `/resume` : Mettre en pause / reprendre la veille.\n"
            "• `/watch <lien>` : Ajouter une annonce à la wishlist.\n"
            "• `/wishlist` : Voir/gérer ta wishlist.\n"
            "• `/trackseller <pseudo>` : Suivre un vendeur.\n"
            "• `/sellers` : Voir/gérer les vendeurs suivis.\n"
            "• `/parrain` : Ton lien de parrainage.\n"
            "• `/disconnect` : Dissocier le compte.\n"
            "• `/feedback` : Suggérer une idée."
        )
        envoyer_message(chat_id, aide_msg, reply_markup=clavier_principal())
        return


def ecouter_telegram():
    envoyer_requete(URL_TELEGRAM_SENDER, "deleteWebhook?drop_pending_updates=true", {})

    offset = 0
    logger.info("🤖 Bot Sender (@VintedPulseBot) 100% opérationnel...")
    while True:
        try:
            response = requests.get(URL_TELEGRAM_SENDER + f"getUpdates?offset={offset}&timeout=30", timeout=35)
            data = response.json()
            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        gerer_callback_query(update["callback_query"])
                    elif "message" in update and "text" in update["message"]:
                        msg_obj = update["message"]
                        chat_id = str(msg_obj["chat"]["id"])
                        text = msg_obj["text"].strip()
                        message_from = msg_obj.get("from", {})

                        if chat_id in user_states:
                            gerer_assistant_recherche(chat_id, text, user_states[chat_id])
                        else:
                            gerer_commandes_texte(chat_id, text, message_from)
        except Exception as e:
            logger.error(f"Erreur polling sender : {e}")
            time.sleep(5)


if __name__ == "__main__":
    init_db()
    definir_commandes_sender()
    threading.Thread(target=ecouter_telegram, daemon=True).start()
    threading.Thread(target=background_monitor, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
