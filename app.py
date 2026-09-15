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
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from hashlib import scrypt
from hmac import compare_digest
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
# Journal brut des pesées reçues (diagnostic Shortcuts, chmod 600).
JOURNAL_PESEES = BASE / ".pesees-brutes.log"
CACHE_MAX_MIN = 90          # cron toutes les 30 min → tolère deux passages ratés
INSIGHTS = BASE / ".insights.json"

# stats.py est le SEUL endroit où les faits sont calculés — l'API et le script
# d'insights l'importent tous les deux.
import importlib.util as _ilu                                       # noqa: E402
_sp = _ilu.spec_from_file_location("stats", BASE / "stats.py")
_stats = _ilu.module_from_spec(_sp)
_sp.loader.exec_module(_stats)

app = FastAPI(title="Journal alimentaire")

# Le journal doit répondre à DEUX adresses sans qu'on ait à choisir : la racine
# (http://machine:8090/ en réseau local) et un sous-chemin derrière le reverse
# proxy Tailscale Funnel (https://…/nutrition/). Tailscale transmet le chemin
# COMPLET au backend — il ne retire pas le préfixe — donc sans rien, une requête
# GET /nutrition/api/jour ne matche aucune route et tombe en 404.
#
# Deux moitiés, indissociables :
#  · côté serveur, le middleware ci-dessous retire le préfixe avant le routage ;
#  · côté client, TOUS les chemins de index.html sont relatifs (`api/jour`,
#    `static/zxing.js`). Ils se résolvent contre l'URL de la page, donc contre
#    le préfixe quand il y en a un. Un seul chemin absolu qui reviendrait dans
#    le frontend casserait l'app sous /nutrition/ sans se voir en local.
_prefixe = os.environ.get("NUTRITION_PREFIX", "").strip("/")
PREFIX = f"/{_prefixe}" if _prefixe else ""


class PrefixeProxy:
    """Retire le préfixe du proxy du chemin, avant le routage.

    On réécrit `scope["path"]` au lieu de poser `root_path` : avec `root_path`,
    le mount StaticFiles recalcule son propre chemin et rend **404 sur
    /static/zxing.js en accès local direct** — le scan de code-barres tombe sur
    l'adresse du réseau local sans que ça se voie derrière le proxy. Réécrire le
    chemin laisse tout l'aval (routes ET mounts) dans son cas nominal.

    Un chemin sans préfixe passe intact : les deux adresses marchent ensemble,
    ce qui est la seule façon de tester en local ce qui tournera derrière le
    proxy."""

    def __init__(self, app, prefix: str = "") -> None:
        self.app, self.prefix = app, prefix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.prefix:
            await self.app(scope, receive, send)
            return
        chemin = scope["path"]
        # `/nutrition` sans slash final : le navigateur le prend pour un fichier
        # et résout `api/jour` contre la RACINE — toutes les requêtes partiraient
        # chez le dashboard. Le slash n'est pas cosmétique, il fixe la base.
        if chemin == self.prefix:
            qs = scope.get("query_string", b"").decode()
            cible = self.prefix + "/" + (f"?{qs}" if qs else "")
            await RedirectResponse(cible, status_code=307)(scope, receive, send)
            return
        if chemin.startswith(self.prefix + "/"):
            scope = dict(scope)
            scope["path"] = chemin[len(self.prefix):]
            scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


# IMPORTANT : ajouté APRÈS AuthMiddleware → exécuté AVANT lui (Starlette
# exécute le dernier ajouté en premier). L'auth voit donc les chemins internes.
if PREFIX:
    app.add_middleware(PrefixeProxy, prefix=PREFIX)


# ── Authentification ─────────────────────────────────────────────────────────
#
# L'app est exposée publiquement via Tailscale Funnel et porte des données de
# santé. Auth par identifiant/mot de passe sur TOUTES les routes (API + pages),
# sauf /login. Même philosophie que nutrition-profil.json : la config est
# RELUE À CHAQUE REQUÊTE — changer le mot de passe ne redémarre rien.
#
# Config : ~/.claude-agent/config/nutrition-auth.json (chmod 600)
#   { "utilisateur": "jbastruz",
#     "scrypt": { "n":…, "r":…, "p":…, "salt":…, "hash":… },
#     "secret": <graine de signature des tokens de session> }
#
# Session : cookie HttpOnly + SameSite=Lax, 7 jours. Le token est un secret
# aléatoire signé HMAC-SHA256 avec `secret` — pas de JWT, pas de dépendance.
AUTH_CFG = Path.home() / ".claude-agent" / "config" / "nutrition-auth.json"
DUREE_SESSION = timedelta(days=7)
COOKIE = "session_nutrition"


def auth_cfg() -> dict:
    cfg = json.loads(AUTH_CFG.read_text(encoding="utf-8"))
    s = cfg["scrypt"]
    cfg["_hash"] = bytes.fromhex(s["hash"])
    cfg["_salt"] = bytes.fromhex(s["salt"])
    return cfg


def _verifie_mdp(mdp: str, cfg: dict) -> bool:
    s = cfg["scrypt"]
    calc = scrypt(mdp.encode(), salt=cfg["_salt"], n=s["n"], r=s["r"], p=s["p"])
    return compare_digest(calc, cfg["_hash"])


def _signe(msg: bytes, cle: bytes) -> str:
    import hashlib as _h
    import hmac as _m
    return _m.new(cle, msg, _h.sha256).hexdigest()


def _nouveau_token(secret: str) -> str:
    cle = secret.encode()
    exp = str(int((datetime.now(timezone.utc) + DUREE_SESSION).timestamp()))
    nonce = os.urandom(16).hex()
    return f"{exp}.{nonce}.{_signe(exp.encode() + b'.' + nonce.encode(), cle)}"


def _token_valide(token: str, secret: str) -> bool:
    cle = secret.encode()
    try:
        exp, nonce, sig = token.split(".")
    except ValueError:
        return False
    if compare_digest(sig, _signe(f"{exp}.{nonce}".encode(), cle)) is False:
        return False
    try:
        return int(exp) > int(datetime.now(timezone.utc).timestamp())
    except ValueError:
        return False


PAGE_LOGIN = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#12121a">
<title>Connexion — Journal alimentaire</title>
<!-- Les mêmes icônes que index.html, et pour une raison précise : c'est sur
     CETTE page qu'on est quand on ajoute l'app à son écran d'accueil (on n'est
     pas encore connecté). Sans ces balises ici, iOS reprenait une vignette de
     la page de login, et le navigateur réclamait `/favicon.ico` à la racine du
     domaine — 404 à chaque affichage. Chemins RELATIFS comme dans index.html :
     ils se résolvent contre le répertoire de la page, donc `/nutrition/static/…`
     derrière le proxy et `/static/…` en local. `/static/…` est servi sans
     session (voir l'exemption dans le middleware), sinon l'icône serait
     inaccessible précisément là où on en a besoin. -->
<link rel="icon" type="image/x-icon" href="static/favicon.ico">
<link rel="icon" type="image/png" sizes="32x32" href="static/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="static/favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="static/apple-touch-icon.png">
<style>
/* Les jetons sont recopiés de index.html et non partagés : la page de login est
   servie AVANT toute session, donc sans le moindre fetch, et une feuille
   externe ajouterait un aller-retour réseau devant un formulaire de trois
   champs. Le prix est cette duplication — la contrepartie est qu'on ne voit
   jamais la page se repeindre. Les valeurs qui comptent (émeraude, orange,
   fonds, rayons) sont les mêmes des deux côtés.

   ⚠️ La copie est MUETTE : changer la teinte du verre dans index.html ne
   change rien ici, et la dérive ne se voit qu'en se déconnectant — un geste
   qu'on ne fait presque jamais. Touchant au verre, repasser sur ce bloc.

   Ce piège s'est refermé une première fois le 13/09/2026 : le rework iOS 26 a
   corrigé les arrêts en pourcentage et le liseré trop clair dans index.html,
   et cette page est restée une journée entière avec l'ANCIENNE version des
   deux — exactement les deux défauts que JB avait fait corriger. Elle ne
   s'affiche jamais tant qu'on a une session, donc rien ne le signalait.
   Resynchronisé le 14/09/2026 : --verre-teinte-fort, --verre-flou,
   --verre-fond-fort, --verre-liseret, --verre-accent, --champ-verre,
   --champ-creux, --accent, --accent-clair, --bg, --txt, --bord, --r-l,
   --r-xl, --ressort — tous copiés à l'identique de index.html. */
:root{
  color-scheme: dark;
  --bg:#12121a; --carte:#1c1f2b;
  --bord:rgba(255,255,255,.075); --bord-fort:rgba(255,255,255,.14);
  --txt:#eef1f7; --doux:#9aa3b5; --faible:#6e7789;
  --accent:#10b981; --accent-clair:#34d399;
  --accent-sourd:rgba(16,185,129,.14); --accent-bord:rgba(16,185,129,.35);
  --sur-accent:#04231a; --danger:#f0655c;
  --r-s:11px; --r-m:14px; --r-l:20px; --r-xl:28px;
  --ressort:cubic-bezier(.32,.72,0,1);
  --champ-verre:rgba(0,0,0,.26);
  --champ-creux:inset 0 1px 2px rgba(0,0,0,.4);
  /* Mêmes jetons de verre que l'app (voir le commentaire long dans
     index.html) : la carte de connexion est le premier objet que l'on voit,
     elle doit annoncer la matière de ce qu'il y a derrière.

     C'est le fond FORT et pas le fond ordinaire : cette carte porte des champs
     de saisie, elle relève donc du même registre que les fiches de l'app
     (`dialog`), pas des barres flottantes. Sous 0,8 d'opacité, un texte tapé
     se met à vibrer sur ce qui est derrière.

     ⚠️ Arrêts en PIXELS, teinte PLATE. Un voile en pourcentage se met à
     l'échelle de l'élément : sur une carte de 450 px de haut il devient un
     lavis qui couvre la moitié de la surface. Ce qui brille sur iOS 26, c'est
     l'ARÊTE — le liseré d'un pixel et l'anneau du ::after — pas la face. */
  --verre-teinte-fort:rgba(22,24,34,.80);
  --verre-flou:saturate(180%) blur(30px);
  --verre-fond-fort:
    linear-gradient(180deg,rgba(255,255,255,.085) 0,
                    rgba(255,255,255,.038) 3px,
                    rgba(255,255,255,.01) 9px,
                    rgba(255,255,255,0) 18px),
    linear-gradient(180deg,var(--verre-teinte-fort),var(--verre-teinte-fort));
  /* Liseré DIRECTIONNEL : l'arête haute est franche (la lumière entre par
     là), le tour est presque muet. Les valeurs d'origine (.28/.07/.055)
     traçaient un contour à intensité constante — un trait dessiné autour de
     la forme, pas un rebord de lentille. */
  --verre-liseret:
    inset 0 1px 0 rgba(255,255,255,.14),
    inset 0 0 0 1px rgba(255,255,255,.016),
    inset 0 -1px 0 rgba(255,255,255,.018);
  /* Verre ACTIF (le bouton de connexion). Le vert ne devient pas un aplat
     opaque quand il s'allume — il se teinte. */
  --verre-accent:
    linear-gradient(180deg,rgba(255,255,255,.20) 0,
                    rgba(255,255,255,.07) 3px,
                    rgba(255,255,255,.018) 9px,
                    rgba(255,255,255,0) 18px),
    linear-gradient(180deg,rgba(52,211,153,.92) 0,
                    rgba(16,185,129,.87) 14px,
                    rgba(16,185,129,.87));
}
*{box-sizing:border-box}
body{font:16px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",
     Inter,Roboto,"Helvetica Neue",sans-serif;
     background:var(--bg);color:var(--txt);
     -webkit-font-smoothing:antialiased;
     display:flex;min-height:100dvh;margin:0;align-items:center;
     justify-content:center;position:relative;overflow:hidden;
     /* La carte ne touche jamais le bord, même sur un écran de 320 px ni sous
        l'encoche : c'est la même gouttière que l'app, exprimée ici en marge
        du corps puisqu'il n'y a qu'un seul bloc à placer. */
     padding:calc(24px + env(safe-area-inset-top)) 20px
             calc(24px + env(safe-area-inset-bottom))}
/* Deux halos posés DERRIÈRE la carte, et non dedans. Sans eux le verre n'a
   rien à réfracter : sur un fond uni, un `backdrop-filter` ne produit
   strictement aucun pixel visible, et toute la matière au-dessus revient à
   une carte grise. Ce sont eux qui rendent la translucidité lisible. */
body::before{content:'';position:absolute;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(46% 38% at 22% 18%,rgba(16,185,129,.30),transparent 70%),
    radial-gradient(42% 34% at 82% 84%,rgba(249,115,22,.20),transparent 72%)}
/* Même registre que les fiches de l'app : rayon 28 (le « continuous corner »
   d'iOS 26), verre épais, liseré directionnel. */
.carte{position:relative;z-index:1;
       background:var(--verre-fond-fort);
       -webkit-backdrop-filter:var(--verre-flou);
       backdrop-filter:var(--verre-flou);
       border:1px solid rgba(255,255,255,.14);border-radius:var(--r-xl);
       padding:30px 22px 22px;width:min(100%,360px);
       box-shadow:var(--verre-liseret),0 28px 70px rgba(0,0,0,.62),
                  0 4px 14px rgba(0,0,0,.4)}
/* Anneau de réfraction : un second flou, plus contrasté, masqué pour ne
   garder que les 2 px du bord. C'est ce qui distingue une lentille d'un
   simple calque dépoli — le décor se tord au bord au lieu d'y être coupé.

   Les valeurs sont celles de l'app depuis le 13/09/2026 : 6px → 2px et
   brightness 1.22 → 1.045, parce qu'à 6 px ce n'était plus un bord mais un
   cadre, et qu'un ruban clair qui fait le tour d'une forme se lit comme un
   trait de contour. La directionnalité est portée par `--verre-liseret`.

   🚫 Ne pas dégrader le masque extérieur : `mask-composite:exclude` compose
   en `A(1−B) + B(1−A)`, donc un extérieur semi-transparent rend l'INTÉRIEUR
   visible et fantôme tout le formulaire. Les deux masques restent pleins. */
.carte::after{content:'';position:absolute;inset:0;border-radius:inherit;
  pointer-events:none;z-index:3;padding:2px;
  -webkit-backdrop-filter:blur(2px) brightness(1.045) saturate(1.12);
  backdrop-filter:blur(2px) brightness(1.045) saturate(1.12);
  -webkit-mask:linear-gradient(#000,#000) content-box,linear-gradient(#000,#000);
  -webkit-mask-composite:xor;
  mask:linear-gradient(#000,#000) content-box,linear-gradient(#000,#000);
  mask-composite:exclude}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
  .carte{background:var(--carte)}
  .carte::after{display:none}
}
/* « Large title » : la même que la barre du haut de l'app (30px / -.033em),
   d'un cran plus petite parce qu'elle doit tenir sur une ligne dans une carte
   de 360 px. */
h1{font-size:27px;margin:0 0 4px;text-align:center;font-weight:700;
   letter-spacing:-.033em;line-height:1.08}
.sous{color:var(--doux);font-size:13.5px;text-align:center;margin:0 0 20px;
      letter-spacing:-.01em}
.logo{display:block;width:72px;height:72px;margin:0 auto 14px;
      filter:drop-shadow(0 4px 12px rgba(0,0,0,.45))}

/* ── Liste groupée ────────────────────────────────────────────────
   Les deux champs vivent dans UN seul conteneur creusé, libellé à gauche,
   saisie à droite, séparateur en retrait — le vocabulaire exact des listes
   du journal, et celui des réglages iOS. Ce que ça remplace : deux gélules
   isolées surmontées d'un libellé EN MAJUSCULES ESPACÉES, qui est un idiome
   iOS 7-13. iOS 26 ne met plus de majuscules à un libellé de champ, et ne
   fait plus flotter les champs les uns au-dessus des autres.

   Le creux est porté par le conteneur et pas par chaque champ : c'est un
   seul objet enfoncé dans le verre, pas deux. */
.champs{background:var(--champ-verre);border:1px solid rgba(255,255,255,.1);
        border-radius:var(--r-l);box-shadow:var(--champ-creux);
        overflow:hidden}
/* Rembourrage 13/15 et retrait de séparateur à 15 px : la RECETTE de `.ligne`
   dans le journal, au pixel près. Ce qui est commun est la recette, pas la
   hauteur — une ligne du journal fait 73 à 98 px parce qu'elle porte un
   détail sur une seconde ligne, celle-ci en fait 50 avec son unique ligne de
   texte. Mesuré le 14/09/2026 : au-dessus des 44 px de cible tactile d'Apple
   dans les deux cas. */
.rang{display:flex;align-items:center;gap:10px;padding:13px 15px;
      transition:background .18s ease}
/* Les lignes portent ELLES-MÊMES l'arrondi du groupe (20 px du conteneur
   moins son 1 px de bordure = 19). On ne compte donc jamais sur le
   `overflow:hidden` du parent pour rogner le surlignage vert.

   Et il ne faut surtout pas y compter : signalé par JB le 14/09/2026, capture
   iPhone à l'appui — le liseré émeraude de la ligne au point sortait par les
   coins arrondis du groupe. `.carte` porte un `backdrop-filter`, ce qui fait
   perdre à Safari le rognage arrondi de ses descendants. Chromium rognait
   correctement, donc le défaut était INVISIBLE ici : c'est la capture de son
   téléphone qui l'a sorti, pas mes mesures.

   Arrondir la ligne règle le cas dans les deux moteurs sans dépendre d'un
   comportement qui diverge de l'un à l'autre. `overflow:hidden` reste sur le
   conteneur, en ceinture et bretelles. */
.rang:first-child{border-radius:19px 19px 0 0}
.rang:last-child{border-radius:0 0 19px 19px}
/* Séparateur décalé de 15 px à gauche et filant jusqu'au bord droit : un
   `border-top` ne sait pas s'arrêter avant le bord, d'où le dégradé plat
   tracé en image de fond. Même recette que `.repasCarte .ligne + .ligne`. */
.rang + .rang{background-image:linear-gradient(var(--bord),var(--bord));
              background-repeat:no-repeat;
              background-size:calc(100% - 15px) 1px;
              background-position:15px 0}
/* Colonne de libellés à largeur fixe : sans elle « Identifiant » et « Mot de
   passe » ne font pas la même largeur et les deux champs ne tombent pas sur
   la même verticale. 104 px pour 93,4 px de texte mesurés sur le plus long —
   la marge couvre les polices système plus larges que SF Pro (Segoe, Roboto)
   sans troncature. */
.rang .lib{flex:none;width:104px;font-size:16px;font-weight:500;
           letter-spacing:-.014em;color:var(--txt);
           transition:color .18s ease}
/* Sous 360 px de fenêtre, la carte ne fait plus que 280 px et la colonne fixe
   ne laissait que 88 px de saisie — une adresse mail y défile dans un hublot.
   On resserre le libellé et la gouttière plutôt que de tronquer « Mot de
   passe ». Concerne les iPhone SE de 1ʳᵉ génération et les vieux Android. */
@media (max-width:359px){
  .rang{padding:13px 12px;gap:8px}
  .rang + .rang{background-size:calc(100% - 12px) 1px;background-position:12px 0}
  /* 94 et pas 88 : à 88 px, « Mot de passe » ne dégageait que 0,5 px, mesuré
     — une police système un cheveu plus large et le libellé se tronque. */
  .rang .lib{width:94px;font-size:15px}
  .carte{padding:26px 18px 18px}
}
/* Le champ n'a plus ni fond ni cadre : le creux et la surface sont au
   conteneur. Un cadre par champ redessinerait deux boîtes dans la boîte.
   16 px minimum : en dessous, iOS zoome à la mise au point et décadre la
   carte. */
.rang input{flex:1;min-width:0;background:none;border:none;color:inherit;
            font-size:16px;font-family:inherit;padding:2px 0;outline:none;
            letter-spacing:-.01em}
.rang input::placeholder{color:var(--faible)}
/* Mise au point signalée SUR LA LIGNE et en ombre interne : le conteneur est
   en `overflow:hidden`, un anneau extérieur y serait rogné. La ligne se
   teinte, le libellé passe à l'émeraude — on voit dans quel champ on écrit
   même au clavier. */
.rang:focus-within{background-color:rgba(16,185,129,.10);
                   box-shadow:inset 0 0 0 1.5px var(--accent-bord)}
.rang:focus-within .lib{color:var(--accent-clair)}

/* Aplat d'émeraude plein, comme le bouton principal des fiches. Texte vert
   très sombre et non blanc : sur un aplat clair, le blanc passe sous le seuil
   de contraste alors que l'œil le croit lisible. */
button{width:100%;background:var(--verre-accent);
       color:var(--sur-accent);border:none;border-radius:99px;
       padding:.88rem;font-size:16px;font-family:inherit;
       cursor:pointer;font-weight:600;letter-spacing:-.01em;
       box-shadow:inset 0 1px 0 rgba(255,255,255,.4),
                  0 4px 16px rgba(16,185,129,.34);
       transition:filter .13s ease,transform .3s var(--ressort)}
button:hover{filter:brightness(1.07)}
button:active{transform:scale(.97)}
button:focus-visible{outline:2px solid var(--accent-clair);outline-offset:2px}
/* L'erreur garde sa place même vide : sans `min-height`, son apparition
   pousse le bouton vers le bas au moment précis où l'on vient de cliquer
   dessus, et le clic suivant rate sa cible. Cette réserve devient ici
   l'espacement normal entre le groupe et le bouton — d'où des marges
   volontairement serrées autour.

   ⚠️ `min-height` DOIT valoir la hauteur réelle de la ligne, soit
   `line-height` × 1 em — et pas une valeur approchée. À `1.2em` pour une
   interligne de 1,5, la réserve manquait 3,9 px : mesuré le 14/09/2026, la
   carte passait de 391,2 à 395,1 px à l'affichage de l'erreur. Le décalage
   qu'on prétendait supprimer existait donc toujours, en plus petit. On fige
   ici l'interligne pour que les deux valeurs ne puissent plus diverger. */
.erreur{color:var(--danger);text-align:center;font-size:13px;
        line-height:1.5;min-height:1.5em;margin:9px 0 5px;letter-spacing:-.01em}
@media (prefers-reduced-motion:reduce){
  *{transition-duration:.01ms !important}
}
</style></head><body>
<div class="carte">
  <img class="logo" src="static/logo-alpha.png" alt="">
  <h1>Journal alimentaire</h1>
  <p class="sous">Connecte-toi pour reprendre ta journée.</p>
  <form method="post" action="login">
    <!-- Les <label> sont les LIGNES de la liste groupée : le libellé reste
         visible en permanence à gauche. Cliquer le libellé met le champ au
         point, et `autocomplete` est intact — les gestionnaires de mots de
         passe voient exactement la même paire qu'avant.

         Le placeholder ne répète plus le libellé, mais il ne disparaît pas
         pour autant : essayé sans, le 14/09/2026, les deux lignes se lisaient
         comme un MENU et plus comme un formulaire — rien n'indiquait qu'on
         pouvait taper à droite du libellé. « Requis » est le mot qu'Apple
         emploie à cet endroit exact, et il dit quelque chose que le libellé
         ne dit pas. -->
    <div class="champs">
      <label class="rang"><span class="lib">Identifiant</span>
        <input name="utilisateur" placeholder="Requis"
               autocomplete="username" required autofocus></label>
      <label class="rang"><span class="lib">Mot de passe</span>
        <input name="mdp" type="password" placeholder="Requis"
               autocomplete="current-password" required></label>
    </div>
    <!-- L'erreur est SOUS le groupe de champs, pas au-dessus du titre : elle
         désigne ce qui ne va pas, et elle parle de ces deux lignes-là. Placée
         plus haut, sa réserve de hauteur (toujours présente, voir le CSS)
         s'additionnait à la marge du sous-titre et creusait un trou de 44 px
         avant le formulaire.
         ⚠️ La chaîne exacte `<div class="erreur" id="err"></div>` est
         remplacée telle quelle côté serveur en cas d'échec — ne pas la
         reformater. -->
    <div class="erreur" id="err"></div>
    <button type="submit">Se connecter</button>
  </form>
</div></body></html>"""


class AuthMiddleware:
    """Branche APRÈS le retrait de préfixe : voit les chemins INTERNES
    (/api/…, /, /static/…), fonctionne donc pareil en local et derrière
    /nutrition/. Toute route est protégée sauf /login (GET et POST).

    Ordre des middlewares : Starlette exécute le DERNIER ajouté en PREMIER.
    Pour que l'auth voie le chemin interne, PrefixeProxy doit être ajouté
    APRÈS AuthMiddleware (voir plus bas)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chemin = scope["path"]
        if chemin == "/login" or chemin == "/login/":
            await self.app(scope, receive, send)
            return
        # Les assets statiques passent SANS session, et c'est indispensable :
        # la page de login affiche le logo, or elle est par définition la seule
        # page qu'on voit déconnecté. Protégé, /static/logo-alpha.png répondrait
        # un 302 vers /login — le navigateur recevrait du HTML là où il attend
        # un PNG et n'afficherait qu'une image cassée. Le dossier ne contient
        # que du public (logo, zxing.js) : aucune donnée du journal n'y vit.
        if chemin.startswith("/static/"):
            await self.app(scope, receive, send)
            return
        # Ingestion de la balance : PAS de session, et c'est délibéré. Les
        # Raccourcis iOS ne partagent pas les cookies de Safari — exiger la
        # session rendrait l'envoi automatique impossible. La route porte donc
        # sa propre authentification (jeton en en-tête, voir `pesee_balance`).
        #
        # ⚠️ Cette exemption est la seule porte de l'app qui s'ouvre sans
        # cookie sur une route qui ÉCRIT. Vérifié le 14/09/2026 :
        # `tailscale funnel status` dit Funnel ON, donc /nutrition est joignable
        # depuis l'internet public et pas seulement depuis le tailnet. Ne jamais
        # élargir ce préfixe à un dossier : il doit désigner UNE route exacte.
        if chemin == "/api/pesee-balance":
            await self.app(scope, receive, send)
            return
        try:
            cfg = auth_cfg()
            token = self._cookie(scope)
            if token and _token_valide(token, cfg["secret"]):
                await self.app(scope, receive, send)
                return
        except Exception:
            pass
        # Le navigateur est derrière /nutrition/ : le Location doit porter le
        # préfixe, sinon il tombe sur la racine du domaine (une autre app).
        cible = PREFIX + "/login"
        if chemin.startswith("/api/"):
            # Un fetch ne sait pas afficher une page de login : plutôt qu'un
            # 302 silencieux suivi d'une erreur de parsing JSON, on renvoie un
            # 401 que le frontend intercepte (window.fetch wrappé dans
            # index.html) pour rediriger vers /login.
            await JSONResponse({"erreur": "non authentifié"}, status_code=401)(scope, receive, send)
            return
        await RedirectResponse(cible, status_code=302)(scope, receive, send)

    @staticmethod
    def _cookie(scope) -> str | None:
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode("latin-1").split(";"):
                    if part.strip().startswith(COOKIE + "="):
                        return part.strip()[len(COOKIE) + 1:]
        return None


app.add_middleware(AuthMiddleware)


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return PAGE_LOGIN


@app.post("/login")
def login_post(utilisateur: str = Form(""), mdp: str = Form("")):
    cfg = auth_cfg()
    if utilisateur != cfg["utilisateur"] or not _verifie_mdp(mdp, cfg):
        return HTMLResponse(PAGE_LOGIN.replace(
            '<div class="erreur" id="err"></div>',
            '<div class="erreur" id="err">Identifiant ou mot de passe incorrect.</div>'
        ), status_code=401)
    resp = RedirectResponse(PREFIX + "/", status_code=303)
    # path=/ : le cookie couvre à la fois la racine locale et /nutrition/.
    resp.set_cookie(COOKIE, _nouveau_token(cfg["secret"]),
                    max_age=int(DUREE_SESSION.total_seconds()),
                    httponly=True, samesite="lax", path="/")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(PREFIX + "/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp

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

        # Plats en routine, pour marquer les lignes qui en viennent. La clé est
        # la MÊME que celle du dédoublonnage de POST /api/recurrents —
        # (source, ref ou nom, repas) — sinon l'icône et la création d'un
        # récurrent ne parleraient pas du même plat. Un aliment maison a
        # souvent ref NULL : la clé retombe alors sur le nom.
        routines = {(r["source"], r["ref"] or r["nom"], r["repas"]): r["id"]
                    for r in c.execute("""select id, source, ref, nom, repas
                                        from repas_recurrents where actif = 1""")}

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
                SELECT id, nom, grammes, energie_kcal, proteines, glucides, lipides,
                       horodatage, source, ref
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
                    "source": l["source"],
                    "ref": l["ref"],
                    # id du plat de routine dont cette ligne est une occurrence,
                    # ou None. Le front s'en sert pour poser l'icône 🔁 et pour
                    # ne pas proposer d'épingler ce qui l'est déjà.
                    "routine": routines.get(
                        (l["source"], l["ref"] or l["nom"], g["repas"])),
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
        lignes = []
        for row in cur.fetchall():
            l = dict(row)
            l["routine"] = routines.get(
                (l["source"], l["ref"] or l["nom"], l["repas"]))
            lignes.append(l)

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


@app.patch("/api/journal/{id}")
def modifier_journal(id: int, item: dict) -> dict:
    """Corrige une ligne DÉJÀ enregistrée : portion, repas, libellé.

    Jusqu'ici la seule correction possible était « supprimer et ressaisir » :
    une pesée notée 150 g au lieu de 250 coûtait le parcours d'ajout complet,
    scan compris. C'est le geste qu'on évite ici, pas une nouvelle saisie.

    Les valeurs nutritionnelles ne se saisissent pas : elles sont **recalculées
    depuis la ligne elle-même**, ramenée à 100 g. Laisser changer le grammage
    sans recalculer les kcal produirait une ligne qui se contredit — exactement
    l'erreur qu'on vient réparer. Et on ne relit PAS la source : une ligne Open
    Food Facts n'a pas de fiche locale, et une fiche CIQUAL corrigée depuis la
    saisie réécrirait rétroactivement un repas déjà mangé.

    Corriger les VALEURS d'un aliment reste le rôle de sa fiche (aliment
    maison), pas celui d'une ligne de journal.

    Le lien avec un plat en routine n'est pas touché : changer la portion
    d'aujourd'hui ne dit rien de la portion habituelle. Le grammage de
    référence se règle dans l'onglet Routine.
    """
    with db() as c:
        l = c.execute("select * from journal where id = ?", (id,)).fetchone()
        if not l:
            raise HTTPException(404, "ligne introuvable")

        champs, vals = [], []

        if "repas" in item:
            if item["repas"] not in LIBELLES:
                raise HTTPException(400, "repas inconnu")
            champs.append("repas = ?"); vals.append(item["repas"])

        if "nom" in item:
            nom = (item["nom"] or "").strip()
            if len(nom) < 2:
                raise HTTPException(400, "nom trop court")
            champs.append("nom = ?"); vals.append(nom)

        if "grammes" in item:
            try:
                g = float(item["grammes"])
            except (TypeError, ValueError):
                raise HTTPException(400, "grammage invalide")
            if g <= 0:
                raise HTTPException(400, "grammage nul")
            ancien = l["grammes"] or 0
            if ancien <= 0:
                raise HTTPException(400, "grammage d'origine inconnu, recalcul impossible")
            champs.append("grammes = ?"); vals.append(g)
            for k in CHAMPS:
                if l[k] is not None:
                    champs.append(f"{k} = ?"); vals.append(round(l[k] * g / ancien, 1))

        if not champs:
            raise HTTPException(400, "rien à modifier")

        c.execute(f"update journal set {', '.join(champs)} where id = ?", (*vals, id))
        c.commit()
        r = c.execute("select * from journal where id = ?", (id,)).fetchone()
    return {"ok": True, "ligne": dict(r)}


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


@app.delete("/api/recurrents/{rid}/definitif")
def supprimer_recurrent(rid: int) -> dict:
    """Supprime pour de bon un plat de routine, décisions comprises.

    Distinct de `DELETE /api/recurrents/{rid}`, qui met seulement en pause :
    une pause se reprend, celle-ci ne se reprend pas. Les deux existent parce
    que « je n'en mange plus cette semaine » et « je n'en mangerai plus » ne
    demandent pas le même geste, et confondre les deux ferait perdre un plat
    qu'on voulait juste suspendre.

    Les LIGNES DE JOURNAL déjà créées par ce plat ne sont pas touchées : elles
    racontent des repas réellement mangés, et les effacer réécrirait
    l'historique (et les totaux) de journées passées.
    """
    with db() as c:
        if not c.execute("select 1 from repas_recurrents where id = ?", (rid,)).fetchone():
            raise HTTPException(404, "récurrent introuvable")
        c.execute("delete from recurrents_jours where recurrent_id = ?", (rid,))
        c.execute("delete from repas_recurrents where id = ?", (rid,))
        c.commit()
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


# ─────────────────────── Balance connectée (Kamtron / Feelfit) ─────────────
# Chaîne : balance Kamtron → app Feelfit → Santé iOS → Raccourci → ICI.
#
# Pourquoi une table à nous et pas le miroir GarminDB : le miroir est un
# *miroir*. Sa table `weight` n'a que deux colonnes (`day`, `weight`) — le
# gras et la masse maigre n'y ont aucune place — et surtout tout ce qu'on y
# écrirait serait à la merci du prochain téléchargement. La preuve est dans
# `scripts/weight-filter.py` : la fausse pesée du 28/08 revient à CHAQUE
# re-téléchargement et il faut un script pour la retuer. On garde donc le
# miroir en lecture seule et on écrit chez nous, ce qui préserve en prime la
# provenance : on sait toujours si une pesée vient de Garmin ou de la balance.
JETON_MIN = 20          # un jeton plus court trahit une config bâclée
POIDS_MIN, POIDS_MAX = 30.0, 300.0
DELTA_MAX_KG = 3.0      # écart toléré avec la dernière pesée connue


def _nombre_sante(brut) -> float | None:
    """Extrait un nombre de ce que les Raccourcis iOS savent produire.

    Écrit le 14/09/2026 après un « sauf erreur, rechercher dans Santé ne
    permet pas d'extraire les données » de JB — il avait raison. Un
    `float(x)` sur la sortie de « Rechercher des échantillons de santé » ne
    marche pas, pour quatre raisons cumulables :

      · l'action rend du TEXTE, pas un nombre ;
      · plusieurs échantillons arrivent COLLÉS, séparés par des `\\n` ;
      · l'iPhone de JB est en français → séparateur décimal VIRGULE ;
      · l'unité peut être accolée (« 83,4 kg »).

    On prend la PREMIÈRE ligne exploitable : le raccourci trie par date
    décroissante, donc c'est la pesée la plus récente. Prendre le max serait
    tentant et faux — ce serait systématiquement la plus lourde du lot.
    """
    if brut is None:
        return None
    if isinstance(brut, (int, float)):
        return float(brut)
    for ligne in str(brut).replace("\r", "\n").split("\n"):
        # On isole le premier motif numérique : « 83,4 kg » → « 83,4 ».
        m = re.search(r"-?\d+(?:[.,]\d+)?", ligne)
        if m:
            try:
                return float(m.group(0).replace(",", "."))
            except ValueError:
                continue
    return None


_MOIS_FR = {"janv": 1, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
            "juil": 7, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def _jour_sante(brut) -> str | None:
    """Date d'une pesée, depuis ce que « Date actuelle » produit réellement.

    Découvert le 14/09/2026 en lisant le journal brut : le raccourci de JB
    n'envoie PAS de l'ISO mais « 14 sept. 2026 à 19:00 », le format long
    français. L'ancien code prenait les 10 premiers caractères, obtenait
    « 14 sept. 2 », ne reconnaissait rien et retombait sur aujourd'hui — donc
    le champ était décoratif. Ça marchait par chance : une pesée à 23h50
    envoyée à 00h05 aurait été datée du mauvais jour, sans un mot.

    On accepte donc les deux écritures, ISO d'abord.
    """
    if not brut:
        return None
    s = str(brut).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        a, mo, j = (int(x) for x in m.groups())
    else:
        # « 14 sept. 2026 », « 1er août 2026 », « 14 septembre 2026 »…
        # Accents retirés pour que « févr./déc./août » tombent sur les clés.
        plat = (s.lower()
                .replace("é", "e").replace("è", "e").replace("û", "u")
                .replace("ô", "o").replace("î", "i").replace("ï", "i"))
        m = re.search(r"(\d{1,2})\s*(?:er)?\s+([a-z]+)\.?\s+(\d{4})", plat)
        if not m:
            return None
        cle = next((k for k in _MOIS_FR if m.group(2).startswith(k)), None)
        if cle is None:
            return None
        j, mo, a = int(m.group(1)), _MOIS_FR[cle], int(m.group(3))
    try:
        return date(a, mo, j).isoformat()
    except ValueError:
        return None


def _pesee_precedente(c: sqlite3.Connection, jour: str) -> tuple[str, float] | None:
    """Dernière pesée connue AVANT `jour`, toutes sources confondues."""
    lignes = list(c.execute(
        "select jour, poids_kg from pesees where jour < ? order by jour desc limit 1",
        (jour,)))
    try:
        with sqlite3.connect(f"file:{GARMIN_DB}?mode=ro", uri=True) as g:
            lignes += [(str(r[0])[:10], r[1]) for r in g.execute(
                "select day, weight from weight where date(day) < ? "
                "order by day desc limit 1", (jour,))]
    except sqlite3.Error:
        pass
    return max(lignes, key=lambda r: r[0]) if lignes else None


@app.post("/api/pesee-balance")
async def pesee_balance(request: Request) -> JSONResponse:
    """Reçoit une pesée de la balance connectée. Authentifié par jeton.

    Le jeton passe par l'en-tête `X-Jeton` et NON par l'URL : une URL se
    retrouve dans les journaux du proxy, dans ceux de Tailscale et dans
    l'historique du navigateur — un secret n'a rien à y faire. Comparaison en
    temps constant (`compare_digest`), comme pour le mot de passe.
    """
    try:
        cfg = auth_cfg()
        attendu = cfg.get("jeton_balance") or ""
    except Exception:
        return JSONResponse({"erreur": "configuration illisible"}, status_code=500)
    if len(attendu) < JETON_MIN:
        return JSONResponse({"erreur": "jeton non configuré"}, status_code=500)

    fourni = request.headers.get("x-jeton", "")
    if not compare_digest(fourni, attendu):
        return JSONResponse({"erreur": "jeton invalide"}, status_code=401)

    # Corps lu en BRUT avant tout décodage, et journalisé tel quel. Mis en
    # place le 14/09/2026 : la première vraie pesée a enregistré 83,0 pour une
    # pesée à 83,5, et j'ai affirmé à JB que Shortcuts tronquait — il a répondu
    # que le raccourci envoyait bien « 83.5 ». Deux hypothèses contradictoires
    # et aucune preuve : ce journal tranche, et il tranche sur les octets.
    #
    # Le jeton voyage dans l'EN-TÊTE, jamais dans le corps : ce fichier ne peut
    # donc pas contenir de secret. Il est malgré tout en 0600.
    brut = await request.body()
    try:
        JOURNAL_PESEES.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_PESEES.open("a", encoding="utf-8") as f:
            f.write("%s  ct=%s  len=%d  %r\n" % (
                datetime.now().isoformat(timespec="seconds"),
                request.headers.get("content-type", "?"),
                len(brut), brut[:600]))
        JOURNAL_PESEES.chmod(0o600)
    except OSError:
        pass

    try:
        mesure = json.loads(brut.decode("utf-8"))
        if not isinstance(mesure, dict):
            raise ValueError("objet JSON attendu")
    except (UnicodeDecodeError, ValueError) as e:
        # On répond le corps reçu : c'est ce qui permet de diagnostiquer depuis
        # le téléphone, sans avoir à venir lire le journal sur le serveur.
        return JSONResponse({"erreur": f"JSON illisible : {e}",
                             "recu": brut[:200].decode("utf-8", "replace")},
                            status_code=400)

    # ── Validation ────────────────────────────────────────────────────────
    # Le parsing est volontairement TOLÉRANT (voir `_nombre_sante`) : c'est
    # nous qui nous adaptons à ce que Shortcuts sait produire, pas l'inverse.
    # Exiger un nombre propre revenait à exiger de JB un travail de parsing
    # dans une app où il n'est pas faisable.
    poids = _nombre_sante(mesure.get("poids_kg"))
    if poids is None:
        return JSONResponse(
            {"erreur": "poids_kg manquant ou illisible",
             "recu": str(mesure.get("poids_kg"))[:120]}, status_code=400)

    # Détection de la décimale perdue. Arrivé le 14/09/2026 sur la toute
    # première vraie pesée : JB monte sur la balance à 83,5 et la base
    # enregistre 83,0. La décimale n'est pas perdue ici — elle l'est DANS
    # Shortcuts : son champ JSON était typé « Nombre », il a converti la
    # chaîne française « 83,5 » en nombre et tronqué à la virgule.
    #
    # On ne peut pas le corriger à distance (83 est un poids valide), mais on
    # peut le DIRE : le raccourci affiche la réponse, donc l'avertissement
    # atterrit sur son téléphone au lieu de dormir dans une table. Une pesée
    # ronde au gramme près est improbable — l'annoncer coûte un faux positif
    # de temps en temps, le taire coûte une courbe de poids fausse.
    avertissement = None
    if isinstance(mesure.get("poids_kg"), (int, float)) and float(poids).is_integer():
        avertissement = ("poids entier reçu (%g) : le champ JSON poids_kg est "
                         "probablement typé « Nombre » au lieu de « Texte », "
                         "ce qui tronque la décimale. Vérifie la pesée."
                         % poids)

    # Garde-fou d'unité : Santé rend le poids dans l'unité de l'utilisateur.
    # Réglé en livres, le raccourci enverrait ~184 pour 83,4 kg — refusé ici
    # plutôt qu'enregistré comme un gain de 100 kg.
    if not (POIDS_MIN <= poids <= POIDS_MAX):
        return JSONResponse(
            {"erreur": f"poids hors plage ({POIDS_MIN}–{POIDS_MAX} kg) : {poids}. "
                       "Santé est peut-être réglé en livres."}, status_code=400)

    gras = _nombre_sante(mesure.get("gras_pct"))
    # Santé rend la masse grasse en FRACTION (0,224) alors que la balance et
    # l'écran parlent en pourcentage (22,4). On accepte les deux et on
    # normalise ici, sinon un 0,224 s'enregistrerait comme « 0,2 % ».
    if gras is not None and 0.0 < gras <= 1.0:
        gras = gras * 100
    if gras is not None and not (1.0 <= gras <= 70.0):
        gras = None
    gras = round(gras, 1) if gras is not None else None
    muscle = _nombre_sante(mesure.get("muscle_kg"))
    if muscle is not None and not (10.0 <= muscle <= 150.0):
        muscle = None

    # L'horodatage ne sert qu'à dater la pesée ; en cas d'absence ou de format
    # exotique on prend aujourd'hui plutôt que de refuser une mesure valide.
    jour = _jour_sante(mesure.get("horodatage")) or date.today().isoformat()
    if jour > date.today().isoformat():
        return JSONResponse({"erreur": f"pesée dans le futur : {jour}"},
                            status_code=400)

    with sqlite3.connect(str(DB)) as c:
        # Cohérence : on se compare à la DERNIÈRE pesée connue, pas au poids du
        # profil. La règle des 8 % de `stats.py` a été mesurée le 14/09/2026 et
        # elle est fausse dans les deux sens : elle REJETTE 75 kg (l'objectif,
        # 10,07 % d'écart) et ACCEPTE la fausse pesée de 80 kg (4,08 %). Un
        # écart au dernier poids connu, lui, reste juste quel que soit le poids.
        prec = _pesee_precedente(c, jour)
        if prec and abs(poids - prec[1]) > DELTA_MAX_KG:
            return JSONResponse(
                {"erreur": f"écart de {abs(poids - prec[1]):.1f} kg avec la pesée "
                           f"du {prec[0]} ({prec[1]} kg) — refusé au-delà de "
                           f"{DELTA_MAX_KG} kg",
                 "poids_recu": poids}, status_code=409)

        # Une pesée = une ligne par jour. Se repeser trois fois dans la matinée
        # doit corriger la valeur du jour, pas empiler trois lignes.
        avant = list(c.execute("select poids_kg from pesees where jour = ?", (jour,)))
        c.execute(
            "insert into pesees (jour, poids_kg, gras_pct, muscle_kg, source, recu_a) "
            "values (?,?,?,?,?,?) "
            "on conflict(jour) do update set "
            "  poids_kg=excluded.poids_kg, gras_pct=excluded.gras_pct, "
            "  muscle_kg=excluded.muscle_kg, source=excluded.source, "
            "  recu_a=excluded.recu_a",
            (jour, round(poids, 2), gras, muscle,
             str(mesure.get("source") or "kamtron")[:32],
             datetime.now().isoformat(timespec="seconds")))
        c.commit()

    reponse = {"ok": True, "jour": jour, "poids_kg": round(poids, 2),
               "gras_pct": gras, "muscle_kg": muscle, "remplace": bool(avant)}
    if avertissement:
        reponse["avertissement"] = avertissement
    return JSONResponse(reponse)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("NUTRITION_PORT", 8090)),
    )