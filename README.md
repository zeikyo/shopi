# Discord Shop Monitor

Bot Discord Python pour surveiller des URLs de boutiques, collections ou produits et notifier:

- nouveaux produits
- retours en stock
- ruptures de stock
- changements de prix
- ping optionnel d'un role Discord sur les alertes

Le bot ne fait pas de checkout automatique, ne contourne aucune protection et n'utilise pas de proxy par defaut.

## Installation

Prerequis: Python 3.11 ou plus.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Creer le bot Discord

1. Va sur le portail developpeur Discord: <https://discord.com/developers/applications>
2. Cree une application, puis un bot.
3. Copie le token du bot.
4. Dans `OAuth2 > URL Generator`, coche:
   - `bot`
   - `applications.commands`
5. Dans les permissions bot, ajoute au minimum:
   - `Send Messages`
   - `Embed Links`
   - `Read Message History`
6. Invite le bot sur ton serveur avec l'URL generee.

## Intents

Le bot utilise uniquement les slash commands et n'a pas besoin de `Message Content Intent`.

Dans le portail Discord, les intents privilegies peuvent rester desactives pour ce projet.

## Configuration

Copie le fichier d'exemple:

```powershell
Copy-Item .env.example .env
```

Puis remplis:

```env
DISCORD_BOT_TOKEN=ton_token_discord
DEFAULT_INTERVAL=15
MAX_WATCHES_PER_GUILD=20
DEBUG_MODE=false
```

## Lancement

```powershell
python main.py
```

Au premier lancement, le bot cree automatiquement `data.db` avec les tables `watches` et `products`.
Les migrations non destructives ajoutent aussi les champs utiles comme `ping_role_id`, `last_change_type` et `last_alerted_at`.

## Commandes

Seules les personnes avec la permission Administrateur peuvent utiliser les commandes.

```text
/add_url url salon intervalle
/remove_url url
/list_urls
/set_channel url salon
/set_interval url secondes
/set_ping_role url role
/pause url
/resume url
/test_alert url
/stats
/ping
/set_logs_channel salon
/dashboard salon
```

Exemple:

```text
/add_url https://lmdx.fr/collections/lmdx-etb #annonces 15
```

Avec ping role:

```text
/add_url https://lmdx.fr/collections/lmdx-etb #annonces 15 @Drops
/set_ping_role https://lmdx.fr/collections/lmdx-etb @Drops
```

Quand une URL est ajoutee, le bot fait un premier scan et sauvegarde les produits trouves sans envoyer d'alertes massives. Les alertes commencent aux scans suivants.

Avant d'envoyer une alerte, le bot refait un second scan quelques secondes apres le premier signal. Cela evite une bonne partie des faux positifs lies aux variations temporaires de stock.

Le bot garde aussi le dernier type d'alerte envoye par produit afin d'eviter les doublons pendant le cooldown.

Apres 5 erreurs consecutives sur une URL, la watch est auto-pausee. La commande `/resume` remet le compteur d'erreurs a zero.

## Intervalles

Le minimum conseille est 10 secondes. Pour une collection classique, 15 a 60 secondes est souvent suffisant.

Evite les intervalles trop courts sur beaucoup d'URLs: cela peut charger inutilement les sites surveilles et augmenter les erreurs HTTP comme 429.

## Scraping

Pour Shopify, le bot essaye d'abord:

- `/collections/.../products.json`
- `/products.json`

S'il ne trouve rien, il bascule sur un fallback HTML avec BeautifulSoup.

La fonction principale a adapter pour ajouter un site specifique est `get_products(url)` dans `scraper.py`.
Le scraping Shopify est separe en fonctions simples:

- `fetch_shopify_json()`
- `parse_products()`
- fallback HTML via BeautifulSoup

Le bot reutilise un client HTTP async global avec connexions keep-alive. Les scans ont un jitter aleatoire de +/- 2 secondes pour eviter un rythme trop fixe.

## Logs et monitoring

Les logs sont separes:

- `bot.log`: logs globaux
- `monitor.log`: scans, timings, produits detectes, requetes HTTP
- `error.log`: warnings et erreurs

Commandes utiles:

- `/stats`: watches actives, produits suivis, alertes envoyees, uptime, scans et requetes HTTP
- `/ping`: latence Discord et temps moyen d'un scan
- `/set_logs_channel`: configure le salon Discord qui recevra les logs importants
- `/dashboard`: envoie un dashboard interactif persistant avec boutons et menus

## Dashboard Discord

Commande:

```text
/dashboard #salon
```

Le dashboard affiche l'etat global du monitor et propose des boutons:

- Ajouter URL
- Liste URLs
- Pause
- Resume
- Supprimer URL
- Parametres
- Stats
- Refresh

Les boutons utilisent des `custom_id` fixes et la View est enregistree au demarrage, donc le dashboard reste utilisable apres redemarrage du bot. Seuls les administrateurs peuvent l'utiliser.

## Logs Discord

Le bot peut envoyer des logs importants dans un salon dedie:

```text
/set_logs_channel #logs-bot
```

Logs envoyes:

- demarrage bot
- watch ajoutee
- watch supprimee
- watch pausee ou reprise
- auto-pause apres trop d'erreurs
- erreur critique sur une watch

Les scans normaux et les requetes HTTP ne sont pas envoyes dans Discord. Si `DEBUG_MODE=true`, quelques logs supplementaires utiles au diagnostic peuvent etre envoyes sans logguer chaque scan normal.

## Proxies

Pas besoin de proxy au debut. Le bot utilise un User-Agent realiste, un timeout reseau, un retry leger et un backoff progressif en cas d'erreurs 403, 429 ou 5xx.
