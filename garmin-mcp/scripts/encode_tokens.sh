#!/usr/bin/env bash
# encode_tokens.sh
# ------------------------------------------------------------------
# Skapar base64-strängar av Garmin OAuth-tokens som ska klistras in
# som GitHub repository secrets (GARMIN_OAUTH1_TOKEN, GARMIN_OAUTH2_TOKEN).
#
# Kör en gång efter lyckad lokal inloggning. Tokens hittas normalt i
# ~/.garminconnect/ – ändra TOKEN_DIR om du satt GARMIN_TOKEN_DIR till
# något annat.
#
#     ./scripts/encode_tokens.sh
#
# ------------------------------------------------------------------
set -euo pipefail

TOKEN_DIR="${1:-$HOME/.garminconnect}"

if [ ! -f "$TOKEN_DIR/oauth1_token.json" ] || [ ! -f "$TOKEN_DIR/oauth2_token.json" ]; then
  echo "Hittar inte tokens i $TOKEN_DIR"
  echo "  Förväntade filer: oauth1_token.json, oauth2_token.json"
  echo
  echo "Kör 'python test_connection.py' först för att skapa dem."
  exit 1
fi

# Portable base64 utan radbrytningar (macOS + Linux)
encode() {
  base64 < "$1" | tr -d '\n'
}

echo "Kopiera värdena nedan till GitHub:"
echo "  Settings → Secrets and variables → Actions → New repository secret"
echo "============================================================"
echo
echo ">>> GARMIN_OAUTH1_TOKEN"
encode "$TOKEN_DIR/oauth1_token.json"
echo
echo
echo ">>> GARMIN_OAUTH2_TOKEN"
encode "$TOKEN_DIR/oauth2_token.json"
echo
echo
echo "============================================================"
echo "Klart. När du också lagt till de övriga secrets (se README)"
echo "kan du köra 'Run workflow' från GitHub-appen på mobilen."
