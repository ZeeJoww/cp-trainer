#!/usr/bin/env sh
set -eu

PREFIX=${PREFIX:-"$HOME/.local"}
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_DIR="$PREFIX/share/cp-trainer-local"
BIN_DIR="$PREFIX/bin"

mkdir -p "$APP_DIR" "$BIN_DIR"
rm -rf "$APP_DIR/cptrainer"
cp -R "$SOURCE_DIR/cptrainer" "$APP_DIR/cptrainer"
cat > "$BIN_DIR/cp-trainer" <<SCRIPT
#!/usr/bin/env sh
set -eu
PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m cptrainer "\$@"
SCRIPT
chmod +x "$BIN_DIR/cp-trainer"
printf 'Installed cp-trainer to %s\n' "$BIN_DIR/cp-trainer"
printf 'Ensure %s is on PATH.\n' "$BIN_DIR"
