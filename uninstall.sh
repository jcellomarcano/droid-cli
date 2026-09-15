#!/usr/bin/env bash
# Quita el comando global. La caché de logs en ~/.droid se conserva (bórrala a mano si quieres).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -f "$HOME/.local/bin/droid"
rm -rf "$DIR/.venv"
echo "✔ droid desinstalado (caché conservada en ~/.droid)"
