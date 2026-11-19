#!/usr/bin/env bash
# Uninstall the soapalin CLI from a Zimbra server.
#
#   sudo ./uninstall.sh              # remove code and virtual environment
#                                    # keep config/ and data/
#   sudo ./uninstall.sh --purge      # also remove config/ and data/
#   sudo ./uninstall.sh --yes        # without confirmation
set -euo pipefail

APP_DIR="${SOAPALIN_DIR:-/opt/soapalin}"
PURGE=0
ASSUME_YES=0

usage() {
  cat <<EOF
Usage : sudo ./uninstall.sh [options]

Options :
  --purge, -p   Supprime aussi la configuration et les donnees.
  --yes, -y     Ne demande pas de confirmation.
  --help, -h    Affiche cette aide.

Variables :
  SOAPALIN_DIR   Repertoire d'installation (defaut : /opt/soapalin).
EOF
}

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33mAttention :\033[0m %s\n' "$*" >&2; }
die() { printf '\n\033[1;31mErreur :\033[0m %s\n' "$*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --purge|-p) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --help|-h) usage; exit 0 ;;
    *) die "Option inconnue : $arg (utilisez --help)." ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "Ce script doit etre lance en root (sudo ./uninstall.sh)."
[[ -n "${APP_DIR}" && "${APP_DIR}" != "/" ]] || die "SOAPALIN_DIR invalide : ${APP_DIR}"
case "${APP_DIR}" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    die "Refus de desinstaller depuis un repertoire systeme : ${APP_DIR}"
    ;;
esac

if [[ "${PURGE}" -eq 1 ]]; then
  [[ -d "${APP_DIR}/soapalin" ]] \
    || die "${APP_DIR} ne ressemble pas a une installation de soapalin ; --purge annule."
  warn "Mode --purge : la configuration et TOUTES les donnees seront supprimees."
else
  warn "La configuration (${APP_DIR}/config) et les donnees (${APP_DIR}/data) seront conservees."
fi

if [[ "${ASSUME_YES}" -eq 0 ]]; then
  if [[ -t 0 ]]; then
    printf '\nConfirmer la desinstallation de "%s" ? [o/N] ' "${APP_DIR}"
    read -r reply
    [[ "${reply}" =~ ^[oOyY]$ ]] || { echo "Annule."; exit 0; }
  else
    die "Entree non interactive : relancez avec --yes pour confirmer."
  fi
fi

if [[ -d "${APP_DIR}" ]]; then
  if [[ "${PURGE}" -eq 1 ]]; then
    log "Suppression complete de ${APP_DIR}"
    rm -rf "${APP_DIR}"
  else
    log "Suppression du code et du venv dans ${APP_DIR}"
    for item in \
      soapalin soapalin_cli.py soapalin vendor tests bin \
      requirements.txt pyproject.toml README.md \
      install.sh uninstall.sh venv
    do
      rm -rf "${APP_DIR:?}/${item}"
    done
    find "${APP_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    mkdir -p "${APP_DIR}/config" "${APP_DIR}/data"
    chmod 700 "${APP_DIR}/config" "${APP_DIR}/data" 2>/dev/null || true
    if id zimbra >/dev/null 2>&1; then
      chown zimbra:zimbra "${APP_DIR}/config" "${APP_DIR}/data" 2>/dev/null || true
    fi
  fi
else
  log "Repertoire ${APP_DIR} absent"
fi

log "Desinstallation terminee"
if [[ "${PURGE}" -eq 1 ]]; then
  echo "Tout a ete supprime, y compris la configuration et les donnees."
else
  cat <<EOF
Les elements suivants sont conserves :
  Configuration : ${APP_DIR}/config
  Donnees       : ${APP_DIR}/data
Pour tout supprimer :
  sudo ./uninstall.sh --purge
EOF
fi
