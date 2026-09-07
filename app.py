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
        """)
        c.commit()


@app.on_event("startup")
def startup():
    init()


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    """Interface mobile : vue jour, entrée, recherche, favoris."""
    return (BASE / "index.html").read_text(encoding="utf-8")


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
                "libelle": {"matin": "Matin", "midi": "Midi", "gouter": "Goûter", "soir": "Soir"}.get(g["repas"], g["repas"]),
                "kcal": round(g["kcal"] or 0),
                "proteines": round(g["proteines"] or 0),
                "lignes": lignes,
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
                energie_kcal = row["energie_kcal"] * grammes / 100
                proteines = row["proteines"] * grammes / 100
                glucides = row["glucides"] * grammes / 100
                sucres = row["sucres"] * grammes / 100
                lipides = row["lipides"] * grammes / 100
                fibres = row["fibres"] * grammes / 100
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
                    energie_kcal = row["energie_kcal"] * grammes / 100
                    proteines = row["proteines"] * grammes / 100
                    glucides = row["glucides"] * grammes / 100
                    sucres = row["sucres"] * grammes / 100
                    lipides = row["lipides"] * grammes / 100
                    fibres = row["fibres"] * grammes / 100
        except Exception:
            pass
    elif source == "off":
        # Depuis Open Food Facts
        energie_kcal = item.get("energie_kcal", 0) * grammes / 100
        proteines = item.get("proteines", 0) * grammes / 100
        glucides = item.get("glucides", 0) * grammes / 100
        sucres = item.get("sucres", 0) * grammes / 100
        lipides = item.get("lipides", 0) * grammes / 100
        fibres = item.get("fibres", 0) * grammes / 100

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

    return {"ok": True}


@app.delete("/api/journal/{id}")
def supprimer_du_journal(id: int) -> dict:
    """Supprime une entrée du journal."""
    with db() as c:
        c.execute("DELETE FROM journal WHERE id = ?", (id,))
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)