# Journal nutrition

Journal alimentaire mobile-first : FastAPI + interface web, conçu autour d'une
seule contrainte — **enregistrer un repas en moins de 20 secondes, depuis le
téléphone, à une main.** Si on rate ça, le journal est abandonné en deux
semaines et tout le reste ne sert à rien.

## Fonctionnalités

- **Recherche alimentaire** : CIQUAL (ANSES, base locale) pour les aliments
  bruts + Open Food Facts (scan code-barres, zxing.js) pour les emballés.
- **Jauges calories / protéines** contre des cibles du profil (rechargées à
  chaque requête — corriger une cible n'impose jamais de redémarrer).
- **Solde du jour** apport − dépense Garmin, marqué « en cours » tant que la
  journée n'est pas consolidée.
- **Onglet Tendances** : moyennes, qualité de saisie, calibration du TDEE
  (perte réelle observée vs perte prédite), projection de l'objectif poids.
- **Coach IA** (optionnel) : 3-5 insights/jour générés par LLM avec un garde-fou
  strict — *le modèle n'a pas le droit de produire un nombre*. Il reçoit des
  faits calculés et ne fait que les formuler ; tout chiffre absent de son
  entrée est vérifié après coup et l'insight est jeté.
- **Alertes 18h / 21h30** : à 18h il reste un dîner (calories + protéines),
  à 21h30 c'est trop tard pour manger mais pas pour un shaker (protéines seules).

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
jamais remplacer un résultat valable.

## Architecture

```
app.py                  # API FastAPI + UI (un seul fichier)
stats.py                # LE SEUL endroit où les faits sont calculés —
                        # importé par l'API ET le script d'insights :
                        # l'écran et le coach parlent des mêmes nombres.
index.html / static/    # interface mobile, une main
scripts/
  garmin-depense-cache.py   # dépense du jour → fichier (cron 30 min)
  nutrition-insights.py     # coach IA quotidien (cron 6h20 + rattrapage 12h20)
  nutrition-alerte.py       # alertes 18h / 21h30
  ciqual-load.py            # construit ciqual.db depuis les XML ANSES
config/
  nutrition-profil.example.json   # cibles — à copier et adapter
```

Les chemins de données sont relatifs au dossier du projet (définis en tête de
`app.py`) : DB du journal, cache dépense, profil, insights.

## Données personnelles

Tout ce qui est personnel (journal, pesées, profil réel, insights) vit
localement et est dans le `.gitignore`. Ce repo ne contient que du code.

## Sources

- [CIQUAL — ANSES](https://ciqual.anses.fr/) (licence libre avec mention de source)
- [Open Food Facts](https://world.openfoodfacts.org/)
- [GarminDB](https://github.com/tcgoetz/GarminDB)
