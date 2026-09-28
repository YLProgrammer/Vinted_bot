# VintedPulse

Bot Telegram d'alertes Vinted : recherches filtrées, wishlist, suivi de vendeurs, alertes de baisse de prix, parrainage et Premium.

## Lancer localement

1. Crée `.env` depuis `env.example` et renseigne les trois tokens Telegram.
2. Installe les dépendances : `pip install -r requirements.txt` puis `playwright install chromium`.
3. Lance `python run_all.py`.

## Sécurité

Ne versionne jamais `.env`, la base SQLite, les journaux ou les tokens Premium. Les tokens Premium sont créés sur le bot admin avec `/createtoken [jours] [libellé]` et sont stockés sous forme d'empreinte non réversible.

## Commandes principales

- Hub : `/start`, `/activate <token>`, `/status`
- Sender FR : `/recherche`; Sender EN : `/newsearch`
- Sender : `/list`, `/delete`, `/pause`, `/resume`, `/wishlist`, `/sellers`, `/stats`, `/deleteaccount`

## Développement et stabilité

- `bot_sender.py` contient l'interface Telegram et l'assistant de création de recherche.
- `monitor_service.py` exécute les scans, le dédoublonnage, les alertes de baisse de prix, la purge et le heartbeat dans un worker isolé. Une erreur sur une recherche est journalisée sans interrompre les autres recherches.
- La base applique ses migrations additives au démarrage et mémorise la version de schéma appliquée dans `schema_migrations`.

Avant une mise à jour, vérifie le code avec :

```bash
python -m py_compile bot_centrale.py bot_sender.py monitor_service.py db.py token_manager.py
python -m unittest discover -s tests -v
```
