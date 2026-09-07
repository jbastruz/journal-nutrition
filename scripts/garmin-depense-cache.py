#!/usr/bin/env python3
"""garmin-depense-cache.py — Dépose la dépense du JOUR COURANT dans un fichier.

    .venv-garmin/bin/python scripts/garmin-depense-cache.py

Pourquoi ce détour au lieu d'appeler l'API depuis l'application
---------------------------------------------------------------
Le journal alimentaire veut afficher le solde du jour, donc la dépense du jour.
Or le miroir GarminDB **n'a jamais la journée en cours** : sa plage s'arrête la
veille (voir `garmin-night.py`). Seule l'API le sait.

Mais brancher l'API sur `/api/jour` reviendrait à taper une API non officielle à
chaque ouverture du journal, depuis un téléphone, plusieurs fois par repas. Deux
raisons de ne pas le faire :

1. **Le 429 du 2026-08-29.** Un enchaînement de logins a fait limiter le compte.
   Ce n'est pas théorique.
2. **Le journal doit rester instantané et fonctionner hors ligne.** Sa contrainte
   de conception est « enregistrer un repas en moins de 20 s ». Un appel réseau
   qui met 4 s, ou qui pend, la casse — pour une information secondaire.

D'où ce script : un seul appel toutes les 30 minutes, écrit sur disque,
l'application ne fait plus que lire un fichier local.

Ce que le fichier contient et pourquoi il porte son horodatage
---------------------------------------------------------------
`jour` et `mesure_a` sont là pour que le lecteur puisse REFUSER la valeur. Un
cache qui ne dit pas quand il a été écrit finit par afficher la dépense d'hier
comme celle d'aujourd'hui — et personne ne le remarque, parce qu'un nombre
plausible ne déclenche rien. L'application vérifie les deux avant d'afficher.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent
CACHE = AGENT / "nutrition" / ".depense-jour.json"


def main() -> int:
    jour = dt.date.today().isoformat()
    try:
        import garminconnect
    except ImportError:
        print("garminconnect absent — lancer avec .venv-garmin/bin/python",
              file=sys.stderr)
        return 2

    try:
        cfg = json.loads((Path.home() / ".GarminDb" / "GarminConnectConfig.json")
                         .read_text(encoding="utf-8"))
        pwd = (Path.home() / ".GarminDb" / ".garmin_password").read_text().strip()
        api = garminconnect.Garmin(cfg["credentials"]["user"], pwd)
        api.login(str(Path.home() / ".garminconnect"))   # jeton réutilisé, pas de SSO
        s = api.get_stats(jour) or {}
    except Exception as e:
        # Échec silencieux : le cache précédent reste en place et vieillit, et
        # c'est l'application qui décidera qu'il est périmé. Écraser le fichier
        # avec une erreur ferait perdre une valeur encore valable.
        print(f"API Garmin injoignable ({type(e).__name__}) — cache inchangé",
              file=sys.stderr)
        return 1

    if s.get("totalKilocalories") is None:
        print("aucune dépense renvoyée pour aujourd'hui — cache inchangé",
              file=sys.stderr)
        return 1

    CACHE.write_text(json.dumps({
        "jour": jour,
        "mesure_a": dt.datetime.now().isoformat(timespec="seconds"),
        "total": s["totalKilocalories"],
        # ⚠️ `bmrKilocalories` porte en réalité un RMR (Mifflin / 0,82). Le nom
        # du champ d'API est trompeur et m'avait lancé sur une fausse piste de
        # double comptage le 2026-09-02. On garde le nom « repos », pas « bmr ».
        "repos": s.get("bmrKilocalories"),
        "actif": s.get("activeKilocalories"),
        "pas": s.get("totalSteps"),
    }, ensure_ascii=False), encoding="utf-8")
    print(f"{jour} — {s['totalKilocalories']:.0f} kcal "
          f"(repos {s.get('bmrKilocalories') or 0:.0f} + actif "
          f"{s.get('activeKilocalories') or 0:.0f}), {s.get('totalSteps') or 0} pas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
