# Journal nutrition

Journal alimentaire mobile-first : FastAPI + interface web monofichier, conçu
autour d'une seule contrainte — **enregistrer un repas en moins de 20 secondes,
depuis le téléphone, à une main.** Si on rate ça, le journal est abandonné en
deux semaines et tout le reste ne sert à rien.

## Fonctionnalités

- **Recherche alimentaire** : CIQUAL (ANSES, base locale) pour les aliments
  bruts, Open Food Facts pour les emballés, et des **aliments maison** saisis
  pour 100 g ou pour la portion.
- **Scan de code-barres** (zxing.js) : en HTTPS, flux caméra dans un viseur 4/3
  avec réticule (cadre blanc + trait rouge, aide au cadrage — l'image entière
  est décodée) ; en HTTP, repli sur une photo prise avec l'appareil.
- **Saisie rapide** : pastilles des aliments repris souvent, presets de
  grammage, reprise d'un repas d'un jour passé, aliments riches en protéines
  pour combler un manque.
- **Correction après coup** : toucher une ligne du journal ouvre sa fiche
  (portion, repas, libellé). Les macros sont remises à l'échelle depuis la ligne
  elle-même, jamais relues à la source — une fiche corrigée depuis ne réécrit
  pas un repas déjà mangé. Suppression annulable.
- **Routine** : un plat marqué récurrent est proposé chaque jour en grisé ; on
  valide (vraie ligne du journal) ou on rejette (rien au journal, mais la
  décision est notée pour ne pas revenir dans la seconde). Grammage de référence
  réglable, désactivation sans perte d'historique.
- **Jauges calories / protéines** contre des cibles du profil (rechargées à
  chaque requête — corriger une cible n'impose jamais de redémarrer).
- **Solde du jour** apport − dépense Garmin, marqué « en cours » tant que la
  journée n'est pas consolidée.
- **Onglet Tendances** : moyennes, qualité de saisie, calibration du TDEE
  (perte réelle observée vs perte prédite), projection de l'objectif poids, et
  **poids réel et trajectoire** avec masse maigre et masse grasse issues d'une
  balance à impédance — la marge d'erreur de l'appareil est dessinée, et aucun
  verdict n'est rendu en dessous d'elle.
- **Balance connectée** : `POST /api/pesee-balance` reçoit les pesées,
  authentifié par un jeton dans l'en-tête `X-Jeton` (jamais dans l'URL).
- **Coach IA** (optionnel) : 3-5 insights/jour générés par LLM avec un garde-fou
  strict — *le modèle n'a pas le droit de produire un nombre*. Il reçoit des
  faits calculés et ne fait que les formuler ; tout chiffre absent de son
  entrée est vérifié après coup et l'insight est jeté.
- **Alertes 18h / 21h30** : à 18h il reste un dîner (calories + protéines),
  à 21h30 c'est trop tard pour manger mais pas pour un shaker (protéines seules).
- **Connexion** par identifiant + mot de passe (scrypt), cookie de session de
  7 jours ; l'interface suit la direction artistique du logo (émeraude = acquis,
  orange = en cours ou alerte) dans un habillage « Liquid Glass » iOS 26,
  installable sur l'écran d'accueil.

## Pourquoi un cache pour la dépense Garmin

Le miroir [GarminDB](https://github.com/tcgoetz/GarminDB) n'a jamais la journée
en cours ; seule l'API officielle la connaît. Mais appeler l'API à chaque
ouverture du journal depuis un téléphone :

1. fait limiter le compte (un enchaînement de logins a valu un 429) ;
2. casse l'instantanéité — un appel réseau qui pend casse la contrainte des 20 s.

D'où `scripts/garmin-depense-cache.py` : **un** appel toutes les 30 min (cron),
écrit dans un fichier, l'app ne fait que lire. Le fichier est refusé s'il porte
une autre date ou a plus de 90 min.

## Le piège central : une journée mal saisie ressemble à un jeûne

Toute journée passée sous le métabolisme de base est marquée non complète et
sort des cumuls — sinon la perte prédite part dans le décor et donne l'illusion
d'une méthode qui marche. Le jour courant est exempté (à 16h on est forcément
sous son BMR). La liste des journées incomplètes est **déclarée par
l'utilisateur**, jamais déduite : une app qui affiche « douteux » sur une
journée réellement vécue contredit son utilisateur sur la seule chose qu'il
sait mieux qu'elle.

## Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash scripts/download_ciqual.sh          # base CIQUAL locale (~100 Mo décompressés)
cp config/nutrition-profil.example.json config/nutrition-profil.json
# éditer config/nutrition-profil.json : poids, taille, âge, cibles

python app.py                            # http://0.0.0.0:8090
```

### Connexion (obligatoire)

L'app redirige tout vers `/login` tant qu'aucune session n'est ouverte. Le
fichier lu par `AUTH_CFG` (chemin défini en tête de `app.py`) porte
l'identifiant, un secret de signature des cookies et le mot de passe haché en
scrypt :

```python
import hashlib, json, os, secrets
salt, n, r, p = os.urandom(16), 2**14, 8, 1
h = hashlib.scrypt(b"MOT-DE-PASSE", salt=salt, n=n, r=r, p=p)
json.dump({"utilisateur": "jb", "secret": secrets.token_hex(32),
           "scrypt": {"hash": h.hex(), "salt": salt.hex(), "n": n, "r": r, "p": p}},
          open("config/nutrition-auth.json", "w"), indent=1)
```

### Derrière un reverse proxy, sous un sous-chemin

`NUTRITION_PREFIX=nutrition python app.py` sert l'app à la fois à la racine
(réseau local) et sous `https://…/nutrition/` (Tailscale Funnel, qui transmet
le chemin complet). Côté client tous les chemins sont relatifs, côté serveur un
middleware retire le préfixe avant le routage ; `NUTRITION_PORT` change le port.

### Crontab (optionnel — dépense du jour, insights, alertes)

```cron
*/30 7-23 * * *  python chemin/vers/scripts/garmin-depense-cache.py
20 6 * * *       python chemin/vers/scripts/nutrition-insights.py
20 12 * * *      python chemin/vers/scripts/nutrition-insights.py --rattrapage --notifier
0 18 * * *       python chemin/vers/scripts/nutrition-alerte.py --creneau 18
30 21 * * *      python chemin/vers/scripts/nutrition-alerte.py --creneau 21
```

Le coach IA (`nutrition-insights.py`) requiert un LLM ; par défaut il appelle le
binaire `claude` (Claude Code) — surchargeable via `INSIGHTS_MODEL`. En cas
d'échec, l'app affiche l'analyse de la veille **étiquetée comme telle**, elle ne
vient jamais remplacer un résultat valable.

## Architecture

```
app.py                  # API FastAPI, page de login, middleware de sous-chemin
stats.py                # LE SEUL endroit où les faits sont calculés —
                        # importé par l'API, le script d'insights ET le plugin
                        # Hermes : tous les écrans parlent des mêmes nombres.
index.html              # interface mobile, un seul fichier (CSS + JS inline) :
                        # Journal, Tendances, Routine, feuille d'ajout, scan
static/                 # zxing.js, logo, favicons, icône d'écran d'accueil
scripts/
  garmin-depense-cache.py   # dépense du jour → fichier (cron 30 min)
  nutrition-insights.py     # coach IA quotidien (cron 6h20 + rattrapage 12h20)
  nutrition-alerte.py       # alertes 18h / 21h30
  ciqual-load.py            # construit ciqual.db depuis les XML ANSES
config/
  nutrition-profil.example.json   # cibles — à copier et adapter
```

Les chemins de données sont définis en tête de `app.py` : DB du journal, cache
dépense, profil, insights, fichier d'authentification.

### Dashboard Hermes (hors de ce dépôt)

Un plugin pour le dashboard de Hermes Agent (Nous Research) lit et écrit la
**même base** (`~/.hermes/plugins/nutrition/`) : journal du jour
avec ajout (CIQUAL, code-barres, aliments maison, scan caméra), correction d'une
ligne, tendances calculées par ce même `stats.py`, plus des outils pour l'agent
(`nutrition_bilan`, `nutrition_ajouter`, …) qui permettent de logger un repas
depuis le chat. Il suit les invariants de `POST` et `PATCH /api/journal` à la
ligne près, sur les mêmes colonnes : les deux écrans ne peuvent pas se
contredire.

## Données personnelles

Tout ce qui est personnel (journal, pesées, profil réel, authentification,
insights) vit localement et est dans le `.gitignore`. Ce repo ne contient que
du code.

## Sources

- [CIQUAL — ANSES](https://ciqual.anses.fr/) (licence libre avec mention de source)
- [Open Food Facts](https://world.openfoodfacts.org/)
- [GarminDB](https://github.com/tcgoetz/GarminDB)
- Marge d'erreur des balances à impédance : comparaison de quinze appareils
  contre ADP, DXA et spectroscopie d'impédance, reprise dans `index.html`.
