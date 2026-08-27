#!/bin/bash

# Emit a square 0/1 QR module matrix for the payload in $1, one row per line.
# Same technique as omarchy-network-qr: qrencode's ASCII type prints two
# characters per module; collapse each pair to a single 0/1. Margin 4 is the
# spec quiet zone.

set -euo pipefail

payload=${1:-}
[[ -n $payload ]] || { echo "usage: make-qr.sh <payload>" >&2; exit 1; }

ascii=$(printf '%s' "$payload" | qrencode --type ASCII --margin 4 --output -)
while IFS= read -r line; do
  row=
  for ((column = 0; column < ${#line}; column += 2)); do
    [[ ${line:column:2} == *#* ]] && row+=1 || row+=0
  done
  printf '%s\n' "$row"
done <<<"$ascii"
