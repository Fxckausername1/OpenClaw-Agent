#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-install}"
PORTS="25503,25520"

apply_rule() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    return
  fi
  if [[ "$ACTION" == "install" ]]; then
    if ! "$tool" -C INPUT '!' -i lo -p tcp -m multiport --dports "$PORTS" -j DROP 2>/dev/null; then
      "$tool" -I INPUT 1 '!' -i lo -p tcp -m multiport --dports "$PORTS" -j DROP
    fi
  elif [[ "$ACTION" == "remove" ]]; then
    while "$tool" -C INPUT '!' -i lo -p tcp -m multiport --dports "$PORTS" -j DROP 2>/dev/null; do
      "$tool" -D INPUT '!' -i lo -p tcp -m multiport --dports "$PORTS" -j DROP
    done
  else
    echo "usage: $0 [install|remove]" >&2
    exit 2
  fi
}

apply_rule iptables
apply_rule ip6tables
