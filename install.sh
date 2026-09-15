#!/usr/bin/env bash
# Instala `droid` como comando global (venv propio + enlace en ~/.local/bin).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
BIN_DIR="${HOME}/.local/bin"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✖ No encuentro python3. Instálalo (brew install python) y vuelve a ejecutar." >&2; exit 1
fi

echo "› Creando entorno en $DIR/.venv"
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
echo "› Instalando droid (editable) y dependencias"
"$DIR/.venv/bin/pip" install -q -e "$DIR"

mkdir -p "$BIN_DIR"
ln -sf "$DIR/.venv/bin/droid" "$BIN_DIR/droid"
echo "✔ Enlace: $BIN_DIR/droid → $DIR/.venv/bin/droid"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    RC="${HOME}/.zshrc"
    if ! grep -qs 'HOME/.local/bin' "$RC"; then
      printf '\n# droid CLI\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
      echo "✔ Añadido ~/.local/bin al PATH en $RC (abre una terminal nueva)"
    fi
    ;;
esac

mkdir -p "$HOME/.droid/logs" "$HOME/.droid/run"
echo "✔ droid instalado. Prueba: droid ls"
