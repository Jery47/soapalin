#!/usr/bin/env bash
# Install soapalin CLI on a Zimbra server.
#
#   sudo ./install.sh
#
# The CLI is installed under /opt/soapalin (or SOAPALIN_DIR). It runs as the
# "zimbra" user when possible so zmmailbox can export/import mailboxes locally.
set -euo pipefail

APP_DIR="${SOAPALIN_DIR:-/opt/soapalin}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mErreur:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "Ce script doit etre lance en root (sudo ./install.sh)."
id zimbra >/dev/null 2>&1 || die "L'utilisateur systeme 'zimbra' est introuvable. Ce script doit tourner sur un serveur Zimbra."
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "${PYTHON_BIN} est introuvable. Installez Python 3.8+ ou definissez PYTHON_BIN."

PY_VERSION="$("${PYTHON_BIN}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "inconnue")"
"${PYTHON_BIN}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8+ est requis. Version detectee : ${PY_VERSION}."

log "Installation dans ${APP_DIR}"
mkdir -p "${APP_DIR}/config" "${APP_DIR}/data"

mkdir -p "${APP_DIR}/bin"

if [[ "${SCRIPT_DIR}" != "${APP_DIR}" ]]; then
  for item in soapalin_cli.py soapalin vendor requirements.txt pyproject.toml README.md \
              install.sh uninstall.sh; do
    [[ -e "${SCRIPT_DIR}/${item}" ]] || die "Element manquant : ${SCRIPT_DIR}/${item}"
    cp -a "${SCRIPT_DIR}/${item}" "${APP_DIR}/"
  done
  cp -a "${SCRIPT_DIR}/bin/soapalin" "${APP_DIR}/bin/soapalin"
fi

chmod 755 "${APP_DIR}/bin/soapalin" "${APP_DIR}/install.sh" "${APP_DIR}/uninstall.sh" 2>/dev/null || true
chown -R zimbra:zimbra "${APP_DIR}"
chmod 700 "${APP_DIR}/config"
chmod 700 "${APP_DIR}/data"

log "Environnement Python"
VENV_PY="${APP_DIR}/venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
  "${PYTHON_BIN}" -m venv "${APP_DIR}/venv" || die "Impossible de creer le venv. Installez le paquet python3-venv."
fi
VENV_VERSION="$("${VENV_PY}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
log "Python du venv : ${VENV_VERSION}"
if ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
  "${VENV_PY}" -m ensurepip --upgrade || die "pip est absent du venv et ensurepip a echoue."
fi
"${VENV_PY}" -m pip install --upgrade pip || die "Impossible de mettre a jour pip."
"${VENV_PY}" -m pip install -r "${APP_DIR}/requirements.txt" \
  || die "L'installation des dependances a echoue."
chown -R zimbra:zimbra "${APP_DIR}/venv"

log "Termine"
cat <<EOF

Commande CLI :
  ${APP_DIR}/bin/soapalin server list

Fichiers :
  Configuration : ${APP_DIR}/config/
  Serveurs      : ${APP_DIR}/config/servers/
  Donnees       : ${APP_DIR}/data/
EOF
