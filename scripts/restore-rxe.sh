#!/usr/bin/env bash
# Restore the saved RXE configuration, then verify communication as the current user.
set -euo pipefail

fail() {
    printf 'RXE restore: %s\n' "$*" >&2
    exit 1
}

[[ $(uname -s) == Linux ]] || fail "run this script inside Linux; see docs/00-environment.md"
[[ $EUID -ne 0 ]] || fail "run as your normal user; only device setup uses sudo"
[[ $# -le 1 ]] || fail "usage: bash scripts/restore-rxe.sh [config-file]"
config=${1:-"$HOME/.config/rdma101/env"}
[[ -r $config ]] || fail "missing $config; complete the first-time RXE setup in Lab 0"
source "$config"

for name in R101_NETDEV R101_DEVICE R101_PORT R101_GID_INDEX; do
    [[ -n ${!name:-} ]] || fail "$name is missing from $config"
done
[[ -d /sys/class/net/$R101_NETDEV ]] || fail "interface $R101_NETDEV is missing; update $config"

sudo modprobe rdma_rxe || fail "install the RXE module for the running kernel; see Lab 0"
sudo modprobe ib_uverbs
if [[ ! -d /sys/class/infiniband/$R101_DEVICE ]]; then
    sudo rdma link add "$R101_DEVICE" type rxe netdev "$R101_NETDEV"
fi

gid_netdev="/sys/class/infiniband/$R101_DEVICE/ports/$R101_PORT/gid_attrs/ndevs/$R101_GID_INDEX"
[[ -r $gid_netdev ]] || fail "configured port/GID is unavailable; run doctor.py and update $config"
[[ $(cat "$gid_netdev") == "$R101_NETDEV" ]] || \
    fail "device/GID is not bound to $R101_NETDEV; check rdma link show and $config"

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
uv run --locked python scripts/doctor.py --check \
    --device "$R101_DEVICE" --port "$R101_PORT" --gid-index "$R101_GID_INDEX"
