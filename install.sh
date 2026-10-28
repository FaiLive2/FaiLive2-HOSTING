#!/usr/bin/env bash
# =============================================================================
#  FaiLive2 Hosting Panel — Installer / Updater
#  Runs in Ubuntu PRoot on Android Termux (no Docker/LXC/Snap/systemd)
# =============================================================================
set -uo pipefail

# ---------------------------------------------------------------------------
#  CONFIGURE YOUR GITHUB REPOSITORY HERE  <-- EDIT THIS ONE LINE
#  Must point to the RAW base (no trailing slash, no /web.py).
# ---------------------------------------------------------------------------
GITHUB_REPO_RAW="https://raw.githubusercontent.com/YOURUSER/FaiLive2-Hosting/main"

PANEL_HOST="127.0.0.1"
PANEL_PORT="8080"
APP_DIR="${HOME}/failive2"
VENV_DIR="${APP_DIR}/venv"
WEB_PY="${APP_DIR}/web.py"
DB_FILE="${APP_DIR}/data/failive2.db"
SECRET_FILE="${APP_DIR}/data/secret.key"

# ---------------------------------------------------------------------------
# Colors (safe fallback if tput missing)
# ---------------------------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_RESET="$(tput sgr0)";  C_BOLD="$(tput bold)"
  C_PURPLE="$(tput setaf 5)"; C_VIOLET="$(tput setaf 13)"
  C_CYAN="$(tput setaf 6)";   C_GREEN="$(tput setaf 2)"
  C_RED="$(tput setaf 1)";    C_YELLOW="$(tput setaf 3)"
  C_DIM="$(tput dim)"
else
  C_RESET=""; C_BOLD=""; C_PURPLE=""; C_VIOLET=""; C_CYAN=""
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_DIM=""
fi

banner() {
  echo
  echo "${C_PURPLE}${C_BOLD}  ███████╗ █████╗ ██╗██╗     ██╗██╗   ██╗███████╗██████╗ ${C_RESET}"
  echo "${C_PURPLE}${C_BOLD}  ██╔════╝██╔══██╗██║██║     ██║██║   ██║██╔════╝╚════██╗${C_RESET}"
  echo "${C_VIOLET}${C_BOLD}  █████╗  ███████║██║██║     ██║██║   ██║█████╗   █████╔╝${C_RESET}"
  echo "${C_VIOLET}${C_BOLD}  ██╔══╝  ██╔══██║██║██║     ██║╚██╗ ██╔╝██╔══╝   ╚═══██╗${C_RESET}"
  echo "${C_CYAN}${C_BOLD}  ██║     ██║  ██║██║███████╗██║ ╚████╔╝ ███████╗██████╔╝${C_RESET}"
  echo "${C_CYAN}${C_BOLD}  ╚═╝     ╚═╝  ╚═╝╚═╝╚══════╝╚═╝  ╚═══╝  ╚══════╝╚═════╝ ${C_RESET}"
  echo
  echo "${C_VIOLET}        FaiLive2 Hosting Panel — Installer / Updater${C_RESET}"
  echo "${C_DIM}        Termux PRoot · Ubuntu · SQLite · Flask${C_RESET}"
  echo
}

step() { echo; echo "${C_CYAN}${C_BOLD}▶ $*${C_RESET}"; }
ok()   { echo "${C_GREEN}  ✔ $*${C_RESET}"; }
warn() { echo "${C_YELLOW}  ⚠ $*${C_RESET}"; }
fail() { echo "${C_RED}${C_BOLD}  ✖ $*${C_RESET}"; }
info() { echo "${C_DIM}  · $*${C_RESET}"; }
die()  { fail "$*"; echo; warn "Installation aborted."; exit 1; }

spin() {
  local pid="$1"; local label="$2"; local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
  local i=0
  while kill -0 "$pid" 2>/dev/null; do
    printf "\r  ${C_PURPLE}%s${C_RESET} %s" "${frames:$((i % 10)):1}" "$label"
    i=$((i + 1)); sleep 0.1
  done
  printf "\r  ${C_GREEN}✔${C_RESET} %s\n" "$label"
}

run_quiet() {
  local label="$1"; local log="$2"; shift 2
  ( "$@" ) >"$log" 2>&1 &
  local pid=$!
  spin "$pid" "$label"
  wait "$pid"; local rc=$?
  if [ $rc -ne 0 ]; then
    fail "$label failed (exit $rc)"
    echo "${C_RED}---------------- log tail ----------------${C_RESET}"
    tail -n 40 "$log" || true
    echo "${C_RED}------------------------------------------${C_RESET}"
    return $rc
  fi
  return 0
}

progress() {
  local cur="$1"; local tot="$2"; local label="$3"; local width=30
  local filled=$(( cur * width / tot )); local empty=$(( width - filled ))
  local bar
  bar="$(printf '%*s' "$filled" '' | tr ' ' '█')$(printf '%*s' "$empty" '' | tr ' ' '░')"
  echo "${C_PURPLE}  [${bar}${C_PURPLE}] ${C_CYAN}${cur}/${tot}${C_RESET} ${label}"
}

TOTAL_STEPS=9
CUR_STEP=0
next_step() { CUR_STEP=$((CUR_STEP + 1)); progress "$CUR_STEP" "$TOTAL_STEPS" "$1"; }

# ---------------------------------------------------------------------------
banner

if grep -qi "proot" /proc/1/cmdline 2>/dev/null || [ -d "/data/data/com.termux" ] || [ -n "${PROOT_TMP_DIR:-}" ]; then
  echo "${C_YELLOW}  Detected Termux PRoot environment.${C_RESET}"
  echo "${C_DIM}  · cgroups / CPU / RAM enforcement NOT available${C_RESET}"
  echo "${C_DIM}  · Local processes only — not isolated VPS containers${C_RESET}"
  echo "${C_DIM}  · Public UDP (Bedrock) may be unreachable externally${C_RESET}"
fi

UPDATE_MODE=0
if [ -f "$WEB_PY" ] && [ -f "$DB_FILE" ]; then
  echo
  echo "${C_VIOLET}  Existing installation detected at:${C_RESET} ${C_BOLD}${APP_DIR}${C_RESET}"
  if [ -t 0 ]; then
    read -r -p "  Update (keep data) or reinstall? [U]pdate / [R]einstall / [A]bort: " choice
    case "${choice,,}" in
      u|update) UPDATE_MODE=1 ;;
      r|reinstall) UPDATE_MODE=0 ;;
      *) echo "${C_YELLOW}  Aborted.${C_RESET}"; exit 0 ;;
    esac
  else
    UPDATE_MODE=1
  fi
fi

mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/apps" || die "Cannot create $APP_DIR"

# ---------- Step 1: prerequisites ----------
next_step "Checking prerequisites"
APT_UPDATED=0
ensure_pkg() {
  local cmd="$1"; local pkg="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      if [ $APT_UPDATED -eq 0 ]; then
        run_quiet "apt-get update" "${APP_DIR}/logs/apt.log" apt-get update || true
        APT_UPDATED=1
      fi
      run_quiet "Installing $pkg" "${APP_DIR}/logs/apt-$pkg.log" \
        apt-get install -y "$pkg" || die "Could not install $pkg"
    else
      die "Missing '$cmd' and apt-get unavailable. Install '$pkg' manually."
    fi
  fi
}
ensure_pkg git git || true
ensure_pkg python3 python3 || true
ensure_pkg pip3 python3-pip || true
ensure_pkg curl curl || true
ensure_pkg unzip unzip || true
ensure_pkg gcc gcc || true
ensure_pkg python3-dev python3-dev || true
ensure_pkg libffi-dev libffi-dev || true
ok "Prerequisites available"

# ---------- Step 2: virtualenv ----------
next_step "Creating virtual environment"
if [ ! -x "${VENV_DIR}/bin/python" ]; then
  run_quiet "python3 -m venv" "${APP_DIR}/logs/venv.log" python3 -m venv "$VENV_DIR" \
    || die "Failed to create venv (try: apt-get install python3-venv)"
fi
PY="${VENV_DIR}/bin/python"
ok "Virtual environment: $VENV_DIR"

# ---------- Step 3: dependencies ----------
next_step "Installing Python dependencies"
"$PY" -m pip install --upgrade pip setuptools wheel >"${APP_DIR}/logs/pip.log" 2>&1 \
  || warn "pip self-upgrade failed (continuing)"
DEPS="flask psutil argon2-cffi werkzeug itsdangerous"
if run_quiet "pip install (core)" "${APP_DIR}/logs/pip-deps.log" \
     "$PY" -m pip install $DEPS; then
  ok "Dependencies installed"
else
  die "Could not install Python dependencies."
fi

# ---------- Step 4: download web.py ----------
next_step "Fetching web.py"
DL_URL="${GITHUB_REPO_RAW%/}/web.py"
rm -f "${APP_DIR}/web.py.new"
if run_quiet "Downloading web.py" "${APP_DIR}/logs/download.log" \
     curl -fsSL -o "${APP_DIR}/web.py.new" "$DL_URL"; then
  if [ -s "${APP_DIR}/web.py.new" ] && head -n1 "${APP_DIR}/web.py.new" | grep -q '^#!.*python'; then
    mv "${APP_DIR}/web.py.new" "$WEB_PY"
    ok "Downloaded from ${DL_URL}"
  else
    rm -f "${APP_DIR}/web.py.new"
    if [ -f "$WEB_PY" ]; then
      warn "Download invalid; keeping existing web.py"
    else
      die "Downloaded file invalid. Check GITHUB_REPO_RAW ($DL_URL)."
    fi
  fi
else
  if [ -f "$WEB_PY" ]; then
    warn "Download failed; keeping existing web.py"
  else
    die "Could not download web.py from $DL_URL"
  fi
fi

# ---------- Step 5: secret key ----------
next_step "Secret key"
mkdir -p "$(dirname "$SECRET_FILE")"
if [ ! -s "$SECRET_FILE" ]; then
  "$PY" - <<'PYEOF' > "$SECRET_FILE"
import secrets
print(secrets.token_hex(64))
PYEOF
  chmod 600 "$SECRET_FILE"
  ok "Generated new secret key"
else
  ok "Existing secret key preserved"
fi

# ---------- Step 6: admin account ----------
next_step "Administrator account"
ADMIN_EXISTS="$(FAI_DB="$DB_FILE" "$PY" - <<'PYEOF'
import sqlite3, os
db = os.environ.get("FAI_DB")
try:
    c = sqlite3.connect(db)
    n = c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
    c.close()
    print(n)
except Exception:
    print("0")
PYEOF
)"
if [ "$ADMIN_EXISTS" = "0" ] || [ "$UPDATE_MODE" -eq 0 ]; then
  [ -t 0 ] || die "First install needs a TTY for admin creation."
  echo "${C_DIM}  Create the initial administrator (password ≥ 8 chars).${C_RESET}"
  while :; do
    read -r -p "  Admin username: " ADMIN_USER
    [ -n "$ADMIN_USER" ] && break
    warn "Username cannot be empty"
  done
  read -r -p "  Admin email (optional): " ADMIN_EMAIL
  while :; do
    read -r -s -p "  Admin password: " ADMIN_PASS; echo
    read -r -s -p "  Confirm password: " ADMIN_PASS2; echo
    [ ${#ADMIN_PASS} -ge 8 ] || { warn "Password must be ≥ 8 chars"; continue; }
    [ "$ADMIN_PASS" = "$ADMIN_PASS2" ] || { warn "Passwords do not match"; continue; }
    break
  done
  export FAI_ADMIN_USER="$ADMIN_USER" FAI_ADMIN_PASS="$ADMIN_PASS" FAI_ADMIN_EMAIL="$ADMIN_EMAIL"
  export FAI_DB="$DB_FILE" FAI_SECRET_FILE="$SECRET_FILE" FAI_INIT_ONLY="1"
  "$PY" "$WEB_PY" --init-only || die "Admin creation failed"
  ok "Administrator '$ADMIN_USER' created"
  unset FAI_ADMIN_PASS
else
  ok "Existing administrator(s) preserved"
fi

# ---------- Step 7: database ----------
next_step "Database"
export FAI_DB="$DB_FILE" FAI_SECRET_FILE="$SECRET_FILE" FAI_INIT_ONLY="1"
"$PY" "$WEB_PY" --init-only || die "Database initialization failed"
ok "Database ready: $DB_FILE"

# ---------- Step 8: run.sh ----------
next_step "Writing run configuration"
cat > "${APP_DIR}/run.sh" <<EOF
#!/usr/bin/env bash
cd "$APP_DIR"
export FAI_DB="$DB_FILE"
export FAI_SECRET_FILE="$SECRET_FILE"
export FAI_HOST="$PANEL_HOST"
export FAI_PORT="$PANEL_PORT"
exec "$PY" "$WEB_PY"
EOF
chmod +x "${APP_DIR}/run.sh"
ok "run.sh written"

# ---------- Step 9: launch ----------
next_step "Starting panel"
echo
echo "${C_GREEN}${C_BOLD}  Installation complete!${C_RESET}"
echo "  ${C_CYAN}URL:${C_RESET} ${C_BOLD}http://${PANEL_HOST}:${PANEL_PORT}${C_RESET}"
echo "  ${C_DIM}Press Ctrl+C to stop the panel gracefully.${C_RESET}"
echo "  ${C_DIM}Re-run this installer later to update (data is preserved).${C_RESET}"
echo

cd "$APP_DIR"
export FAI_DB="$DB_FILE" FAI_SECRET_FILE="$SECRET_FILE"
export FAI_HOST="$PANEL_HOST" FAI_PORT="$PANEL_PORT"
trap 'echo; echo "${C_YELLOW}  Shutting down...${C_RESET}"; kill -TERM "$CHILD_PID" 2>/dev/null; wait "$CHILD_PID" 2>/dev/null; exit 0' INT TERM
"$PY" "$WEB_PY" &
CHILD_PID=$!
wait "$CHILD_PID"
