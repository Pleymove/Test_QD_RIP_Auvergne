#!/usr/bin/env bash
# Installation du plugin QD RIP Auvergne dans QGIS 3
# Usage: bash install_plugin.sh

PLUGIN_NAME="QD_RIP_Auvergne_QC"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/$PLUGIN_NAME"

# Détection du dossier plugins QGIS
if [[ "$OSTYPE" == "darwin"* ]]; then
    DEST="$HOME/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins"
else
    DEST="$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins"
fi

echo "Source      : $SRC"
echo "Destination : $DEST/$PLUGIN_NAME"

if [ ! -d "$SRC" ]; then
    echo "ERREUR : dossier source introuvable : $SRC"
    exit 1
fi

mkdir -p "$DEST"

# Supprimer l'ancienne version si elle existe
if [ -d "$DEST/$PLUGIN_NAME" ]; then
    echo "Suppression de l'ancienne version…"
    rm -rf "$DEST/$PLUGIN_NAME"
fi

cp -r "$SRC" "$DEST/$PLUGIN_NAME"
echo ""
echo "✓ Plugin installé dans : $DEST/$PLUGIN_NAME"
echo ""
echo "Dans QGIS :"
echo "  1. Menu Extensions > Gérer et installer des extensions"
echo "  2. Onglet « Installées » → cocher « QD RIP Auvergne »"
echo "  3. (ou) Menu Extensions > QD RIP Auvergne"
echo ""
echo "Si QGIS est déjà ouvert, rechargez le plugin via :"
echo "  Menu Extensions > Recharger tous les plugins (ou Plugin Reloader)"
