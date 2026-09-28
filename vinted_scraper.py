"""
Recherche d'articles Vinted.

Correctif principal : l'ancienne version relançait un navigateur Chromium
complet (Playwright) à CHAQUE recherche, pour chaque utilisateur, toutes les
3 minutes — juste pour récupérer un cookie de session et un csrf token.
C'était très lourd en CPU/RAM et lent.

Ici, la session (cookies + csrf token) est mise en cache pendant
VINTED_SESSION_TTL_SECONDS et réutilisée pour tous les appels API tant
qu'elle est valide. Le navigateur n'est relancé que quand le cache expire,
ou si l'API Vinted répond 401/403 (session invalidée côté serveur).

⚠️ Fonctions marquées "NON TESTÉ EN CONDITIONS RÉELLES" : ce container n'a
pas d'accès réseau vers Vinted, donc ces endpoints n'ont pas pu être validés
en live. Teste-les d'abord à la main avant de compter dessus en prod.
(obtenir_prix_item : format JSON-LD validé sur une vraie page d'annonce, le
chargement réseau reste à tester ; veille vendeur : toujours non validée.)
"""
import re
import json
import time
import threading
import requests
from playwright.sync_api import sync_playwright

from config import VINTED_SESSION_TTL_SECONDS
from logger_config import get_logger

logger = get_logger("vinted_scraper")

_lock = threading.Lock()
_cache = {"cookies": None, "csrf": None, "expires_at": 0}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "fr-FR,fr;q=0.9",
    "origin": "https://www.vinted.fr",
    "referer": "https://www.vinted.fr/",
    "user-agent": USER_AGENT,
    "x-next-app": "web",
    "platform": "web",
    "locale": "fr",
}


def _rafraichir_session():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
        page = context.new_page()
        try:
            page.goto("https://www.vinted.fr/", wait_until="domcontentloaded", timeout=20000)
            cookies_dict = {}
            for c in context.cookies():
                name, value = c.get("name"), c.get("value")
                if name and value:
                    cookies_dict[str(name)] = str(value)
            csrf_token = page.evaluate(
                "() => { const meta = document.querySelector('meta[name=\"csrf-token\"]'); "
                "return meta ? meta.content : ''; }"
            )
        finally:
            browser.close()
    return cookies_dict, csrf_token


def get_session(force_refresh=False):
    with _lock:
        now = time.time()
        if not force_refresh and _cache["cookies"] and now < _cache["expires_at"]:
            return _cache["cookies"], _cache["csrf"]
        try:
            cookies, csrf = _rafraichir_session()
        except Exception as e:
            logger.error(f"Erreur init session Playwright : {e}")
            # on retente avec l'ancienne session plutôt que de tout perdre
            return _cache["cookies"], _cache["csrf"]
        _cache["cookies"] = cookies
        _cache["csrf"] = csrf
        _cache["expires_at"] = now + VINTED_SESSION_TTL_SECONDS
        return cookies, csrf


def _appel_api(url, params, extra_headers=None):
    """GET générique vers l'API Vinted avec la session en cache, et un seul
    retry après rafraîchissement de session en cas de 401/403."""
    cookies_dict, csrf_token = get_session()
    if not cookies_dict:
        return None

    def _do(cookies, csrf):
        headers = dict(BASE_HEADERS)
        headers["x-csrf-token"] = csrf
        if cookies.get("anon_id"):
            headers["x-anon-id"] = cookies["anon_id"]
        if extra_headers:
            headers.update(extra_headers)
        return requests.get(url, headers=headers, params=params, cookies=cookies, timeout=10)

    try:
        response = _do(cookies_dict, csrf_token)
        if response.status_code in (401, 403):
            cookies_dict, csrf_token = get_session(force_refresh=True)
            if cookies_dict:
                response = _do(cookies_dict, csrf_token)
        return response
    except Exception as e:
        logger.error(f"Erreur requête API Vinted ({url}) : {e}")
        return None


def _item_depuis_json(item):
    titre = item.get("title", "Article Vinted")

    prix_brut = item.get("price")
    if isinstance(prix_brut, dict):
        prix_brut = prix_brut.get("amount")
    prix = f"{prix_brut} €" if prix_brut not in (None, "") else "N/A"

    lien = item.get("url") or ""
    if not lien and item.get("id"):
        lien = f"https://www.vinted.fr/items/{item['id']}"
    if lien.startswith("/"):
        lien = "https://www.vinted.fr" + lien
    lien = lien or "#"

    # Recherche : champ "photo" (dict). Garde-robe vendeur : tableau "photos"
    # (structure observée en réel). On ne prend JAMAIS user.photo (avatar).
    photo = item.get("photo") or {}
    if not photo and item.get("photos"):
        photo = item["photos"][0] or {}
    img_url = ""
    if isinstance(photo, dict):
        img_url = photo.get("url") or photo.get("full_size_url") or ""
        if not img_url and photo.get("thumbnails"):
            img_url = (photo["thumbnails"][0] or {}).get("url", "")
    return {"titre": titre, "prix": prix, "lien": lien, "image": img_url}


def _valeur_attribut(ids):
    """Valeur envoyée à Vinted pour un filtre à choix multiple.
    Les recherches stockent les IDs séparés par des virgules ("209,210"). On les
    transmet tels quels (format `a,b` courant sur l'API Vinted). Si Vinted attend
    plutôt un paramètre répété, c'est le seul endroit à changer :
        return ids.split(",")   # requests enverra attribute_ids[size]=209&attribute_ids[size]=210
    """
    return ids


def chercher_vinted(query="nike", min_price=None, max_price=None, size=None, status=None, limit_count=20):
    items_trouves = []
    api_url = "https://api.vinted.fr/svc-catalogue/items"
    params = {"search_text": query, "order": "relevance", "per_page": min(limit_count, 96)}
    if min_price:
        params["price_from"] = min_price
    if max_price:
        params["price_to"] = max_price
    if size:
        params["attribute_ids[size]"] = _valeur_attribut(size)
    if status:
        params["attribute_ids[status]"] = _valeur_attribut(status)

    response = _appel_api(api_url, params)
    if response is None:
        return []

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            logger.error(f"Réponse Vinted non-JSON pour la requête '{query}'")
            return []
        for item in data.get("items", []):
            if len(items_trouves) >= limit_count:
                break
            items_trouves.append(_item_depuis_json(item))
    else:
        logger.warning(f"Vinted a répondu {response.status_code} pour la requête '{query}'")

    return items_trouves


_JS_WARDROBE = """async ([sid, perPage, order]) => {
  const h = {
    accept: 'application/json, text/plain, */*',
    locale: 'fr-FR',
    'x-anon-id': document.cookie.match(/anon_id=([^;]+)/)?.[1] || '',
    'x-csrf-token': document.querySelector('meta[name="csrf-token"]')?.content || ''
  };
  const r = await fetch(`/api/v2/wardrobe/${sid}/items?page=1&per_page=${perPage}&order=${order}`, { headers: h });
  if (!r.ok) return { __status: r.status };
  return await r.json();
}"""


def _wardrobe_via_playwright(seller_id, per_page, order):
    """Repli : exécute le fetch DANS une vraie page vinted.fr (mêmes cookies,
    empreinte navigateur, en-têtes du site), ce qui passe l'anti-bot quand
    requests prend un 403. Plus lent (lance un navigateur)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
            page = context.new_page()
            page.goto("https://www.vinted.fr/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)  # laisse le temps au cookie anon_id d'être posé
            return page.evaluate(_JS_WARDROBE, [str(seller_id), per_page, order])
        finally:
            browser.close()


def chercher_items_vendeur(seller_id, limit_count=20):
    """Dernières annonces d'un vendeur : GET /api/v2/wardrobe/<id>/items.
    Validé en réel depuis le navigateur : il FAUT les en-têtes x-csrf-token,
    x-anon-id et locale (sinon 403 HTML anti-bot) ; order=newest_first est
    accepté ; le prix est {"amount": "1079.0", "currency_code": "EUR"}."""
    per_page = min(limit_count, 96)
    order = "newest_first"
    data = None

    response = _appel_api(
        f"https://www.vinted.fr/api/v2/wardrobe/{seller_id}/items",
        {"page": 1, "per_page": per_page, "order": order},
        extra_headers={"locale": "fr-FR"},
    )
    if response is not None and response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            logger.warning(f"Réponse non-JSON (requests) pour le vendeur '{seller_id}'")
    else:
        code = response.status_code if response is not None else "aucune réponse"
        logger.info(f"Vendeur '{seller_id}' : {code} via requests, repli sur Playwright")

    if data is None:
        try:
            data = _wardrobe_via_playwright(seller_id, per_page, order)
        except Exception as e:
            logger.error(f"Erreur Playwright (vendeur '{seller_id}') : {e}")
            return []
        if not isinstance(data, dict) or "__status" in data:
            statut = data.get("__status") if isinstance(data, dict) else "?"
            logger.warning(f"Vendeur '{seller_id}' : Playwright a aussi échoué (statut {statut})")
            return []

    # Champs observés en réel : on ignore les annonces vendues, réservées,
    # masquées ou en brouillon (inutile de t'alerter sur un article non achetable).
    items = [
        it for it in data.get("items", [])
        if not (it.get("is_closed") or it.get("is_reserved") or it.get("is_hidden") or it.get("is_draft"))
    ]
    return [_item_depuis_json(it) for it in items[:limit_count]]


def resoudre_vendeur(identifiant):
    """⚠️ NON TESTÉ EN CONDITIONS RÉELLES. Essaie de retrouver l'ID numérique
    d'un vendeur à partir d'un pseudo ou d'une URL de profil
    (ex: 'https://www.vinted.fr/member/12345678-pseudo' ou juste 'pseudo').
    Retourne (seller_id, label) ou (None, None) si non trouvé."""
    identifiant = identifiant.strip()

    # Cas simple : l'URL contient déjà l'ID numérique (format Vinted standard
    # /member/<id>-<pseudo>).
    match = re.search(r"/member/(\d+)", identifiant)
    if match:
        return match.group(1), identifiant

    # Sinon on tente de résoudre via une recherche Vinted sur le pseudo et on
    # regarde le vendeur des résultats — best-effort, à valider en vrai.
    cookies_dict, csrf_token = get_session()
    if not cookies_dict:
        return None, None
    try:
        headers = dict(BASE_HEADERS)
        headers["x-csrf-token"] = csrf_token
        resp = requests.get(
            "https://api.vinted.fr/svc-catalogue/items",
            headers=headers, cookies=cookies_dict, timeout=10,
            params={"search_text": identifiant, "per_page": 20},
        )
        if resp.status_code == 200:
            for item in resp.json().get("items", []):
                user = item.get("user", {}) or {}
                login = (user.get("login") or "").lower()
                if login == identifiant.lower().lstrip("@"):
                    return str(user.get("id")), user.get("login")
    except Exception as e:
        logger.error(f"Erreur résolution vendeur '{identifiant}' : {e}")

    return None, None


def _extraire_produit_json_ld(html):
    """Cherche dans le HTML d'une page d'annonce le bloc JSON-LD schema.org
    de type Product (validé sur une vraie page Vinted : contient name,
    offers.price, offers.availability...). Retourne le dict ou None."""
    blocs = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    for bloc in blocs:
        try:
            data = json.loads(bloc)
        except ValueError:
            continue
        candidats = data if isinstance(data, list) else [data]
        for c in candidats:
            if isinstance(c, dict) and c.get("@type") == "Product":
                return c
    return None


def _produit_vers_titre_prix(produit):
    """Extrait (titre, prix_float) d'un Product JSON-LD. Retourne (None, None)
    si l'annonce n'est plus disponible (vendue/réservée) ou sans prix."""
    if not produit:
        return None, None
    offre = produit.get("offers") or {}
    if isinstance(offre, list):
        offre = offre[0] if offre else {}
    dispo = str(offre.get("availability", ""))
    if dispo and "InStock" not in dispo:
        return None, None
    try:
        prix = float(offre.get("price"))
    except (TypeError, ValueError):
        return None, None
    return produit.get("name", "Article Vinted"), prix


def _html_via_requests(item_url):
    cookies_dict, _csrf = get_session()
    if not cookies_dict:
        return None
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "fr-FR,fr;q=0.9",
        "user-agent": USER_AGENT,
    }
    try:
        resp = requests.get(item_url, headers=headers, cookies=cookies_dict, timeout=10)
        if resp.status_code == 200:
            return resp.text
        logger.info(f"Page annonce : HTTP {resp.status_code} via requests, bascule sur Playwright")
    except Exception as e:
        logger.warning(f"Page annonce via requests impossible ({e}), bascule sur Playwright")
    return None


def _html_via_playwright(item_url):
    """Plus lent mais fiable face à Cloudflare/DataDome (vrai navigateur)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
            page = context.new_page()
            page.goto(item_url, wait_until="domcontentloaded", timeout=25000)
            return page.content()
        finally:
            browser.close()


def obtenir_prix_item(item_url):
    """Récupère titre + prix actuel d'une annonce (pour la wishlist) en
    lisant le JSON-LD Product embarqué dans la page (format validé sur une
    vraie annonce). Essaie d'abord requests (rapide), puis Playwright si la
    protection anti-bot bloque ou si le JSON-LD est absent.
    Retourne (titre, prix_float) ou (None, None) si introuvable/vendu."""
    if not re.search(r"/items/\d+", item_url):
        return None, None
    url_propre = item_url.split("?")[0]

    html = _html_via_requests(url_propre)
    produit = _extraire_produit_json_ld(html) if html else None

    if produit is None:
        try:
            html = _html_via_playwright(url_propre)
            produit = _extraire_produit_json_ld(html)
        except Exception as e:
            logger.error(f"Erreur Playwright sur l'annonce {url_propre} : {e}")
            return None, None

    return _produit_vers_titre_prix(produit)
