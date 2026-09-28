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
