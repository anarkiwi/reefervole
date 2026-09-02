#!/bin/sh
# Put each bench NIC in its own network namespace so the host stack cannot short-circuit
# the path through the board. Physical interfaces return to the host when a namespace is
# deleted, so "down" is a complete undo.
#
#   tools/bench_netns.sh up   enp1s0 enp2s0
#   tools/bench_netns.sh ping
#   tools/bench_netns.sh down
#
# Run "up" with the two NICs cabled directly to each other first: if ping fails then, the
# rig is at fault, not the board. Only then insert the board and re-test.
set -eu

NS_A=bsw-a
NS_B=bsw-b
PFX=2001:db8:1

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

setup_side() {
    ns=$1; ifc=$2; host=$3
    ip netns add "$ns" 2>/dev/null || true
    ip link set "$ifc" netns "$ns"
    ip netns exec "$ns" sysctl -qw "net.ipv6.conf.$ifc.accept_ra=0" "net.ipv6.conf.$ifc.autoconf=0"
    ip netns exec "$ns" ip link set "$ifc" up
    ip netns exec "$ns" ip -6 addr add "$PFX::$host/64" dev "$ifc"
    echo "  $ns: $ifc -> $PFX::$host/64"
}

case "${1:-}" in
up)
    [ $# -eq 3 ] || usage
    [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
    setup_side "$NS_A" "$2" 1
    setup_side "$NS_B" "$3" 2
    echo "up. link state settles in a few seconds; then: $0 ping"
    ;;
ping)
    ip netns exec "$NS_A" ping -6 -c 3 -W 2 "$PFX::2"
    ;;
status)
    for ns in "$NS_A" "$NS_B"; do
        echo "== $ns =="
        ip netns exec "$ns" ip -br -6 addr 2>/dev/null || echo "  absent"
        ip netns exec "$ns" ip -br link 2>/dev/null | sed 's/^/  /' || true
    done
    ;;
down)
    [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
    for ns in "$NS_A" "$NS_B"; do ip netns del "$ns" 2>/dev/null && echo "  deleted $ns" || true; done
    echo "interfaces returned to the host namespace"
    ;;
*) usage ;;
esac
