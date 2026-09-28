# Migration vers la version corrigée

## 1. Installer les dépendances
```
pip install -r requirements.txt
playwright install chromium
```

## 2. Configurer tes tokens
Copie `.env.example` en `.env` dans le même dossier, puis remplis tes vrais
tokens Telegram (central, sender, dev) et ton chat_id personnel :
```
copy .env.example .env
```
Ouvre ensuite `.env` avec un éditeur et remplace les valeurs d'exemple.

## 3. Régénérer tes tokens Telegram (important)
Comme tes anciens tokens étaient en clair dans le code, considère-les comme
compromis. Sur chaque bot via **@BotFather** : `/mybots` → choisis le bot →
`API Token` → `Revoke current token`, puis colle le nouveau token dans `.env`.

## 4. Base de données existante
`vinted_bot.db` sera automatiquement mise à jour au démarrage (les nouvelles
tables/colonnes sont ajoutées si elles n'existent pas), tu n'as rien à faire
de spécial. Si tu veux repartir propre, tu peux aussi supprimer le fichier
`vinted_bot.db` (tu perdras l'historique des utilisateurs et recherches).

## 5. Fichiers à placer ensemble dans le même dossier
```
config.py
db.py
telegram_utils.py
token_manager.py
vinted_scraper.py
bot_centrale.py
bot_sender.py
logger_config.py
run_all.py / start.bat   (lanceur des deux bots)
requirements.txt
.env            (créé par toi à l'étape 2, jamais commité sur Git)
tokens.txt      (ton fichier existant, inchangé)
```

## 6. Lancer les bots

**Option A — un seul terminal (recommandé en local)**
```
python run_all.py
```
ou double-clic sur `start.bat` sous Windows. Les deux bots démarrent ensemble,
un bot qui plante est relancé automatiquement, `Ctrl+C` arrête tout.

**Option B — deux terminaux séparés** (comme avant)
```
python bot_centrale.py
python bot_sender.py
```

**Option C — Docker** (pour un serveur/VPS) : ⚠️ non testé, pas de Docker dans l'environnement de développement.
```
docker compose up -d --build
docker compose logs -f
```
Prérequis : Docker + `.env` rempli. La base vit dans un volume Docker (`vinted_data`),
donc elle démarre vide : pour reprendre ton `vinted_bot.db` existant, lance d'abord
`docker compose up -d`, puis `docker cp vinted_bot.db <nom_du_conteneur_sender>:/data/vinted_bot.db`
(nom visible avec `docker ps`) et `docker compose restart`.

## Ce qui a changé
- **Schéma DB unifié** entre les deux bots (corrige le bug potentiel `no such column`).
- **Tokens Premium à usage unique** : un token ne peut plus être réutilisé par plusieurs comptes.
- **Tokens Telegram** sortis du code, chargés depuis `.env`.
- **Session Vinted mise en cache** (5 min) : le bot ne relance plus un navigateur à chaque recherche.
- **Veille parallélisée** (jusqu'à 3 recherches en même temps) pour rester réactif avec plusieurs utilisateurs.
- **Erreurs Telegram gérées** : retry sur rate-limit, découpage des messages > 4096 caractères, log des échecs.
- **Auto-pause** des recherches d'un utilisateur injoignable (bot bloqué) après 5 échecs consécutifs.
- **Étape de confirmation** ("✅ Confirmer / 🔄 Recommencer") ajoutée avant l'enregistrement d'une recherche.
- **`/cancel`** ajouté au menu de commandes visible.
- Mode SQLite **WAL** activé pour éviter les erreurs `database is locked` entre les deux bots.

## Nouveautés de cette mise à jour

### ⚙️ Backend
- **Logs propres** : les `print()` sont remplacés par de vrais logs (module `logging`), écrits dans la console ET dans un fichier avec rotation (`vintedpulse.log` par défaut, configurable via `LOG_FILE`).
- **Purge automatique de la DB** : `seen_items` et `item_prices` de plus de `DATA_RETENTION_DAYS` jours (30 par défaut) sont supprimés une fois par jour automatiquement.
- **Heartbeat / monitoring externe** : si tu renseignes `HEALTHCHECK_URL` (ex: un ping [healthchecks.io](https://healthchecks.io)) dans `.env`, le bot sender ping cette URL à chaque cycle de veille — tu es alerté par mail si le bot plante.

### 🎨 UI / UX
- **Clavier persistant** : les commandes principales sont toujours visibles en bas de l'écran Telegram sur le bot sender.
- **Aperçu immédiat** : juste après la création d'une recherche, le bot lance un premier scan et montre jusqu'à 3 résultats en exemple.
- **Regroupement des notifications** : si plusieurs articles arrivent d'un coup, ils sont envoyés dans un seul message au lieu d'un message par article.

### ✨ Nouvelles fonctionnalités
- **Mots-clés à exclure** : demandés à la création de chaque recherche (`/newsearch`).
- **Veille par vendeur** (`/trackseller <lien du profil>`, `/sellers`) : endpoint `/api/v2/wardrobe/<id>/items` validé depuis le navigateur (en-têtes `x-csrf-token`, `x-anon-id`, `locale` obligatoires, sinon 403 anti-bot ; `order=newest_first` accepté). Si `requests` prend un 403, l'appel est refait dans un vrai navigateur Playwright (plus lent). À l'ajout d'un vendeur, ses annonces existantes sont marquées comme vues (pas de flood au premier cycle). Structure d'un article observée en réel (`url`, tableau `photos`, `is_closed`/`is_reserved`/`is_hidden`/`is_draft`) : les annonces vendues, réservées ou masquées sont ignorées. Reste à voir en réel : le format exact d'un élément de `photos` (plusieurs champs de secours prévus).
- **Détection de vraies bonnes affaires** : compare le prix de chaque nouvel article à la moyenne des prix vus pour cette recherche (badge 🔥), au lieu de juste comparer à l'ancien prix du même article.
- **Plage horaire de veille** : par défaut, le bot ne scanne qu'entre 8h et 23h (heure du serveur). Modifiable à chaud avec la commande dev **`/plage`** (`/plage` pour voir, `/plage 8 23` pour définir, `/plage 22 6` pour une plage à cheval sur minuit, `/plage off` pour 24h/24) : prise en compte au prochain cycle, sans redémarrer. `MONITOR_HOUR_START` / `MONITOR_HOUR_END` dans `.env` servent de valeurs par défaut tant que la commande n'a pas été utilisée. L'heure est celle de la machine qui fait tourner le bot (sous Docker, c'est l'UTC par défaut).
- **Parrainage** (`/parrain`) : lien d'invitation qui offre `REFERRAL_BONUS_DAYS` jours de Premium (3 par défaut) au parrain ET au filleul.
- **Wishlist** (`/watch <lien>`, `/wishlist`) : le prix est lu dans le bloc JSON-LD `Product` de la page d'annonce (format validé sur une vraie annonce ; parsing testé hors-ligne). Le chargement de la page passe par `requests` puis Playwright en secours (Cloudflare/DataDome) : à tester une fois en réel avec `/watch`.
- **Commande dev `/addpremium <chat_id> [jours]`** sur le bot Dev : offre le Premium à n'importe quel compte, avec ou sans durée limitée (`/removepremium <chat_id>` pour le retirer).
- Multi-plateformes (Leboncoin, Depop...) **n'a volontairement pas été implémenté** dans cette mise à jour — à prévoir pour une prochaine fois.

### Nouvelles variables d'environnement (toutes optionnelles, voir `.env.example`)
`SENDER_BOT_USERNAME`, `HEALTHCHECK_URL`, `LOG_FILE`, `DATA_RETENTION_DAYS`, `MONITOR_HOUR_START`, `MONITOR_HOUR_END`, `REAL_DEAL_THRESHOLD`, `REAL_DEAL_MIN_SAMPLES`, `REFERRAL_BONUS_DAYS`.

### Base de données
Les tables et colonnes manquantes sont ajoutées automatiquement au démarrage (`init_db()`, liste déclarative `COLONNES_ATTENDUES` dans `db.py`) : rien à faire manuellement, y compris sur une base créée par une très ancienne version. Les lignes historiques de `seen_items`/`item_prices` sont datées du jour de la migration et ne sont donc purgées qu'après `DATA_RETENTION_DAYS` jours.

### /newsearch : retours utilisateurs (sélection multiple + bouton Retour)
- **Tailles et états multiples** : boutons à cocher (✅) puis « Valider ». Aucun état coché = tous les états. Les IDs sont stockés séparés par une virgule (`209,210`) dans les colonnes `size` / `status` existantes : pas de migration, les anciennes recherches restent valides.
- **⬅️ Retour** sous chaque étape (sauf la 1re) : ré-affiche l'étape précédente en conservant les choix déjà faits. Les anciens boutons d'un message quitté sont désactivés.
- `/list`, le récap et la confirmation affichent maintenant aussi les états choisis.
- ⚠️ À valider en réel : Vinted doit accepter `attribute_ids[size]=209,210`. Sinon, changer `_valeur_attribut()` dans `vinted_scraper.py` (voir le commentaire).
