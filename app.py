#!/usr/bin/env python3
"""Journal alimentaire — API + interface mobile, réseau local uniquement.

    .venv-garmin/bin/python3 nutrition/app.py

Deux sources, une seule barre de recherche :
  · **CIQUAL** (ANSES, local) pour les aliments bruts — une pomme n'a pas de
    code-barres, et Open Food Facts ne sait pas la trouver : sa recherche
    « pomme » rend du pain de mie, vérifié le 2026-08-30.
  · **Open Food Facts** (en ligne) pour les produits emballés, par code-barres.

La seule exigence de conception : **enregistrer un repas en moins de 20 s,
depuis le téléphone, à une main.** Si on rate ça, le journal est abandonné en
deux semaines et tout le reste ne sert à rien. Tout découle de cette contrainte.

Version Hermes — utilise les données dans ~/.hermes/
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Jean-Charles utilise son propre dossier
BASE = Path.home() / ".claude-agent" / "nutrition"
DB = BASE / "nutrition.db"
OFF = "https://world.openfoodfacts.org/api/v2/product/{}.json"

# Les objectifs vivent dans config/nutrition-profil.json
PROFIL = Path.home() / ".claude-agent" / "config" / "nutrition-profil.json"

# Dépense Garmin. Deux sources, aucune n'est l'API : voir depense() plus bas.
GARMIN_DB = Path.home() / "HealthData" / "DBs" / "garmin.db"
CACHE_DEPENSE = BASE / ".depense-jour.json"
CACHE_MAX_MIN = 90          # cron toutes les 30 min → tolère deux passages ratés
INSIGHTS = BASE / ".insights.json"

# stats.py est le SEUL endroit où les faits sont calculés — l'API et le script
# d'insights l'importent tous les deux.
import importlib.util as _ilu                                       # noqa: E402
_sp = _ilu.spec_from_file_location("stats", BASE / "stats.py")
_stats = _ilu.module_from_spec(_sp)
_sp.loader.exec_module(_stats)

app = FastAPI(title="Journal alimentaire")

# ZXing est servi DEPUIS ICI, pas depuis un CDN : le journal doit fonctionner
# même sans Internet (la recherche CIQUAL est locale), et une dépendance
# extérieure qui tombe emporterait le scan avec elle.
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    """Le journal vit dans SA base, jamais dans celle de GarminDB : cette
    dernière peut être reconstruite de zéro (`--rebuild_db`) et la saisie
    partirait avec. Une donnée saisie à la main ne se retélécharge pas."""
    with db() as c:
        c.executescript("""
            create table if not exists journal (
                id integer primary key autoincrement,
                jour text not null,
                horodatage text not null,
                repas text,
                source text not null,          -- 'ciqual' | 'off'
                ref text,                      -- code CIQUAL ou code-barres
                nom text not null,
                grammes real not null,
                energie_kcal real, energie_calculee integer default 0,
                proteines real, glucides real, sucres real,
                lipides real, fibres real, sel real
            );
            create index if not exists idx_journal_jour on journal(jour);

            -- Ce qu'on mange revient tous les jours. Remonter les habitudes en
            -- tête de liste est le principal levier sur les 20 secondes.
            create table if not exists favoris (
                source text, ref text, nom text,
                usages integer default 0, dernier text,
                primary key (source, ref)
            );

            -- Aliments saisis à la main : une TROISIÈME SOURCE, au même rang
            -- que CIQUAL et Open Food Facts, pas un cas particulier greffé sur
            -- le journal. Conséquence voulue : ils remontent dans la même
            -- recherche et favoris, et on ne paie pas l'interface deux fois.
            create table if not exists aliments_manuels (
                nom text primary key,
                energie_kcal real, proteines real,
                glucides real, sucres real,
                lipides real, fibres real, sel real
            );

            -- Repas récurrents : « je mange la même chose tous les midis ».
            -- Le plat de RÉFÉRENCE vit ici, jamais dans le journal : une
            -- proposition n'est pas une saisie, et la compter avant que JB ait
            -- dit « oui » gonflerait l'apport d'un repas peut-être sauté.
            -- Les valeurs pour 100 g sont un instantané : pour CIQUAL et les
            -- aliments maison on relit la source au moment de valider (une
            -- correction de la fiche suit), l'instantané ne sert qu'en secours
            -- et pour Open Food Facts (pas d'appel réseau depuis le journal).
            create table if not exists repas_recurrents (
                id integer primary key autoincrement,
                source text not null,
                ref text,
                nom text not null,
                grammes real not null,
                repas text not null default 'midi',
                actif integer not null default 1,
                cree text not null,
                energie_kcal real, energie_calculee integer default 0,
                proteines real, glucides real, sucres real,
                lipides real, fibres real, sel real
            );
            -- Une décision par jour et par plat : validé (→ ligne du journal)
            -- ou rejeté. La clé composite est ce qui garantit qu'un plat n'est
            -- jamais compté deux fois ni re-proposé une fois tranché.
            create table if not exists recurrents_jours (
                recurrent_id integer not null references repas_recurrents(id),
                jour text not null,
                statut text not null,          -- 'valide' | 'rejete'
                journal_id integer,
                decide text not null,
                primary key (recurrent_id, jour)
            );
        """)
        c.commit()


@app.on_event("startup")
def startup():
    init()


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    """Interface mobile : vue jour, entrée, recherche, favoris."""
    return (BASE / "index.html").read_text(encoding="utf-8")


LIBELLES = {"matin": "Matin", "midi": "Midi", "gouter": "Goûter", "soir": "Soir"}
# Heure posée sur une validation faite APRÈS COUP (un jour passé) : le journal
# exige un horodatage et « 12h30 » est plus honnête qu'un now() qui daterait un
# déjeuner de la veille à 22h.
HEURE_REPAS = {"matin": "08:00", "midi": "12:30", "gouter": "16:30", "soir": "19:30"}
CHAMPS = ("energie_kcal", "proteines", "glucides", "sucres", "lipides", "fibres", "sel")


def _valeurs_100g(rec: sqlite3.Row | dict) -> dict:
    """Valeurs pour 100 g d'un plat de référence, relues à la source quand
    elle est locale. Si la fiche maison ou CIQUAL a été corrigée depuis, la
    proposition suit — c'est ce que JB attend (« si les valeurs du plat de
    référence changent, les jours suivants suivent »)."""
    snap = {k: (rec[k] or 0) for k in CHAMPS}
    snap["energie_calculee"] = rec["energie_calculee"] or 0
    source, ref, nom = rec["source"], rec["ref"], rec["nom"]
    try:
        with db() as c:
            if source == "manuel":
                row = c.execute("""select energie_kcal, proteines, glucides, sucres,
                                          lipides, fibres, sel
                                   from aliments_manuels where nom = ?""",
                                (ref or nom,)).fetchone()
                if row:
                    return {**{k: (row[k] or 0) for k in CHAMPS}, "energie_calculee": 0}
            elif source == "ciqual" and ref:
                row = c.execute("""select energie_kcal, proteines, glucides, sucres,
                                          lipides, fibres, sel, energie_calculee
                                   from ciqual where code = ?""", (ref,)).fetchone()
                if row:
                    return {**{k: (row[k] or 0) for k in CHAMPS},
                            "energie_calculee": row["energie_calculee"] or 0}
    except Exception:
        pass
    return snap


def _portion(rec: sqlite3.Row | dict, grammes: float) -> dict:
    v = _valeurs_100g(rec)
    out = {k: round(v[k] * grammes / 100, 1) for k in CHAMPS}
    out["energie_calculee"] = v["energie_calculee"]
    return out


def _en_attente(c: sqlite3.Connection, jour: str) -> list[dict]:
    """Propositions non tranchées pour un jour : récurrents actifs, créés au
    plus tard ce jour-là, sans décision enregistrée. Jamais pour un jour
    futur — on ne valide pas un déjeuner qu'on n'a pas encore mangé."""
    if jour > date.today().isoformat():
        return []
    rows = c.execute("""
        select r.* from repas_recurrents r
        where r.actif = 1 and substr(r.cree, 1, 10) <= ?
          and not exists (select 1 from recurrents_jours j
                          where j.recurrent_id = r.id and j.jour = ?)
        order by r.repas, r.nom
    """, (jour, jour)).fetchall()
    out = []
    for r in rows:
        p = _portion(r, r["grammes"])
        out.append({
            "recurrent_id": r["id"], "jour": jour, "repas": r["repas"],
            "libelle": LIBELLES.get(r["repas"], r["repas"]),
            "source": r["source"], "ref": r["ref"], "nom": r["nom"],
            "grammes": r["grammes"], "en_attente": True, **p,
        })
    return out


def _decision(c: sqlite3.Connection, rid: int, jour: str) -> sqlite3.Row | None:
    return c.execute("select * from recurrents_jours where recurrent_id = ? and jour = ?",
                     (rid, jour)).fetchone()


@app.get("/api/jour")
def jour_data(d: str | None = None) -> dict:
    """API principale : solde du jour (apport − dépense)."""
    target_jour = d if d else date.today().isoformat()

    # Récupérer les objectifs pour calculer les jauges
    try:
        p = json.loads(PROFIL.read_text(encoding="utf-8"))
        objectifs = {
            "kcal": p.get("apport_cible_kcal"),
            "proteines": round(p.get("proteines_g_par_kg", 0) * p.get("poids_kg", 70)),
        }
    except Exception:
        objectifs = None

    # Apport — depuis journal (CIQUAL, OFF, manuel)
    with db() as c:
        # Total du jour
        cur = c.execute("""
            SELECT
                SUM(energie_kcal) as energie_kcal,
                SUM(proteines) as proteines,
                SUM(glucides) as glucides,
                SUM(lipides) as lipides,
                SUM(fibres) as fibres,
                COUNT(*) as entrees
            FROM journal
            WHERE jour = ?
        """, (target_jour,))
        total = cur.fetchone()

        # Bornes de l'historique (sans filtre de jour)
        bornes = c.execute("SELECT MIN(jour), MAX(jour) FROM journal").fetchone()
        premier_jour = bornes[0]
        dernier_jour = bornes[1]

        # Regrouper par repas
        cur = c.execute("""
            SELECT repas,
                   SUM(energie_kcal) as kcal,
                   SUM(proteines) as proteines,
                   COUNT(*) as n
            FROM journal
            WHERE jour = ?
            GROUP BY repas
        """, (target_jour,))
        groupes = []
        for g in cur.fetchall():
            # Récupérer les lignes de ce repas
            cur2 = c.execute("""
                SELECT id, nom, grammes, energie_kcal, proteines, glucides, lipides, horodatage
                FROM journal
                WHERE jour = ? AND repas = ?
                ORDER BY horodatage DESC
            """, (target_jour, g["repas"]))
            lignes = []
            for l in cur2.fetchall():
                lignes.append({
                    "id": l["id"],
                    "nom": l["nom"],
                    "grammes": l["grammes"],
                    "energie_kcal": l["energie_kcal"],
                    "proteines": l["proteines"],
                    "glucides": l["glucides"],
                    "lipides": l["lipides"],
                    "horodatage": l["horodatage"],
                })
            groupes.append({
                "libelle": LIBELLES.get(g["repas"], g["repas"]),
                "repas": g["repas"],
                "kcal": round(g["kcal"] or 0),
                "proteines": round(g["proteines"] or 0),
                "lignes": lignes,
                "en_attente": [],
            })

        # Toutes les lignes pour undo
        cur = c.execute("""
            SELECT id, jour, horodatage, repas, source, ref, nom, grammes,
                   energie_kcal, proteines, glucides, lipides, fibres
            FROM journal
            WHERE jour = ?
            ORDER BY horodatage DESC
        """, (target_jour,))
        lignes = [dict(row) for row in cur.fetchall()]

        # Repas récurrents en attente de validation. Rattachés au groupe de leur
        # repas (créé s'il n'existe pas encore) mais PAS additionnés : ni dans
        # `total`, ni dans le `kcal` du groupe. Le total du jour reste la somme
        # de la table journal, et rien d'autre.
        en_attente = _en_attente(c, target_jour)
        for p in en_attente:
            g = next((g for g in groupes if g.get("repas") == p["repas"]), None)
            if g is None:
                g = {"libelle": p["libelle"], "repas": p["repas"],
                     "kcal": 0, "proteines": 0, "lignes": [], "en_attente": []}
                groupes.append(g)
            g.setdefault("en_attente", []).append(p)

    # Dépense Garmin
    depense = None
    if target_jour == date.today().isoformat():
        # Jour courant : utiliser le cache
        if CACHE_DEPENSE.exists():
            try:
                cache = json.loads(CACHE_DEPENSE.read_text(encoding="utf-8"))
                if cache.get("jour") == target_jour and cache.get("mesure_a"):
                    cache_dt = datetime.fromisoformat(cache["mesure_a"])
                    age = (datetime.now() - cache_dt).total_seconds() / 60
                    if age < CACHE_MAX_MIN:
                        depense = {
                            "total": round(cache["total"]),
                            "repos": round(cache.get("repos", 0)),
                            "actif": round(cache.get("actif", 0)),
                            "en_cours": True,
                        }
                        if "pas" in cache:
                            depense["pas"] = cache["pas"]
            except Exception:
                pass
    else:
        # Jour passé : depuis GarminDB
        try:
            with sqlite3.connect(str(GARMIN_DB)) as c:
                c.row_factory = sqlite3.Row
                cur = c.execute("""
                    SELECT
                        COALESCE(calories_total, 0) as total,
                        COALESCE(calories_bmr, 0) as repos,
                        COALESCE(calories_active, 0) as actif,
                        COALESCE(steps, 0) as pas
                    FROM daily_summary
                    WHERE DATE(day) = ?
                """, (target_jour,))
                row = cur.fetchone()
                if row and row["total"]:
                    depense = {
                        "total": round(row["total"]),
                        "repos": round(row["repos"]),
                        "actif": round(row["actif"]),
                        "pas": row["pas"],
                        "en_cours": False,
                    }
        except Exception:
            pass

    # Suggestion de repas selon l'heure
    h = datetime.now().hour
    repas_suggere = "matin" if h < 11 else "midi" if h < 15 else "gouter" if h < 18 else "soir"

    return {
        "jour": target_jour,
        "total": {
            "energie_kcal": round(total["energie_kcal"] or 0),
            "proteines": round(total["proteines"] or 0),
            "glucides": round(total["glucides"] or 0),
            "lipides": round(total["lipides"] or 0),
            "fibres": round(total["fibres"] or 0),
        },
        "groupes": groupes,
        "lignes": lignes,
        "en_attente": en_attente,
        "premier_jour": premier_jour,
        "dernier_jour": dernier_jour,
        "objectifs": objectifs,
        "depense": depense,
        "repas_suggere": repas_suggere,
    }


@app.get("/api/favoris")
def favoris() -> dict:
    """Liste des favoris, triée par usage décroissant."""
    with db() as c:
        cur = c.execute("""
            SELECT f.source, f.ref, f.nom, f.usages, f.dernier,
                   (SELECT MAX(grammes) FROM journal j
                    WHERE j.source = f.source AND j.ref = f.ref
                    ORDER BY j.horodatage DESC LIMIT 1) as dernier_grammage
            FROM favoris f
            ORDER BY f.usages DESC, f.dernier DESC
        """)
        return {"resultats": [dict(row) for row in cur.fetchall()]}


@app.get("/api/recherche")
def recherche(q: str) -> dict:
    """Recherche CIQUAL (local) + Open Food Facts (en ligne)."""
    results = []

    # 1. Recherche CIQUAL (locale)
    try:
        with sqlite3.connect(str(DB)) as c:
            c.row_factory = sqlite3.Row
            cur = c.execute("""
                SELECT code as ref, nom,
                       energie_kcal,
                       proteines,
                       glucides,
                       sucres,
                       lipides,
                       fibres,
                       'ciqual' as source,
                       energie_calculee,
                       NULL as poids_comestible,
                       NULL as marque
                FROM ciqual
                WHERE nom LIKE ?
                LIMIT 20
            """, (f"%{q}%",))
            results.extend([dict(row) for row in cur.fetchall()])
    except Exception:
        pass

    # 2. Recherche Open Food Facts (si q ressemble à un code-barres)
    if re.match(r'^\d{8,13}$', q):
        try:
            url = urllib.request.Request(
                OFF.format(q),
                headers={"User-Agent": "JournalAlimentaire - Personal Use"}
            )
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.load(resp)
                if data.get("status") == 1:
                    p = data["product"]
                    results.append({
                        "ref": q,
                        "nom": p.get("product_name", "Produit"),
                        "energie_kcal": p.get("nutriments", {}).get("energy-kcal_100g"),
                        "proteines": p.get("nutriments", {}).get("proteins_100g"),
                        "glucides": p.get("nutriments", {}).get("carbohydrates_100g"),
                        "sucres": p.get("nutriments", {}).get("sugars_100g"),
                        "lipides": p.get("nutriments", {}).get("fat_100g"),
                        "fibres": p.get("nutriments", {}).get("fiber_100g"),
                        "source": "off",
                        "energie_calculee": None,
                        "poids_comestible": None,
                        "marque": p.get("brands"),
                    })
        except Exception:
            pass

    # 3. Recherche aliments manuels
    with db() as c:
        cur = c.execute("""
            SELECT nom, energie_kcal, proteines, glucides, sucres, lipides, fibres,
                   'manuel' as source, NULL as energie_calculee,
                   NULL as poids_comestible, NULL as marque, nom as ref
            FROM aliments_manuels
            WHERE nom LIKE ?
            LIMIT 10
        """, (f"%{q}%",))
        results.extend([dict(row) for row in cur.fetchall()])

    return {"resultats": results, "message": ""}


@app.post("/api/journal")
def ajouter_au_journal(item: dict) -> dict:
    """Ajoute un aliment au journal (avec date et repas)."""
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    source = item.get("source")
    ref = item.get("ref")
    nom = item["nom"]
    grammes = item["grammes"]
    repas = item.get("repas", "midi")

    # Récupérer les valeurs nutritionnelles
    energie_kcal = 0
    proteines = 0
    glucides = 0
    sucres = 0
    lipides = 0
    fibres = 0

    if source == "manuel":
        # Depuis aliments_manuels
        with db() as c:
            cur = c.execute("""
                SELECT energie_kcal, proteines, glucides, sucres, lipides, fibres
                FROM aliments_manuels
                WHERE nom = ?
            """, (nom,))
            row = cur.fetchone()
            if row:
                energie_kcal = (row["energie_kcal"] or 0) * grammes / 100
                proteines = (row["proteines"] or 0) * grammes / 100
                glucides = (row["glucides"] or 0) * grammes / 100
                sucres = (row["sucres"] or 0) * grammes / 100
                lipides = (row["lipides"] or 0) * grammes / 100
                fibres = (row["fibres"] or 0) * grammes / 100
    elif source == "ciqual":
        # Depuis CIQUAL
        try:
            with sqlite3.connect(str(DB)) as c:
                c.row_factory = sqlite3.Row
                cur = c.execute("""
                    SELECT energie_kcal, proteines, glucides,
                           sucres, lipides, fibres, energie_calculee
                    FROM ciqual
                    WHERE code = ?
                """, (ref,))
                row = cur.fetchone()
                if row:
                    energie_kcal = (row["energie_kcal"] or 0) * grammes / 100
                    proteines = (row["proteines"] or 0) * grammes / 100
                    glucides = (row["glucides"] or 0) * grammes / 100
                    sucres = (row["sucres"] or 0) * grammes / 100
                    lipides = (row["lipides"] or 0) * grammes / 100
                    fibres = (row["fibres"] or 0) * grammes / 100
        except Exception:
            pass
    elif source == "off":
        # Depuis Open Food Facts
        # ⚠️ `.get(k, 0)` ne suffit PAS : le défaut ne s'applique que si la clé
        # est ABSENTE. Open Food Facts envoie la clé avec `null` pour un champ
        # qu'il ne connaît pas (la whey isolate n'a pas de fibres déclarées), et
        # `None * grammes` lève un TypeError → 500 à l'enregistrement, vu le
        # 2026-09-09. Le `or 0` traite absent et nul de la même façon — c'est
        # l'idiome déjà utilisé par `_valeurs_100g` plus haut.
        energie_kcal = (item.get("energie_kcal") or 0) * grammes / 100
        proteines = (item.get("proteines") or 0) * grammes / 100
        glucides = (item.get("glucides") or 0) * grammes / 100
        sucres = (item.get("sucres") or 0) * grammes / 100
        lipides = (item.get("lipides") or 0) * grammes / 100
        fibres = (item.get("fibres") or 0) * grammes / 100

    # Enregistrer dans le journal
    with db() as c:
        cur = c.execute("""
            INSERT INTO journal
            (jour, horodatage, repas, source, ref, nom, grammes,
             energie_kcal, proteines, glucides, sucres, lipides, fibres)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (today, now, repas, source, ref, nom, grammes,
            round(energie_kcal, 1), round(proteines, 1), round(glucides, 1),
            round(sucres, 1), round(lipides, 1), round(fibres, 1)))
        nouvel_id = cur.lastrowid
        c.commit()

    # Mettre à jour les favoris
    if source and ref:
        with db() as c:
            cur = c.execute("""
                INSERT INTO favoris (source, ref, nom, usages, dernier)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT (source, ref) DO UPDATE SET
                    usages = usages + 1,
                    nom = ?,
                    dernier = ?
            """, (source, ref, nom, now, nom, now))
            c.commit()

    # L'id sert au front à épingler la ligne comme récurrent dans la foulée.
    return {"ok": True, "id": nouvel_id}


@app.delete("/api/journal/{id}")
def supprimer_du_journal(id: int) -> dict:
    """Supprime une entrée du journal."""
    with db() as c:
        c.execute("DELETE FROM journal WHERE id = ?", (id,))
        # Si la ligne venait d'un récurrent validé, la supprimer veut dire
        # « finalement non » : on passe en rejeté plutôt que d'effacer la
        # décision, sinon le plat reviendrait en grisé dans la seconde.
        c.execute("""update recurrents_jours set statut = 'rejete', decide = ?
                     where journal_id = ? and statut = 'valide'""",
                  (datetime.now().isoformat(), id))
        c.commit()
    return {"ok": True}


@app.post("/api/journal/restaurer")
def restaurer_du_journal(ligne: dict) -> dict:
    """Restaure une ligne supprimée (undo)."""
    with db() as c:
        cur = c.execute("""
            INSERT INTO journal
            (id, jour, horodatage, repas, source, ref, nom, grammes,
             energie_kcal, proteines, glucides, lipides, fibres)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ligne["id"], ligne["jour"], ligne["horodatage"], ligne["repas"],
              ligne["source"], ligne["ref"], ligne["nom"], ligne["grammes"],
              ligne["energie_kcal"], ligne["proteines"], ligne["glucides"],
              ligne["lipides"], ligne["fibres"]))
        # Miroir de la suppression : « Annuler » sur une ligne issue d'un
        # récurrent la remet en validé — l'id est conservé, le lien tient.
        c.execute("""update recurrents_jours set statut = 'valide', decide = ?
                     where journal_id = ? and statut = 'rejete'""",
                  (datetime.now().isoformat(), ligne["id"]))
        c.commit()
    return {"ok": True}


@app.get("/api/stats")
def stats(jours: int = 30) -> dict:
    """Statistiques pour l'onglet Tendances."""
    return _stats.stats(jours)


@app.get("/api/insights")
def insights() -> dict:
    """Insights IA du jour."""
    if not INSIGHTS.exists():
        return {"insights": []}
    try:
        return json.loads(INSIGHTS.read_text(encoding="utf-8"))
    except Exception:
        return {"insights": []}


@app.get("/api/pref")
def preferences() -> dict:
    """Bornes de navigation et objectifs."""
    with db() as c:
        premier = c.execute("SELECT MIN(jour) FROM journal").fetchone()[0]
        dernier = c.execute("SELECT MAX(jour) FROM journal").fetchone()[0]
    
    try:
        p = json.loads(PROFIL.read_text(encoding="utf-8"))
        return {
            "premier_jour": premier,
            "dernier_jour": dernier,
            "objectifs": {
                "kcal": p.get("apport_cible_kcal"),
                "proteines": round(p.get("poids_kg", 0) * p.get("proteines_g_par_kg", 0)) if p.get("poids_kg") and p.get("proteines_g_par_kg") else None,
            },
            "repas_suggere": "soir",
        }
    except Exception:
        return {
            "premier_jour": premier,
            "dernier_jour": dernier,
            "repas_suggere": "soir",
        }


@app.get("/api/repas-recents")
def repas_recents(limite: int = 10) -> dict:
    """Liste des repas récents pour reprise."""
    target_jour = date.today().isoformat()
    with db() as c:
        cur = c.execute("""
            SELECT repas, jour, COUNT(*) as n, SUM(energie_kcal) as kcal,
                   SUM(proteines) as proteines,
                   GROUP_SUBSTR(nom, ', ') as apercu
            FROM journal
            WHERE jour < ?
            GROUP BY repas, jour
            ORDER BY jour DESC, repas DESC
            LIMIT ?
        """, (target_jour, limite))
        # SQLite n'a pas GROUP_SUBSTR, on le fait en Python
        repas = []
        for g in cur.fetchall():
            cur2 = c.execute("""
                SELECT nom FROM journal
                WHERE jour = ? AND repas = ?
                ORDER BY horodatage DESC
                LIMIT 3
            """, (g["jour"], g["repas"]))
            noms = [row["nom"] for row in cur2.fetchall()]
            repas.append({
                "libelle": {"matin": "Matin", "midi": "Midi", "gouter": "Goûter", "soir": "Soir"}.get(g["repas"], g["repas"]),
                "jour": g["jour"],
                "n": g["n"],
                "kcal": round(g["kcal"] or 0),
                "proteines": round(g["proteines"] or 0),
                "apercu": ", ".join(noms),
                "repas": g["repas"],
            })
        return {"repas": repas}


@app.post("/api/repeter")
def repeter_meal(item: dict) -> dict:
    """Reprend un repas d'un jour passé."""
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    jour_source = item["jour"]
    repas_source = item["repas"]
    vers_repas = item["vers_repas"]

    # Récupérer les lignes du repas source
    with db() as c:
        cur = c.execute("""
            SELECT source, ref, nom, grammes, energie_kcal, proteines,
                   glucides, sucres, lipides, fibres
            FROM journal
            WHERE jour = ? AND repas = ?
        """, (jour_source, repas_source))
        lignes = [dict(row) for row in cur.fetchall()]

        # Insérer pour aujourd'hui
        for l in lignes:
            cur = c.execute("""
                INSERT INTO journal
                (jour, horodatage, repas, source, ref, nom, grammes,
                 energie_kcal, proteines, glucides, sucres, lipides, fibres)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (today, now, vers_repas, l["source"], l["ref"], l["nom"],
                  l["grammes"], l["energie_kcal"], l["proteines"], l["glucides"],
                  l["sucres"], l["lipides"], l["fibres"]))
        c.commit()

    return {"ok": True}


@app.get("/api/proteines")
def proteines(reste: float, limite: int = 12) -> dict:
    """Aliments riches en protéines pour combler un manque."""
    # Rechercher dans CIQUAL
    try:
        with sqlite3.connect(str(BASE / "ciqual.db")) as c:
            c.row_factory = sqlite3.Row
            cur = c.execute("""
                SELECT oridgef as ref, ordsfrrmlr as nom,
                       energ_kcal_100g as energie_kcal,
                       prot_g_100g as proteines,
                       'ciqual' as source
                FROM ciqual
                WHERE prot_g_100g > 5
                ORDER BY prot_g_100g DESC
                LIMIT ?
            """, (limite,))
            resultats = []
            for row in cur.fetchall():
                p_portion = row["proteines"] * 100 / 100  # 100 g
                portion_g = round(reste / (row["proteines"] / 100))
                kcal_portion = row["energie_kcal"] * portion_g / 100
                part_du_reste = min(100, round(p_portion / reste * 100))
                resultats.append({
                    "nom": row["nom"],
                    "ref": row["ref"],
                    "energie_kcal": row["energie_kcal"],
                    "proteines": row["proteines"],
                    "source": "ciqual",
                    "p_portion": round(p_portion),
                    "kcal_portion": round(kcal_portion),
                    "part_du_reste": part_du_reste,
                })
            return {
                "reste_g": reste,
                "portion_g": 100,
                "resultats": resultats,
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/manuel")
def creer_aliment_manuel(item: dict) -> dict:
    """Crée un aliment manuel."""
    nom = item["nom"]
    with db() as c:
        cur = c.execute("""
            INSERT OR REPLACE INTO aliments_manuels
            (nom, energie_kcal, proteines, glucides, sucres, lipides, fibres, sel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (nom, item.get("energie_kcal"), item.get("proteines"),
              item.get("glucides"), item.get("sucres"), item.get("lipides"),
              item.get("fibres"), item.get("sel")))
        c.commit()
    return {"aliment": {"nom": nom, **item}}


@app.delete("/api/manuel/{nom}")
def supprimer_aliment_manuel(nom: str) -> dict:
    """Supprime un aliment manuel."""
    with db() as c:
        c.execute("DELETE FROM aliments_manuels WHERE nom = ?", (nom,))
        c.commit()
    return {"ok": True}


# ── Repas récurrents ─────────────────────────────────────────────────────
#
# JB mange souvent le même plat le midi. Plutôt que de le ressaisir, le plat est
# proposé chaque jour en grisé et il ne reste qu'à dire oui ou non. Le « oui »
# crée une vraie ligne de journal ; le « non » ne crée rien, mais s'inscrit
# quand même — c'est ce qui empêche la proposition de revenir le même jour.

def _rec_dict(r: sqlite3.Row, jour: str | None = None, c: sqlite3.Connection | None = None) -> dict:
    d = dict(r)
    d["portion"] = _portion(r, r["grammes"])
    if jour and c is not None:
        dec = _decision(c, r["id"], jour)
        d["statut_jour"] = dec["statut"] if dec else ("en_attente" if r["actif"] else None)
    return d


@app.get("/api/recurrents")
def lister_recurrents(tous: bool = False, d: str | None = None) -> dict:
    """Récurrents actifs (ou tous), avec leur statut pour le jour demandé."""
    jour = d or date.today().isoformat()
    with db() as c:
        rows = c.execute(
            "select * from repas_recurrents" + ("" if tous else " where actif = 1")
            + " order by actif desc, repas, nom").fetchall()
        return {"recurrents": [_rec_dict(r, jour, c) for r in rows], "jour": jour}


@app.post("/api/recurrents")
def creer_recurrent(item: dict) -> dict:
    """Marque un plat comme récurrent. Deux entrées possibles :
    · `journal_id` — depuis une ligne existante du journal (valeurs ramenées
      à 100 g depuis la ligne) ;
    · `source`/`ref`/`nom`/`grammes` + valeurs pour 100 g — depuis une fiche.
    Même plat (source, ref/nom) au même repas ⇒ on réactive et on met à jour
    le grammage au lieu de dupliquer."""
    now = datetime.now().isoformat()
    repas = item.get("repas") or "midi"
    if repas not in LIBELLES:
        raise HTTPException(400, "repas inconnu")

    with db() as c:
        if item.get("journal_id") is not None:
            l = c.execute("select * from journal where id = ?", (item["journal_id"],)).fetchone()
            if not l:
                raise HTTPException(404, "ligne de journal introuvable")
            g = l["grammes"] or 0
            if g <= 0:
                raise HTTPException(400, "grammage nul")
            base = {"source": l["source"], "ref": l["ref"], "nom": l["nom"],
                    "grammes": item.get("grammes") or g,
                    "energie_calculee": l["energie_calculee"] or 0}
            for k in CHAMPS:
                base[k] = round((l[k] or 0) * 100 / g, 2)
            repas = item.get("repas") or l["repas"] or "midi"
        else:
            if not item.get("nom") or not item.get("grammes"):
                raise HTTPException(400, "nom et grammes requis")
            base = {"source": item.get("source") or "manuel", "ref": item.get("ref"),
                    "nom": item["nom"], "grammes": float(item["grammes"]),
                    "energie_calculee": item.get("energie_calculee") or 0}
            for k in CHAMPS:
                base[k] = item.get(k) or 0
        if base["grammes"] <= 0:
            raise HTTPException(400, "grammage nul")

        # Un aliment maison a souvent ref NULL dans le journal : la clé de
        # dédoublonnage retombe alors sur le nom.
        cle = base["ref"] or base["nom"]
        exist = c.execute("""select id from repas_recurrents
                             where source = ? and coalesce(ref, nom) = ? and repas = ?""",
                          (base["source"], cle, repas)).fetchone()
        if exist:
            c.execute("""update repas_recurrents set actif = 1, grammes = ?, nom = ?,
                         energie_kcal = ?, energie_calculee = ?, proteines = ?, glucides = ?,
                         sucres = ?, lipides = ?, fibres = ?, sel = ? where id = ?""",
                      (base["grammes"], base["nom"], base["energie_kcal"],
                       base["energie_calculee"], base["proteines"], base["glucides"],
                       base["sucres"], base["lipides"], base["fibres"], base["sel"],
                       exist["id"]))
            rid = exist["id"]
        else:
            cur = c.execute("""insert into repas_recurrents
                (source, ref, nom, grammes, repas, actif, cree, energie_kcal,
                 energie_calculee, proteines, glucides, sucres, lipides, fibres, sel)
                values (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (base["source"], base["ref"], base["nom"], base["grammes"], repas, now,
                 base["energie_kcal"], base["energie_calculee"], base["proteines"],
                 base["glucides"], base["sucres"], base["lipides"], base["fibres"],
                 base["sel"]))
            rid = cur.lastrowid
        # Créé depuis une ligne du journal du jour : ce jour-là est déjà mangé,
        # on le marque validé pour ne pas proposer en double ce qui est saisi.
        if item.get("journal_id") is not None and l["jour"] <= date.today().isoformat():
            c.execute("""insert or ignore into recurrents_jours
                         (recurrent_id, jour, statut, journal_id, decide)
                         values (?, ?, 'valide', ?, ?)""", (rid, l["jour"], l["id"], now))
        c.commit()
        r = c.execute("select * from repas_recurrents where id = ?", (rid,)).fetchone()
        return {"ok": True, "recurrent": _rec_dict(r, date.today().isoformat(), c),
                "reactive": bool(exist)}


@app.patch("/api/recurrents/{rid}")
def modifier_recurrent(rid: int, item: dict) -> dict:
    """Grammage, repas, actif. Le grammage modifié vaut pour les propositions
    à venir — celles déjà validées sont des lignes de journal, on n'y touche pas."""
    champs, vals = [], []
    if "grammes" in item:
        g = float(item["grammes"])
        if g <= 0:
            raise HTTPException(400, "grammage nul")
        champs.append("grammes = ?"); vals.append(g)
    if "repas" in item:
        if item["repas"] not in LIBELLES:
            raise HTTPException(400, "repas inconnu")
        champs.append("repas = ?"); vals.append(item["repas"])
    if "actif" in item:
        champs.append("actif = ?"); vals.append(1 if item["actif"] else 0)
    if not champs:
        raise HTTPException(400, "rien à modifier")
    with db() as c:
        if not c.execute("select 1 from repas_recurrents where id = ?", (rid,)).fetchone():
            raise HTTPException(404, "récurrent introuvable")
        c.execute(f"update repas_recurrents set {', '.join(champs)} where id = ?", (*vals, rid))
        c.commit()
        r = c.execute("select * from repas_recurrents where id = ?", (rid,)).fetchone()
        return {"ok": True, "recurrent": _rec_dict(r, date.today().isoformat(), c)}


@app.delete("/api/recurrents/{rid}")
def desactiver_recurrent(rid: int) -> dict:
    """Désactive (ne supprime pas) : l'historique des décisions reste lisible,
    et réactiver plus tard ne repart pas de zéro."""
    with db() as c:
        cur = c.execute("update repas_recurrents set actif = 0 where id = ?", (rid,))
        c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "récurrent introuvable")
    return {"ok": True}


def _jour_cible(item: dict) -> str:
    jour = item.get("jour") or date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", jour) or jour > date.today().isoformat():
        raise HTTPException(400, "jour invalide ou futur")
    return jour


@app.post("/api/recurrents/{rid}/valider")
def valider_recurrent(rid: int, item: dict | None = None) -> dict:
    """La proposition devient une ligne du journal, comptée normalement.
    Idempotent : déjà tranché ce jour-là ⇒ on renvoie l'état, on n'insère rien."""
    item = item or {}
    jour = _jour_cible(item)
    now = datetime.now().isoformat()
    with db() as c:
        r = c.execute("select * from repas_recurrents where id = ?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "récurrent introuvable")
        dec = _decision(c, rid, jour)
        if dec:
            return {"ok": True, "statut": dec["statut"], "journal_id": dec["journal_id"],
                    "deja": True}
        grammes = float(item.get("grammes") or r["grammes"])
        if grammes <= 0:
            raise HTTPException(400, "grammage nul")
        p = _portion(r, grammes)
        horodatage = now if jour == date.today().isoformat() \
            else f"{jour}T{HEURE_REPAS.get(r['repas'], '12:30')}:00"
        cur = c.execute("""
            INSERT INTO journal
            (jour, horodatage, repas, source, ref, nom, grammes,
             energie_kcal, energie_calculee, proteines, glucides, sucres, lipides, fibres, sel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (jour, horodatage, r["repas"], r["source"], r["ref"], r["nom"], grammes,
              p["energie_kcal"], p["energie_calculee"], p["proteines"], p["glucides"],
              p["sucres"], p["lipides"], p["fibres"], p["sel"]))
        jid = cur.lastrowid
        c.execute("""insert into recurrents_jours (recurrent_id, jour, statut, journal_id, decide)
                     values (?, ?, 'valide', ?, ?)""", (rid, jour, jid, now))
        if r["source"] and r["ref"]:
            c.execute("""
                INSERT INTO favoris (source, ref, nom, usages, dernier)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT (source, ref) DO UPDATE SET
                    usages = usages + 1, nom = ?, dernier = ?
            """, (r["source"], r["ref"], r["nom"], now, r["nom"], now))
        c.commit()
    return {"ok": True, "statut": "valide", "journal_id": jid, "deja": False}


@app.post("/api/recurrents/{rid}/rejeter")
def rejeter_recurrent(rid: int, item: dict | None = None) -> dict:
    """« Pas aujourd'hui » : rien au journal, mais la décision est notée pour
    que la proposition ne revienne pas ce jour-là."""
    jour = _jour_cible(item or {})
    with db() as c:
        if not c.execute("select 1 from repas_recurrents where id = ?", (rid,)).fetchone():
            raise HTTPException(404, "récurrent introuvable")
        dec = _decision(c, rid, jour)
        if dec:
            return {"ok": True, "statut": dec["statut"], "deja": True}
        c.execute("""insert into recurrents_jours (recurrent_id, jour, statut, journal_id, decide)
                     values (?, ?, 'rejete', NULL, ?)""", (rid, jour, datetime.now().isoformat()))
        c.commit()
    return {"ok": True, "statut": "rejete", "deja": False}


@app.delete("/api/recurrents/{rid}/jour/{jour}")
def annuler_decision(rid: int, jour: str) -> dict:
    """Annule un REJET : la proposition réapparaît en attente. Une validation
    ne s'annule pas ici — sa ligne de journal se supprime comme les autres,
    et c'est cette suppression qui bascule la décision."""
    with db() as c:
        dec = _decision(c, rid, jour)
        if not dec:
            return {"ok": True, "deja": True}
        if dec["statut"] == "valide":
            raise HTTPException(409, "déjà validé : supprimer la ligne du journal")
        c.execute("delete from recurrents_jours where recurrent_id = ? and jour = ?", (rid, jour))
        c.commit()
    return {"ok": True, "deja": False}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)