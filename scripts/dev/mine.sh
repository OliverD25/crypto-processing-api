#!/bin/sh
# Mine regtest blocks.
#
#   sh scripts/dev/mine.sh 101            # mine to the node's own wallet
#   sh scripts/dev/mine.sh 1 bcrt1q...    # mine to a specific address
#
# 101 is the useful first number: coinbase outputs need 100 confirmations
# before they are spendable, so block 101 is the first one that gives the node
# money to send.
set -eu

# Git Bash rewrites anything that looks like a POSIX path into a Windows one,
# which turns -datadir=/data into -datadir=C:/Program Files/Git/data.
case "$(uname -s 2>/dev/null || echo unknown)" in
MINGW* | MSYS*) MSYS_NO_PATHCONV=1 && export MSYS_NO_PATHCONV ;;
esac

BLOCKS="${1:-1}"
ADDRESS="${2:-}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.regtest.yml}"
WALLET="${REGTEST_WALLET:-regtest}"

cli() {
    docker compose -f "$COMPOSE_FILE" exec -T bitcoind bitcoin-cli -datadir=/data "$@"
}

if [ -z "$ADDRESS" ]; then
    cli loadwallet "$WALLET" >/dev/null 2>&1 ||
        cli createwallet "$WALLET" >/dev/null 2>&1 ||
        true
    ADDRESS=$(cli -rpcwallet="$WALLET" getnewaddress)
fi

echo "mining $BLOCKS block(s) to $ADDRESS"
cli generatetoaddress "$BLOCKS" "$ADDRESS"
cli getblockchaininfo | grep -E '"(chain|blocks)"'
