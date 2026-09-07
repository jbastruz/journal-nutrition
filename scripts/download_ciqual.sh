#!/usr/bin/env bash
# Télécharge la base CIQUAL (ANSES) et construit ciqual.db
# Licence : https://ciqual.anses.fr/ (usage libre avec mention de la source)
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="https://pro.anses.fr/tableciqual/Documents"
for f in alim_2020_07_07.xml compo_2020_07_07.xml const_2020_07_07.xml          sources_2020_07_07.xml alim_grp_2020_07_07.xml; do
    [ -f "$f" ] || curl -L -o "$f" "$BASE_URL/$f"
done

python3 scripts/ciqual-load.py
echo "ciqual.db prêt."
