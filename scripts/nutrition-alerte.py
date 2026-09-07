#!/usr/bin/env python3
"""nutrition-alerte.py — Prévient JB en cours de journée s'il décroche de ses cibles.

    python3 scripts/nutrition-alerte.py            # décide selon l'heure
    python3 scripts/nutrition-alerte.py --creneau 18
    python3 scripts/nutrition-alerte.py --test     # affiche sans notifier

Pourquoi ce script existe
-------------------------
Le 2026-09-05, JB était à **466 kcal et 9,8 g de protéines à 16h35**, un jour de
Pure Cardio, pour une cible de 2480 / 174. Rien ne le lui a dit. Je l'ai vu par
hasard, parce qu'il m'a parlé de son goûter pour une autre raison. L'application
tenait le chiffre juste et ne le confrontait à rien.

Un journal qui enregistre sans jamais alerter documente le problème au lieu de
l'éviter.

Deux créneaux, deux usages différents
-------------------------------------
**18h00** — il reste un dîner pour rattraper. C'est le créneau utile : à cette
heure-là, une alerte change ce qu'il met dans son assiette le soir.

**21h30** — dernière fenêtre. Trop tard pour un repas, mais **pas** pour les
protéines : un shaker se boit en trois minutes. Ce créneau ne parle donc QUE
des protéines, parce que c'est la seule chose encore rattrapable.

Les seuils ne sont pas des pourcentages ronds choisis au hasard : ils
correspondent à ce qu'il reste faisable après. À 18h, il faut avoir mangé la
moitié des calories pour que le dîner suffise à finir la journée sans être
énorme. Les protéines sont plus exigeantes (40 % à 18h) parce qu'elles se
rattrapent mal en un seul repas.

Trois garde-fous, tous appris ailleurs dans ce dépôt
----------------------------------------------------
1. **Une alerte par créneau et par jour.** État sur disque. Sans ça, un cron
   toutes les 20 min répéterait la même chose et on apprendrait à l'ignorer.
2. **Silence si l'app n'est plus utilisée.** Journal vide aujourd'hui ET aucune
   saisie depuis 3 jours = il a arrêté de saisir, pas arrêté de manger. Le
   harceler ne le fera pas revenir. Même logique que `memory-healthcheck.sh`,
   qui compare la mémoire à l'activité réelle et jamais au calendrier.
3. **Le message dit d'où il tient son chiffre.** « d'après ton journal » — une
   saisie faite plus tard ne sera pas dedans, et l'alerte ne doit pas affirmer
   ce qu'elle ne peut pas savoir.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent
DB = AGENT / "nutrition" / "nutrition.db"
PROFIL = AGENT / "config" / "nutrition-profil.json"
ETAT = AGENT / ".nutrition-alerte-state.json"

# créneau → (seuil kcal, seuil protéines, protéines seulement ?)
# None = ce créneau ne regarde pas ce nutriment.
CRENEAUX = {
    18: (0.50, 0.40, False),
    21: (None, 0.70, True),
}
JOURS_AVANT_ABANDON = 3


def profil() -> dict | None:
    """Cibles du jour. Même source que l'app et que nutrition-bilan.py."""
    try:
        p = json.loads(PROFIL.read_text(encoding="utf-8"))
        kcal, poids, gkg = (p.get("apport_cible_kcal"), p.get("poids_kg"),
                            p.get("proteines_g_par_kg"))
        if not kcal or not poids or not gkg:
            return None
        return {"kcal": round(kcal), "proteines": round(poids * gkg)}
    except (OSError, ValueError, TypeError):
        return None


def journal(jour: str) -> tuple[float, float, int]:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        r = c.execute("select coalesce(sum(energie_kcal),0), "
                      "coalesce(sum(proteines),0), count(*) "
                      "from journal where jour=?", (jour,)).fetchone()
    return r[0], r[1], r[2]


def derniere_saisie(avant: str) -> str | None:
    """Dernier jour saisi À LA DATE DEMANDÉE OU AVANT.

    Le `avant` n'est pas décoratif : une première version prenait le `max(jour)`
    de toute la base, ce qui donnait un écart NÉGATIF dès qu'on évaluait un jour
    passé — et le garde-fou « app abandonnée » ne se déclenchait jamais. Repéré
    en testant sur le 2026-08-01, où le script proposait d'alerter sur une
    journée vide vieille de cinq semaines.
    """
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        return c.execute("select max(jour) from journal where jour <= ?",
                         (avant,)).fetchone()[0]


def deja_alerte(jour: str, creneau: int) -> bool:
    try:
        e = json.loads(ETAT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return e.get("jour") == jour and creneau in e.get("creneaux", [])


def marque(jour: str, creneau: int) -> None:
    try:
        e = json.loads(ETAT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        e = {}
    if e.get("jour") != jour:
        e = {"jour": jour, "creneaux": []}
    e["creneaux"] = sorted(set(e["creneaux"] + [creneau]))
    ETAT.write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creneau", type=int, help="forcer un créneau (18 ou 21)")
    ap.add_argument("--jour", help="forcer une date (test)")
    ap.add_argument("--test", action="store_true",
                    help="afficher sans notifier ni marquer l'état")
    a = ap.parse_args()

    maintenant = dt.datetime.now()
    jour = a.jour or maintenant.date().isoformat()

    creneau = a.creneau
    if creneau is None:
        # Le cron passe à heure fixe ; on tolère un décalage d'exécution en
        # rattachant l'heure courante au créneau ouvert le plus récent.
        ouverts = [h for h in CRENEAUX if maintenant.hour >= h]
        if not ouverts:
            print("hors créneau")
            return 0
        creneau = max(ouverts)
    if creneau not in CRENEAUX:
        print(f"créneau inconnu : {creneau}", file=sys.stderr)
        return 2

    if not a.test and deja_alerte(jour, creneau):
        print(f"déjà alerté pour {jour} au créneau {creneau}h")
        return 0

    cibles = profil()
    if not cibles:
        print("pas de cibles lisibles dans config/nutrition-profil.json")
        return 0

    kcal, prot, n = journal(jour)

    # Garde-fou « app abandonnée » : ne pas confondre « il ne mange pas » et
    # « il ne saisit plus ». Sans ça, le script alerterait tous les soirs d'une
    # semaine de vacances.
    if n == 0:
        derniere = derniere_saisie(jour)
        if derniere is None:
            print("aucune saisie à cette date ni avant — silence")
            return 0
        ecart = (dt.date.fromisoformat(jour) - dt.date.fromisoformat(derniere)).days
        if ecart > JOURS_AVANT_ABANDON:
            print(f"aucune saisie depuis {ecart} j — app abandonnée, silence")
            return 0

    s_kcal, s_prot, prot_seul = CRENEAUX[creneau]
    manques = []
    if s_kcal is not None and kcal < cibles["kcal"] * s_kcal:
        manques.append("kcal")
    if s_prot is not None and prot < cibles["proteines"] * s_prot:
        manques.append("proteines")

    r_kcal = kcal / cibles["kcal"] * 100
    r_prot = prot / cibles["proteines"] * 100
    print(f"{jour} {creneau}h — {kcal:.0f}/{cibles['kcal']} kcal ({r_kcal:.0f} %), "
          f"{prot:.1f}/{cibles['proteines']} g P ({r_prot:.0f} %) → "
          f"{'ALERTE ' + '+'.join(manques) if manques else 'rien à signaler'}")

    if not manques:
        return 0

    reste_k = cibles["kcal"] - kcal
    reste_p = cibles["proteines"] - prot
    if prot_seul:
        msg = (f"Journal alimentaire — dernière fenêtre. Tu es à {prot:.0f} g de "
               f"protéines sur {cibles['proteines']} ({r_prot:.0f} %), d'après ton "
               f"journal. Il t'en manque {reste_p:.0f} g. Trop tard pour un repas, "
               f"mais pas pour un shaker. Relaie-lui en une phrase, sans cérémonie, "
               f"et propose-lui de rattraper.")
    else:
        msg = (f"Journal alimentaire — il est {creneau}h et, d'après son journal, JB "
               f"est à {kcal:.0f} kcal sur {cibles['kcal']} ({r_kcal:.0f} %) et "
               f"{prot:.0f} g de protéines sur {cibles['proteines']} ({r_prot:.0f} %). "
               f"Il lui reste {reste_k:.0f} kcal et {reste_p:.0f} g de protéines pour "
               f"la journée, et un dîner pour les placer. Préviens-le sur Discord en "
               f"une phrase et propose-lui un repas qui couvre le manque. Si une "
               f"saisie est simplement en retard, il te le dira.")

    if a.test:
        print(f"\n[TEST] message qui serait envoyé :\n  {msg}")
        return 1

    subprocess.run(["bash", str(AGENT / "scripts" / "notify-agent.sh"), msg],
                   capture_output=True, timeout=30)
    marque(jour, creneau)
    return 1


if __name__ == "__main__":
    sys.exit(main())
