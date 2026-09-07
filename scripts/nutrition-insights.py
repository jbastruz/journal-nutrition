#!/usr/bin/env python3
"""nutrition-insights.py — Lecture coach des chiffres de nutrition.py, par Sonnet 5.

    python3 scripts/nutrition-insights.py            # génère et écrit
    python3 scripts/nutrition-insights.py --afficher # sans écrire ni appeler le modèle
    python3 scripts/nutrition-insights.py --sec      # affiche le prompt envoyé

La règle qui gouverne tout ce fichier
--------------------------------------
**Le modèle n'a pas le droit de produire un nombre.** Il reçoit des faits déjà
calculés par `nutrition/stats.py` et ne fait que les hiérarchiser et les
formuler. Tout nombre qu'il écrit doit se retrouver textuellement dans son
entrée — et c'est **vérifié après coup** (`verifie_chiffres`), pas seulement
demandé dans le prompt.

Pourquoi cette paranoïa : le 2026-09-04, j'ai chiffré un burger à 626 kcal sur
une fiche approchante alors qu'il en faisait 1 162. JB avait déjà commandé.
Un modèle qui improvise sur des données de santé produit des affirmations
plausibles, confiantes et fausses — et personne ne les relit.

Sonnet 5 et pas Haiku : règle de JB du 2026-08-08. Haiku pour le mécanique
(classer, extraire) ; dès qu'un modèle **rédige** un texte que je relirai
ensuite comme vrai, c'est Sonnet.

Une fois par jour par cron, pas à chaque ouverture de l'app : un insight qui
change à chaque rafraîchissement n'est pas un avis, c'est du bruit.

Pourquoi un rattrapage à midi (--rattrapage --notifier)
-------------------------------------------------------
Le 2026-09-06, le passage de 6h20 a échoué : l'authentification du binaire
`claude` avait expiré. Le script a fait ce qu'il devait — laisser le fichier de
la veille en place plutôt que d'écrire une erreur — puis s'est tu. L'app a donc
affiché toute la matinée l'analyse de la veille, correctement étiquetée
« générée il y a 15 h »… et c'est **JB qui l'a vu, pas moi**.

D'où deux gestes, pas un :

* `--rattrapage` : ne fait rien si l'analyse du jour existe déjà. Un second
  passage à 12h20 rattrape donc une panne du matin sans jamais écraser un
  résultat valable, ni consommer un appel pour rien.
* `--notifier` : en cas d'échec, prévient sur Discord via `notify-agent.sh`.

Le passage de 6h20 ne notifie **pas**. C'est délibéré : une panne à 6h a six
heures pour se résoudre, et une alerte au réveil pour un problème qui se
réparera tout seul est exactement le genre de bruit qu'on apprend à ignorer.
C'est le rattrapage qui parle, parce que s'il échoue à son tour, c'est
réellement cassé.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent
SORTIE = AGENT / "nutrition" / ".insights.json"
MODEL = os.environ.get("INSIGHTS_MODEL", "claude-sonnet-5")

_spec = importlib.util.spec_from_file_location("stats", AGENT / "nutrition" / "stats.py")
_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stats)


class LLMIndisponible(RuntimeError):
    pass


def notifie(raison: str) -> None:
    """Fait remonter un échec jusqu'à JB. Silencieux si le relais est absent.

    Un job cron n'a pas de session Discord autour de lui : `notify-agent.sh`
    injecte le message dans la session tmux de l'agent, qui le relaie. Même
    canal que les alertes nutrition et le healthcheck mémoire.
    """
    script = AGENT / "scripts" / "notify-agent.sh"
    if not script.exists():
        return
    msg = (f"Insights nutrition — la génération de rattrapage a échoué "
           f"({raison}). L'onglet Tendances affiche donc encore l'analyse "
           f"précédente, et elle est étiquetée comme telle. Préviens JB en une "
           f"phrase et propose-lui de relancer "
           f"`python3 scripts/nutrition-insights.py`.")
    try:
        subprocess.run(["bash", str(script), msg], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass          # une notif ratée ne doit pas masquer l'erreur d'origine


def resolve_claude() -> str:
    b = shutil.which("claude")
    if not b:
        raise LLMIndisponible("binaire `claude` introuvable dans le PATH")
    return b


def ask(prompt: str) -> str:
    b = resolve_claude()
    try:
        p = subprocess.run([b, "-p", "--model", MODEL, "--output-format", "text"],
                           input=prompt, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as e:
        raise LLMIndisponible("timeout après 180 s") from e
    except OSError as e:
        raise LLMIndisponible(f"exécution impossible : {e}") from e
    if p.returncode != 0:
        d = (p.stderr or "").strip().splitlines()
        raise LLMIndisponible(f"code {p.returncode} : {d[-1] if d else 'sans message'}")
    return p.stdout.strip()


def extrait_json(brut: str):
    """Le modèle enrobe parfois son JSON de texte ou d'un fence."""
    for amorce in ("[", "{"):
        i = brut.find(amorce)
        while i != -1:
            try:
                return json.JSONDecoder().raw_decode(brut[i:])[0]
            except ValueError:
                i = brut.find(amorce, i + 1)
    raise ValueError("aucun JSON exploitable dans la réponse")


def _norme(n: str) -> str:
    """« 2188,0 » et « 2188.0 » et « 2188 » désignent le même nombre.

    Sans cette normalisation, le garde-fou rejetait des insights JUSTES : les
    faits contiennent `2188.0` (float JSON) et le modèle écrit naturellement
    « 2188 ». Repéré au test unitaire, avant le premier appel réel.
    """
    n = n.replace(",", ".")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n or "0"


def _nombres_autorises(faits: dict) -> set[str]:
    return {_norme(n) for n in re.findall(r"\d+(?:[.,]\d+)?",
                                         json.dumps(faits, ensure_ascii=False))}


def chiffres_inventes(texte: str, autorises: set[str]) -> set[str]:
    """Nombres du texte qui ne viennent pas des faits.

    Deux tolérances, volontairement étroites :

    * **entier suivi de « % »** — le modèle a le droit de dériver un pourcentage
      de deux nombres qu'on lui a donnés ; c'est une lecture, pas une invention.
    * **entier ≤ 31** — comptages, rangs, jours (« 3 jours sur 5 »).

    Ce qui n'est PAS toléré, et c'est le point : **les décimales**. Une première
    version laissait passer tout nombre ≤ 100, et « tu as perdu 4,7 kg » — une
    hallucination pure — franchissait le filtre sans bruit. Les grandeurs
    physiques qui comptent ici (kilos, kcal/jour rapportés, g/kg) sont
    précisément des petits nombres à décimale.
    """
    t = texte.replace("\u202f", "").replace("\u00a0", "").replace(" ", "")
    inventes = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)?", t):
        brut = m.group()
        n = _norme(brut)
        if n in autorises:
            continue
        suite = t[m.end():m.end() + 1]
        entier = "." not in n
        if suite == "%" and entier and float(n) <= 100:
            continue
        if entier and float(n) <= 31:
            continue
        inventes.add(brut)
    return inventes


def verifie_chiffres(insights: list[dict], faits: dict) -> list[dict]:
    """Retire tout insight contenant un nombre absent des faits fournis.

    C'est le garde-fou qui compte. Demander dans le prompt « n'invente pas de
    chiffre » ne suffit pas : ça se respecte la plupart du temps, et l'une des
    fois où ça ne l'est pas, le chiffre faux part avec l'autorité du reste. On
    préfère perdre un insight juste plutôt qu'en publier un faux.
    """
    autorises = _nombres_autorises(faits)
    gardes = []
    for i in insights:
        texte = f"{i.get('titre', '')} {i.get('corps', '')} {i.get('action') or ''}"
        inventes = chiffres_inventes(texte, autorises)
        if inventes:
            print(f"  ✂️  écarté (chiffres absents des faits : "
                  f"{', '.join(sorted(inventes))}) — {i.get('titre', '?')[:60]}",
                  file=sys.stderr)
            continue
        gardes.append(i)
    return gardes


def analyse_du_jour() -> bool:
    """L'analyse en place a-t-elle été générée aujourd'hui ?

    On lit la date de `genere_a` plutôt que le mtime du fichier : le mtime est
    remis à zéro par n'importe quelle copie ou restauration de sauvegarde, et
    dirait alors « à jour » d'un contenu vieux de trois jours.
    """
    try:
        d = json.loads(SORTIE.read_text(encoding="utf-8"))
        return (d.get("genere_a") or "").startswith(dt.date.today().isoformat())
    except (OSError, ValueError):
        return False


PROMPT = """Tu es un coach sportif et diététique. Voici les données CALCULÉES de \
Jean-Baptiste, 33 ans, 178 cm, {poids} kg. Il suit le programme Insanity (HIIT \
pliométrique, 6 jours sur 7, démarré le 2026-09-01) et vise une perte de poids.

RÈGLE ABSOLUE — tu n'as PAS le droit d'écrire un nombre qui ne figure pas dans les \
données ci-dessous. Pas de calcul de ta part, pas d'estimation, pas d'ordre de \
grandeur inventé. Si un chiffre te manque pour dire quelque chose, dis-le en mots \
sans le chiffrer. Tout insight contenant un nombre absent des données sera supprimé \
automatiquement avant affichage.

DONNÉES :
{faits}

CE QUE TU DOIS SAVOIR POUR LES INTERPRÉTER :
- « complet » = journée réellement saisie. **Jean-Baptiste a confirmé DEUX FOIS \
(le 04/09 et le 05/09) que ses journées passées correspondent exactement à ce qu'il a \
mangé.** N'écris JAMAIS que son apport est sous-déclaré, sous-estimé, ou que le journal \
est incomplet : c'est faux, il te l'a dit, et le lui répéter revient à le contredire sur \
la seule chose qu'il sait mieux que toi. Les seules journées écartées sont celles qu'il \
a lui-même déclarées incomplètes (champ `exclus`).
- Corollaire : si le déficit mesuré est énorme, prends-le AU SÉRIEUX comme un fait \
physiologique — il mange vraiment très peu — au lieu de l'expliquer par une erreur de \
saisie. C'est un risque de santé, pas un artefact.
- La dépense Garmin vient à ~70 % d'une formule (Mifflin-St Jeor) et non d'une mesure. \
Elle est sans biais en moyenne mais peut se tromper de ±190 kcal sur un individu.
- Le jour courant est incomplet par construction : ne le compare pas à une journée finie.
- La calibration (poids réel vs poids prédit) est la seule vraie mesure de sa dépense. \
Si elle n'est pas possible, dis pourquoi et ce qu'il manque.
- Insanity n'est PAS de la musculation : pas de tension mécanique progressive, donc peu \
de protection de la masse maigre. Sa cible protéines existe pour ça.

PRODUIS un tableau JSON de 3 à 5 insights, du plus important au moins important. \
Chaque élément : {{"titre": "...", "corps": "...", "ton": "alerte"|"conseil"|"constat", \
"action": "..." ou null}}

- `titre` : 6 mots maximum.
- `corps` : 2 phrases maximum. Direct, tutoiement, pas de préambule.
- `action` : une chose concrète à faire, ou null s'il n'y a rien à faire.
- Priorise ce qui est ACTIONNABLE et ce qui va mal. Ne félicite pas pour meubler.
- Si les données sont trop maigres pour conclure, dis-le : c'est un insight valable.

Réponds UNIQUEMENT le JSON, rien autour."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--afficher", action="store_true",
                    help="affiche les faits sans appeler le modèle")
    ap.add_argument("--sec", action="store_true", help="affiche le prompt")
    ap.add_argument("--jours", type=int, default=30)
    ap.add_argument("--rattrapage", action="store_true",
                    help="ne rien faire si l'analyse du jour existe déjà")
    ap.add_argument("--notifier", action="store_true",
                    help="prévenir JB sur Discord en cas d'échec")
    a = ap.parse_args()

    if a.rattrapage and analyse_du_jour():
        print("analyse du jour déjà présente — rien à faire")
        return 0

    faits = _stats.stats(a.jours)
    # Allège : la série complète est utile à l'écran, pas au modèle. On ne lui
    # envoie que les jours qui portent une information.
    maigre = {**faits, "serie": [x for x in faits["serie"]
                                 if x["entrees"] or x["depense"]]}
    prompt = PROMPT.format(poids=faits["cibles"]["poids_kg"],
                           faits=json.dumps(maigre, ensure_ascii=False, indent=1))

    if a.sec:
        print(prompt)
        return 0
    if a.afficher:
        print(json.dumps(maigre, ensure_ascii=False, indent=2))
        return 0

    try:
        brut = ask(prompt)
    except LLMIndisponible as e:
        print(f"modèle indisponible : {e} — fichier inchangé", file=sys.stderr)
        if a.notifier:
            notifie(f"modèle indisponible : {e}")
        return 1
    try:
        insights = extrait_json(brut)
    except ValueError as e:
        print(f"réponse inexploitable : {e}", file=sys.stderr)
        if a.notifier:
            notifie(f"réponse inexploitable : {e}")
        return 1
    if not isinstance(insights, list):
        print("réponse inattendue (pas un tableau)", file=sys.stderr)
        if a.notifier:
            notifie("réponse inattendue du modèle (pas un tableau)")
        return 1

    avant = len(insights)
    insights = verifie_chiffres(insights, maigre)
    SORTIE.write_text(json.dumps({
        "genere_a": dt.datetime.now().isoformat(timespec="seconds"),
        "modele": MODEL,
        "insights": insights,
        "ecartes": avant - len(insights),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(insights)} insight(s) écrits"
          + (f", {avant - len(insights)} écarté(s) pour chiffre inventé"
             if avant != len(insights) else ""))
    for i in insights:
        print(f"  [{i.get('ton', '?')}] {i.get('titre', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
