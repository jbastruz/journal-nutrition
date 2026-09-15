#!/usr/bin/env python3
"""stats.py — Faits calculés sur le journal et la dépense Garmin.

    python3 nutrition/stats.py          # affiche le bloc, pour vérifier à la main

Séparé de `app.py` pour une raison précise : **ces chiffres doivent être
vérifiables sans lancer un serveur web**. Ce sont eux qui alimenteront les
insights rédigés par un modèle, et un modèle ne doit jamais avoir le droit de
sortir un nombre qui n'est pas déjà là — donc ce fichier est la frontière entre
ce qui est calculé et ce qui est raconté.

Qui décide qu'une journée ne compte pas
----------------------------------------
**JB, et lui seul.** Une version antérieure marquait « douteuse » toute journée
sous le métabolisme de base, en supposant que personne ne vit à 313 kcal. Il
m'a repris deux fois (04/09 et 05/09) : ses journées basses sont réelles.
L'heuristique écartait donc des données vraies des cumuls et affichait
« douteux » sur des journées qu'il avait vécues.

Règle actuelle : **une journée avec des saisies est une journée.** Les seules
exclusions viennent de `jours_saisie_incomplete` dans le profil, que JB
alimente lui-même. Le nombre de jours écartés est renvoyé avec le résultat,
jamais caché : un chiffre qui repose sur 4 jours sur 14 doit le dire.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from datetime import date as date_cls
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "nutrition.db"
PROFIL = BASE.parent / "config" / "nutrition-profil.json"
GARMIN_DB = Path.home() / "HealthData" / "DBs" / "garmin.db"
CACHE_DEPENSE = BASE / ".depense-jour.json"

KCAL_PAR_KG_GRAS = 7700       # équivalent énergétique classique du tissu adipeux


def bmr_mifflin(kg: float, cm: float, age: int, sexe: str) -> float:
    return 10 * kg + 6.25 * cm - 5 * age + (5 if sexe == "h" else -161)


def profil() -> dict:
    try:
        return json.loads(PROFIL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _depenses(depuis: str) -> dict[str, dict]:
    """Dépense par jour, depuis le miroir. Le jour courant vient du cache écrit
    par scripts/garmin-depense-cache.py — le miroir ne l'a jamais."""
    out: dict[str, dict] = {}
    if GARMIN_DB.exists():
        try:
            with sqlite3.connect(f"file:{GARMIN_DB}?mode=ro", uri=True) as g:
                g.row_factory = sqlite3.Row
                for r in g.execute(
                        "select date(day) as j, calories_total, calories_bmr, "
                        "calories_active, steps from daily_summary "
                        "where date(day) >= ? and calories_total is not null",
                        (depuis,)):
                    out[r["j"]] = {"total": r["calories_total"],
                                   "repos": r["calories_bmr"],
                                   "actif": r["calories_active"],
                                   "pas": r["steps"], "en_cours": False}
        except sqlite3.Error:
            pass
    try:
        c = json.loads(CACHE_DEPENSE.read_text(encoding="utf-8"))
        if c.get("jour") and c["jour"] not in out and c.get("total"):
            out[c["jour"]] = {"total": c["total"], "repos": c.get("repos"),
                              "actif": c.get("actif"), "pas": c.get("pas"),
                              "en_cours": True}
    except (OSError, ValueError):
        pass
    return out


def _pesees(depuis: str, poids_profil: float | None) -> list[dict]:
    """Pesées Garmin, avec les aberrantes signalées mais PAS supprimées.

    Le 28/08/2026, la balance a renvoyé 80,0 kg pour quelqu'un à 87,1 — JB a
    tranché que c'était faux. Supprimer la ligne en silence ferait disparaître
    la preuve que la balance ment parfois ; la garder sans la marquer ferait
    plonger toutes les tendances. On la marque, et le lecteur décide.
    """
    # Deux sources, fusionnées par jour : le miroir Garmin (lecture seule) et
    # la table `pesees` alimentée par la balance connectée via
    # /api/pesee-balance. La balance GAGNE sur Garmin pour un même jour — c'est
    # la mesure directe, là où Garmin peut n'en être qu'une recopie différée.
    #
    # `gras_pct` / `muscle_kg` ne viennent QUE de la balance : la table `weight`
    # du miroir Garmin n'a que deux colonnes (jour, poids). Un jour repris de
    # Garmin porte donc None sur les deux — c'est une absence de mesure, pas un
    # zéro, et l'écran doit pouvoir faire la différence.
    par_jour: dict[str, tuple[float, str, float | None, float | None]] = {}
    # Les jours où Garmin a pesé. Sert à trancher, plus bas, si une ligne marquée
    # `simulation` porte quand même un POIDS réel — cas courant, puisqu'une
    # composition inventée se pose volontiers sur une vraie pesée.
    jours_garmin: set[str] = set()
    if GARMIN_DB.exists():
        try:
            with sqlite3.connect(f"file:{GARMIN_DB}?mode=ro", uri=True) as g:
                for r in g.execute(
                        "select day, weight from weight where date(day) >= ? "
                        "order by day", (depuis,)):
                    par_jour[str(r[0])[:10]] = (r[1], "garmin", None, None)
                    jours_garmin.add(str(r[0])[:10])
        except sqlite3.Error:
            pass
    try:
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
            for j, kg, src, gras, muscle in c.execute(
                    "select jour, poids_kg, source, gras_pct, muscle_kg "
                    "from pesees where jour >= ? order by jour", (depuis,)):
                par_jour[j] = (kg, src or "balance", gras, muscle)
    except sqlite3.Error:
        pass
    if not par_jour:
        return []

    out = []
    for j in sorted(par_jour):
        kg, src, gras, muscle = par_jour[j]
        # Aberrante = plus de 8 % d'écart au poids du profil. Une variation
        # journalière réelle dépasse rarement 2 %, même avec l'eau et le sel.
        #
        # ⚠️ Ce critère est SIGNALÉTIQUE, pas un filtre d'entrée, et il ne doit
        # pas le devenir : mesuré le 14/09/2026 contre un profil à 83,4 kg, il
        # rejetterait 75 kg (l'objectif, 10,07 % d'écart) et laisserait passer
        # la fausse pesée de 80 kg (4,08 %) — faux dans les deux sens. Le
        # contrôle qui protège réellement la base est côté /api/pesee-balance,
        # et il compare à la DERNIÈRE pesée connue, pas au poids du profil.
        aberrante = bool(poids_profil and abs(kg - poids_profil) / poids_profil > 0.08)
        # `gras_kg` est CALCULÉ ici plutôt que renvoyé tel quel par la balance :
        # c'est le produit poids × %, donc il reste cohérent avec le poids retenu
        # pour ce jour même si les deux sources divergent. La masse maigre, elle,
        # est reprise de la balance quand elle existe et déduite sinon — les deux
        # doivent sommer au poids, et une mesure d'impédance ne le garantit pas.
        gras_kg = round(kg * gras / 100, 2) if gras is not None else None
        # Deux drapeaux et pas un seul : sur une ligne de démonstration, le poids
        # peut être vrai alors que la composition est inventée. Les confondre
        # ferait passer une vraie pesée pour une invention — ou l'inverse, plus
        # grave. Le poids est réputé RÉEL dès que Garmin l'a mesuré ce jour-là :
        # c'est une confrontation à une autre source, pas un drapeau posé à la
        # main qu'il faudrait penser à mettre à jour.
        simule = src == "simulation"
        out.append({"jour": j, "kg": round(kg, 2), "aberrante": aberrante,
                    "source": src,
                    "poids_simule": simule and j not in jours_garmin,
                    "compo_simule": simule,
                    "gras_pct": gras,
                    "gras_kg": gras_kg,
                    "maigre_kg": round(muscle, 2) if muscle is not None
                                 else (round(kg - gras_kg, 2)
                                       if gras_kg is not None else None)})
    return out


def deficit_a(poids: float, bareme: dict) -> float:
    """Déficit tenable à ce poids, interpolé entre les paliers du profil.

    Le déficit n'est pas une constante : il se referme en maigrissant, parce que
    la dépense baisse avec la masse et que l'apport finit par rejoindre le
    plancher. 850 kcal à 87 kg, 650 à 77.
    """
    pts = sorted(((float(k), v) for k, v in bareme.items()), reverse=True)
    if not pts:
        return 0.0
    if poids >= pts[0][0]:
        return float(pts[0][1])
    if poids <= pts[-1][0]:
        return float(pts[-1][1])
    for (p1, d1), (p2, d2) in zip(pts, pts[1:]):
        if p2 <= poids <= p1:
            t = (poids - p2) / (p1 - p2) if p1 != p2 else 0
            return d2 + t * (d1 - d2)
    return float(pts[-1][1])


def projection(depart: float, cible: float, bareme: dict,
               deficit_mesure: float | None = None) -> dict:
    """Semaines nécessaires pour aller de `depart` à `cible`.

    **Simulation semaine par semaine, pas une division.** Diviser l'écart de
    poids par un rythme constant suppose que le déficit reste le même tout du
    long — il rétrécit, donc la date sortirait trop optimiste. Ici le déficit
    est recalculé à chaque semaine sur le poids courant.

    `deficit_mesure` remplace le barème quand on a une mesure réelle : à ce
    moment-là on projette sur ce que JB fait vraiment, pas sur ce qu'il vise.
    """
    if not depart or not cible or depart <= cible:
        return {"possible": False, "raison": "objectif déjà atteint ou données absentes"}
    poids, semaines, trace = depart, 0, []
    while poids > cible and semaines < 260:          # garde-fou : 5 ans
        d = deficit_mesure if deficit_mesure is not None else deficit_a(poids, bareme)
        if d <= 0:
            return {"possible": False, "semaines": semaines,
                    "poids_atteint": round(poids, 1),
                    "raison": f"le déficit tombe à zéro vers {poids:.0f} kg — "
                              f"l'apport rejoint le plancher avant la cible"}
        perte = d * 7 / KCAL_PAR_KG_GRAS
        poids -= perte
        semaines += 1
        if semaines <= 52:
            trace.append({"semaine": semaines, "kg": round(max(poids, cible), 2)})
    if semaines >= 260:
        return {"possible": False, "raison": "au-delà de 5 ans au rythme actuel"}
    date = (date_cls.today() + timedelta(weeks=semaines)).isoformat()
    return {"possible": True, "semaines": semaines, "date": date,
            "perte_hebdo_kg": round((depart - cible) / semaines, 3),
            "trace": trace}


def stats(jours: int = 30) -> dict:
    p = profil()
    poids = p.get("poids_kg")
    cible_kcal = p.get("apport_cible_kcal")
    cible_prot = round(poids * p["proteines_g_par_kg"]) if poids and p.get("proteines_g_par_kg") else None
    bmr = (bmr_mifflin(poids, p.get("taille_cm") or p.get("hypothese_taille_cm", 178),
                       p.get("age") or p.get("hypothese_age", 35), p.get("sexe", "h"))
           if poids else None)

    exclus = set(p.get("jours_saisie_incomplete") or [])
    debut = (date.today() - timedelta(days=jours - 1)).isoformat()
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        saisi = {r["jour"]: dict(r) for r in c.execute("""
            select jour,
                   round(sum(energie_kcal), 0) as kcal,
                   round(sum(proteines), 1)    as proteines,
                   round(sum(lipides), 1)      as lipides,
                   round(sum(glucides), 1)     as glucides,
                   count(*)                    as entrees
            from journal where jour >= ? group by jour""", (debut,))}
    dep = _depenses(debut)

    serie = []
    for i in range(jours):
        j = (date.today() - timedelta(days=jours - 1 - i)).isoformat()
        s = saisi.get(j)
        d = dep.get(j)
        kcal = s["kcal"] if s else None
        # ⚠️ RÈGLE RETIRÉE LE 2026-09-05, ne pas la réintroduire.
        #
        # Le code marquait « douteuse » toute journée sous le métabolisme de base,
        # en supposant que personne ne vit à 313 kcal. JB m'a repris DEUX FOIS :
        # ses journées basses sont réelles. L'heuristique excluait donc des
        # données vraies des cumuls, et affichait « douteux » sur des journées
        # qu'il avait vécues — une app qui contredit son utilisateur sur la seule
        # chose qu'il sait mieux qu'elle.
        #
        # Remplacée par un fait vérifiable : une journée AVEC des saisies est une
        # journée. Les seules exclusions sont celles que JB déclare lui-même,
        # dans `jours_saisie_incomplete` du profil.
        en_cours = j == date.today().isoformat()
        complet = bool(s) and not en_cours and j not in exclus
        serie.append({
            "jour": j, "kcal": kcal,
            "proteines": s["proteines"] if s else None,
            "lipides": s["lipides"] if s else None,
            "glucides": s["glucides"] if s else None,
            "entrees": s["entrees"] if s else 0,
            "depense": d["total"] if d else None,
            "actif": d["actif"] if d else None,
            "pas": d["pas"] if d else None,
            "depense_en_cours": bool(d and d["en_cours"]),
            "solde": round(kcal - d["total"]) if (kcal is not None and d) else None,
            "complet": complet, "en_cours": en_cours,
        })

    def moyenne(n: int, champ: str, seulement_complets: bool = True) -> float | None:
        vals = [x[champ] for x in serie[-n:]
                if x[champ] is not None and (x["complet"] or not seulement_complets)]
        return round(sum(vals) / len(vals), 1) if vals else None

    complets = [x for x in serie if x["complet"]]
    declares_incomplets = [x for x in serie
                           if x["entrees"] and not x["complet"] and not x["en_cours"]]
    # Renommé le 2026-09-06 : ces jours ne sont plus « douteux » (un jugement de
    # la machine, retiré le 05/09) mais déclarés incomplets PAR JB. Garder
    # l'ancien nom laissait l'écran afficher « Douteux » sur une donnée qu'il
    # avait lui-même qualifiée — exactement le reproche qu'il m'avait fait.
    # par JB, jamais un verdict de la machine.
    vides = [x for x in serie if not x["entrees"] and not x["en_cours"]]

    # ── Calibration : la seule mesure RÉELLE du TDEE ──
    # Perte prédite = somme des soldes des jours complets / 7700.
    # Perte réelle  = première pesée fiable − dernière pesée fiable.
    # L'écart entre les deux dit de combien la dépense estimée se trompe.
    soldes = [x["solde"] for x in complets if x["solde"] is not None]
    solde_cumule = round(sum(soldes)) if soldes else None
    pes = [w for w in _pesees(debut, poids) if not w["aberrante"]]
    calib: dict = {
        "pesees": _pesees(debut, poids),
        "jours_de_solde": len(soldes),
        "solde_cumule_kcal": solde_cumule,
        "perte_predite_kg": round(-solde_cumule / KCAL_PAR_KG_GRAS, 2) if solde_cumule else None,
        "possible": False,
        "raison": None,
    }
    if len(pes) < 2:
        calib["raison"] = (f"{len(pes)} pesée(s) fiable(s) sur la période — il en faut "
                           f"au moins 2, espacées de 2 à 3 semaines.")
    elif not soldes:
        calib["raison"] = "aucun jour de saisie complète à confronter aux pesées."
    else:
        d_kg = pes[-1]["kg"] - pes[0]["kg"]
        n_j = (date.fromisoformat(pes[-1]["jour"]) - date.fromisoformat(pes[0]["jour"])).days
        calib.update({
            "possible": True,
            "perte_reelle_kg": round(-d_kg, 2),
            "jours_entre_pesees": n_j,
            # Écart entre la perte prédite et la perte réelle, ramené en kcal/jour.
            # Positif = on surestime la dépense (il perd moins que prévu).
            "erreur_kcal_par_jour": round(
                (d_kg * KCAL_PAR_KG_GRAS + (-solde_cumule)) / n_j) if n_j else None,
        })

    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as c:
        premier = c.execute("select min(jour) from journal").fetchone()[0]

    # ── Projection vers l'objectif de poids ──
    # Deux rythmes affichés séparément, et c'est le point : le PRÉVU (sa cible)
    # et le MESURÉ (ce que ses données montrent). Les confondre ferait passer une
    # intention pour un résultat.
    MIN_JOURS_FIABLE = 5
    objectif = p.get("objectif_poids_kg")
    bareme = p.get("deficit_par_poids") or {}
    depart = pes[-1]["kg"] if pes else poids
    # `depart_initial_kg` = la PREMIÈRE pesée fiable, `depart_kg` = la dernière.
    # Les distinguer permet d'afficher le chemin parcouru : avec une seule
    # valeur pour les deux, la jauge de progression n'aurait rien à mesurer.
    proj = {"objectif_kg": objectif, "depart_kg": depart,
            "deficit_cible": p.get("deficit_cible_kcal"),
            "depart_initial_kg": pes[0]["kg"] if pes else poids,
            "jours_mesures": len(soldes), "min_jours": MIN_JOURS_FIABLE}
    if objectif and depart and bareme:
        proj["prevue"] = projection(depart, objectif, bareme)
        if len(soldes) >= MIN_JOURS_FIABLE:
            d_mes = -sum(soldes) / len(soldes)          # déficit moyen, positif
            proj["deficit_mesure_kcal"] = round(d_mes)
            proj["mesuree"] = projection(depart, objectif, bareme, deficit_mesure=d_mes)
        else:
            proj["mesuree"] = {"possible": False,
                               "raison": f"{len(soldes)} jour(s) de saisie complète — "
                                         f"il en faut au moins {MIN_JOURS_FIABLE} pour "
                                         f"mesurer un rythme."}
    else:
        proj["prevue"] = {"possible": False, "raison": "objectif ou barème absent du profil"}

    return {
        "serie": serie,
        "projection": proj,
        # Sans cette date, 23 « jours vides » se lisent comme un abandon alors
        # que l'app vient d'être mise en service. Un trou avant la naissance de
        # l'outil n'est pas un trou.
        "premier_jour_saisi": premier,
        "cibles": {"kcal": cible_kcal, "proteines": cible_prot,
                   "bmr": round(bmr) if bmr else None, "poids_kg": poids},
        "moyennes": {
            "kcal_7j": moyenne(7, "kcal"), "kcal_14j": moyenne(14, "kcal"),
            "proteines_7j": moyenne(7, "proteines"),
            "proteines_14j": moyenne(14, "proteines"),
            "depense_7j": moyenne(7, "depense", seulement_complets=False),
            "depense_14j": moyenne(14, "depense", seulement_complets=False),
            # solde_7j = les MÊMES jours complets que le déficit mesuré de la
            # projection (variable `soldes`), pas une fenêtre calendaire : deux
            # fenêtres différentes donnaient deux « déficit moyen » incohérents
            # sur la même page (2165 vs 2101, constaté le 2026-09-07).
            "solde_7j": round(-sum(soldes) / len(soldes)) if soldes else None,
            "solde_jours": len(soldes),
        },
        "saisie": {
            "jours_complets": len(complets),
            "jours_declares_incomplets": len(declares_incomplets),
            "jours_vides": len(vides),
            "seuil_kcal": None,
            "note": ("Une journée avec des saisies compte comme une journée réelle. "
                     "Seules les dates que tu as toi-même déclarées incomplètes sont "
                     "écartées des cumuls — la machine ne juge plus tes journées."),
            "exclus": sorted(exclus),
        },
        "calibration": calib,
    }


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
