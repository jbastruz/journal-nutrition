#!/usr/bin/env python3
"""ciqual-load.py — Charge la table CIQUAL (ANSES) dans une base SQLite locale.

    python3 scripts/ciqual-load.py            # télécharge si absent, puis charge
    python3 scripts/ciqual-load.py --force    # re-télécharge

Pourquoi une base et pas le XML à la volée : le fichier de composition fait
55 Mo, il est mal formé, et une recherche d'aliment doit répondre en quelques
millisecondes pour que la saisie tienne sous les 20 secondes. On paie le coût
une fois.

Les trois pièges du fichier de l'ANSES, tous rencontrés le 2026-08-30 :
  1. déclaré `windows-1252`, contient des octets non attribués dans cet encodage ;
  2. **pas du XML valide** — `<` littéral (« Panaché préemballé (<1° alc.) ») et
     `&` nu (« elle & Vire ») dans les libellés ;
  3. énergie absente sur une partie des aliments (4 entrées « pomme » sur 7).
     On la recalcule alors via les coefficients du règlement UE 1169/2011, et on
     **marque le champ comme calculé** : un chiffre dérivé ne doit jamais être
     présenté comme une mesure.
"""

from __future__ import annotations

import argparse
import io
import re
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

URL = "https://ciqual.anses.fr/cms/sites/default/files/inline-files/XML_2020_07_07.zip"
BASE = Path.home() / ".claude-agent" / "nutrition"
DB = BASE / "nutrition.db"
CACHE = BASE / "ciqual.zip"

# Constituants retenus. CIQUAL en compte 67 ; on garde ce qui sert à un suivi
# alimentaire, pas la totalité — une base plus étroite se lit plus vite et se
# comprend mieux.
# ⚠️ Ces codes ne s'inventent pas de mémoire. La liste qui fait autorité est
# `const_<version>.xml` DANS l'archive : <const_code> + <const_nom_fr>. Erreur
# constatée le 2026-08-31 : les six codes énergie/macros étaient bons, mais les
# SEPT codes eau/minéraux/vitamine étaient tous faux — « sel » lisait du
# magnésium (d'où une banane à « 37 g de sel »), « eau » du sodium, « fer » de la
# vitamine D, et trois codes n'existaient même pas dans CIQUAL. Ces trois-là sont
# le vrai piège : un code inconnu ne lève aucune erreur, il ne matche jamais, la
# colonne reste NULL et le chargement annonce « ✅ 3185 aliments ». D'où le
# contrôle `verifie_codes()` plus bas, qui refuse de charger sur un code inconnu.
CONST = {
    "328": "energie_kcal",      # Energie, Règlement UE 1169/2011 (kcal/100 g)
    "25000": "proteines",       # Protéines, N x facteur de Jones (g/100 g)
    "31000": "glucides",        # Glucides (g/100 g)
    "32000": "sucres",          # Sucres (g/100 g)
    "40000": "lipides",         # Lipides (g/100 g)
    "34100": "fibres",          # Fibres alimentaires (g/100 g)
    "400": "eau",               # Eau (g/100 g)
    "10004": "sel",             # Sel chlorure de sodium (g/100 g)
    "10120": "magnesium",       # Magnésium (mg/100 g)
    "10190": "potassium",       # Potassium (mg/100 g)
    "10200": "calcium",         # Calcium (mg/100 g)
    "10260": "fer",             # Fer (mg/100 g)
    "55100": "vitamine_c",      # Vitamine C (mg/100 g)
}


def lire_xml(data: bytes) -> ET.Element:
    """Décode et répare le XML de l'ANSES. Voir l'en-tête du module."""
    t = data.decode("windows-1252", errors="replace").replace("�", " ")
    t = re.sub(r"^<\?xml[^>]*\?>", '<?xml version="1.0" ?>', t, count=1)
    t = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", t)
    t = re.sub(r"<(?![/?!a-zA-Z])", "&lt;", t)
    return ET.fromstring(t)


def nombre(v: str | None) -> float | None:
    """CIQUAL écrit les décimaux à la française, et marque l'absence par '-',
    'traces' ou '< 0,1'. Tout ça vaut « pas de valeur exploitable »."""
    if not v:
        return None
    v = v.strip().replace(",", ".").replace("\xa0", "").replace(" ", "")
    if v in ("-", "", "traces", "-traces"):
        return None
    v = v.lstrip("<")          # « <0.1 » → on retient l'ordre de grandeur
    try:
        return float(v)
    except ValueError:
        return None


def energie_calculee(r: dict) -> float | None:
    """Coefficients du règlement UE 1169/2011, annexe XIV.
    Utilisé uniquement quand CIQUAL ne donne pas l'énergie."""
    p, g, l, f = (r.get(k) for k in ("proteines", "glucides", "lipides", "fibres"))
    if p is None and g is None and l is None:
        return None
    return round(4 * (p or 0) + 4 * (g or 0) + 9 * (l or 0) + 2 * (f or 0), 1)


def verifie_codes(const_xml: bytes) -> None:
    """Refuse de charger si un code de CONST ne désigne pas ce qu'on croit.

    Le vrai piège n'est pas le code faux, c'est le code INEXISTANT : il ne matche
    aucune ligne de composition, la colonne reste NULL, et le chargement annonce
    « ✅ 3185 aliments » sans le moindre avertissement. Trois des sept codes
    minéraux étaient dans ce cas et personne ne l'a vu pendant deux jours — c'est
    le même « succès vide » que le split('_') d'hier, et que garmindb_cli qui
    sort en 0 sur un login raté. Un chargeur doit échouer bruyamment.

    Le contrôle est une correspondance de mots, pas une égalité : `const_nom_fr`
    vaut « Sel chlorure de sodium (g/100 g) » pour le champ `sel`.
    """
    ATTENDU = {
        "energie_kcal": "energie", "proteines": "protéines", "glucides": "glucides",
        "sucres": "sucres", "lipides": "lipides", "fibres": "fibres",
        "eau": "eau", "sel": "sel", "magnesium": "magnésium",
        "potassium": "potassium", "calcium": "calcium", "fer": "fer",
        "vitamine_c": "vitamine c",
    }
    txt = const_xml.decode("windows-1252", errors="replace")
    reels = {c: n.strip() for c, n in re.findall(
        r"<const_code>\s*(\d+)\s*</const_code>.*?<const_nom_fr>(.*?)</const_nom_fr>",
        txt, re.S)}

    fautes = []
    for code, champ in CONST.items():
        vrai = reels.get(code)
        if vrai is None:
            fautes.append(f"  {code:>6} → `{champ}` : ce code N'EXISTE PAS dans CIQUAL "
                          f"(la colonne resterait vide en silence)")
            continue
        mot = ATTENDU.get(champ)
        if mot and mot not in vrai.lower():
            fautes.append(f"  {code:>6} → `{champ}` : désigne en réalité « {vrai} »")

    if fautes:
        raise SystemExit("❌ Codes de constituants incohérents, rien n'a été chargé :\n"
                         + "\n".join(fautes)
                         + f"\n\nLa liste qui fait autorité est {len(reels)} lignes dans "
                           "const_*.xml de l'archive (<const_code> + <const_nom_fr>).")
    print(f"codes vérifiés : {len(CONST)}/{len(CONST)} désignent bien le bon constituant")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-télécharge l'archive")
    args = ap.parse_args()

    BASE.mkdir(parents=True, exist_ok=True)

    if args.force or not CACHE.exists():
        print(f"téléchargement de CIQUAL… ({URL.rsplit('/', 1)[-1]})")
        req = urllib.request.Request(URL, headers={"User-Agent": "JeanCharles/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            CACHE.write_bytes(r.read())
    print(f"archive : {CACHE.stat().st_size / 1e6:.1f} Mo")

    z = zipfile.ZipFile(io.BytesIO(CACHE.read_bytes()))

    def fichier(prefixe: str) -> str:
        """Retrouve un fichier par son préfixe EXACT.
        Un simple split('_')[0] ne marche pas : `alim_grp_2020_07_07.xml` donne
        la même clé que `alim_2020_07_07.xml` et l'écrase — la table des groupes
        remplace alors celle des aliments, et le chargement rend 0 ligne
        sans la moindre erreur. Bug commis puis corrigé le 2026-08-30."""
        for n in z.namelist():
            base = n.rsplit("/", 1)[-1]
            if base.startswith(prefixe + "_") and base.endswith(".xml"):
                # `alim_` ne doit pas attraper `alim_grp_`
                reste = base[len(prefixe) + 1:-4]
                if re.fullmatch(r"\d{4}_\d{2}_\d{2}", reste):
                    return n
        raise SystemExit(f"fichier {prefixe}_*.xml introuvable dans l'archive : "
                         f"{z.namelist()}")

    noms = {k: fichier(k) for k in ("alim", "compo", "const")}
    print(f"fichiers retenus : {[v.rsplit('/', 1)[-1] for v in noms.values()]}")

    verifie_codes(z.read(noms["const"]))

    # ── aliments ──
    aliments = {}
    for a in lire_xml(z.read(noms["alim"])):
        code = (a.findtext("alim_code") or "").strip()
        nom = (a.findtext("alim_nom_fr") or "").strip()
        if code and nom:
            aliments[code] = {"nom": nom, "grp": (a.findtext("alim_grp_code") or "").strip()}
    print(f"{len(aliments)} aliments")

    # ── composition ──
    for c in lire_xml(z.read(noms["compo"])):
        ac = (c.findtext("alim_code") or "").strip()
        cc = (c.findtext("const_code") or "").strip()
        if ac in aliments and cc in CONST:
            v = nombre(c.findtext("teneur"))
            if v is not None:
                aliments[ac][CONST[cc]] = v

    # ── écriture ──
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
        drop table if exists ciqual;
        create table ciqual (
            code text primary key, nom text not null, groupe text,
            energie_kcal real, energie_calculee integer default 0,
            proteines real, glucides real, sucres real, lipides real,
            fibres real, eau real, sel real,
            potassium real, magnesium real, calcium real, fer real, vitamine_c real
        );
    """)
    # Recherche plein texte : c'est elle qui doit répondre en millisecondes.
    db.executescript("""
        drop table if exists ciqual_fts;
        create virtual table ciqual_fts using fts5(
            code unindexed, nom, tokenize='unicode61 remove_diacritics 2');
    """)

    cols = ["proteines", "glucides", "sucres", "lipides", "fibres",
            "eau", "sel", "potassium", "magnesium", "calcium", "fer", "vitamine_c"]
    calcules = 0
    for code, r in aliments.items():
        kcal, derive = r.get("energie_kcal"), 0
        if kcal is None:
            kcal = energie_calculee(r)
            derive = 1 if kcal is not None else 0
            calcules += derive
        db.execute(
            f"insert into ciqual (code, nom, groupe, energie_kcal, energie_calculee, "
            f"{', '.join(cols)}) values ({', '.join('?' * (5 + len(cols)))})",
            [code, r["nom"], r["grp"], kcal, derive] + [r.get(c) for c in cols])
        db.execute("insert into ciqual_fts (code, nom) values (?, ?)", [code, r["nom"]])

    db.commit()
    n = db.execute("select count(*) from ciqual").fetchone()[0]
    sans = db.execute("select count(*) from ciqual where energie_kcal is null").fetchone()[0]
    print(f"\n✅ {n} aliments dans {DB}")
    print(f"   énergie mesurée   : {n - calcules - sans}")
    print(f"   énergie calculée  : {calcules}   (marquées energie_calculee=1)")
    print(f"   sans énergie      : {sans}")
    db.close()


if __name__ == "__main__":
    main()
