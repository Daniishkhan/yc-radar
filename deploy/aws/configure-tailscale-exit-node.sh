#!/usr/bin/env bash
set -Eeuo pipefail

readonly SYSCTL_FILE=/etc/sysctl.d/99-tailscale.conf

if [[ ${EUID} -ne 0 ]]; then
  echo "configure-tailscale-exit-node.sh must run as root" >&2
  exit 1
fi

advertise=false
case ${1:-} in
  "") ;;
  --advertise) advertise=true ;;
  *)
    echo "Usage: configure-tailscale-exit-node.sh [--advertise]" >&2
    exit 2
    ;;
esac

temporary=$(mktemp /tmp/radar-tailscale-sysctl.XXXXXX)
trap 'rm -f "${temporary}"' EXIT
printf '%s\n' \
  'net.ipv4.ip_forward = 1' \
  'net.ipv6.conf.all.forwarding = 1' \
  > "${temporary}"
install -m 0644 "${temporary}" "${SYSCTL_FILE}"
sysctl --load="${SYSCTL_FILE}"

if ${advertise}; then
  if ! command -v tailscale >/dev/null 2>&1; then
    echo "tailscale is not installed" >&2
    exit 1
  fi
  tailscale set --advertise-exit-node
fi
