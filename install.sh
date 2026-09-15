#!/usr/bin/env bash
# Installs `droid` as a global command: a private venv plus a link in ~/.local/bin.
# PYTHON=/path/to/python3.x forces an interpreter; otherwise the first Python 3.9+ found is used, trying python3 first, then python3.13, 3.12, 3.11 and the Homebrew kegs.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
MIN_MINOR=9

minor_of() {
  "$1" -c 'import sys; print(sys.version_info.minor if sys.version_info.major == 3 else -1)' 2>/dev/null || echo -1
}

hint() {
  echo "  En macOS: brew install python@3.11 (queda en /opt/homebrew/opt/python@3.11/bin)" >&2
  echo "  En Linux: apt install python3.11 python3.11-venv, o pyenv" >&2
  echo "  Luego: PYTHON=/ruta/a/python3.11 ./install.sh" >&2
}

if [ -n "${PYTHON:-}" ]; then
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "✖ PYTHON=$PYTHON no existe o no es ejecutable." >&2; hint; exit 1
  fi
  if [ "$(minor_of "$PYTHON")" -lt "$MIN_MINOR" ]; then
    echo "✖ PYTHON=$PYTHON no es Python 3.${MIN_MINOR}+ ($("$PYTHON" --version 2>&1 || echo 'no responde'))." >&2; hint; exit 1
  fi
  PY="$PYTHON"
else
  PY=""
  for c in python3 python3.13 python3.12 python3.11 \
           /opt/homebrew/opt/python@3.13/bin/python3.13 /opt/homebrew/opt/python@3.12/bin/python3.12 /opt/homebrew/opt/python@3.11/bin/python3.11 \
           /usr/local/opt/python@3.11/bin/python3.11; do
    command -v "$c" >/dev/null 2>&1 || continue
    if [ "$(minor_of "$c")" -ge "$MIN_MINOR" ]; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "✖ droid necesita Python 3.${MIN_MINOR} o superior y no encuentro ninguno en PATH ni en las rutas de Homebrew." >&2; hint; exit 1
  fi
fi

echo "› Python: $PY ($("$PY" --version 2>&1))"
echo "› Creando entorno en $DIR/.venv"
"$PY" -m venv --clear "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
echo "› Instalando droid (editable) y dependencias"
"$DIR/.venv/bin/pip" install -q -e "$DIR"

mkdir -p "$BIN_DIR"
ln -sf "$DIR/.venv/bin/droid" "$BIN_DIR/droid"
echo "✔ Enlace: $BIN_DIR/droid → $DIR/.venv/bin/droid"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    case "$(basename "${SHELL:-zsh}")" in
      bash) RC="${HOME}/.bashrc" ;;
      *) RC="${HOME}/.zshrc" ;;
    esac
    if ! grep -qs 'HOME/.local/bin' "$RC"; then
      printf '\n# droid CLI\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
      echo "✔ Añadido ~/.local/bin al PATH en $RC (abre una terminal nueva)"
    fi
    ;;
esac

mkdir -p "$HOME/.droid/logs" "$HOME/.droid/run"
echo "✔ droid instalado. Prueba: droid ls"
