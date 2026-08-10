#!/bin/sh
# Bring the regtest Lightning nodes to a state the drills can run against.
#
#   sh scripts/dev/ln_bootstrap.sh
#
# Safe to re-run, and re-running is the normal case rather than the exception.
# Every step asks before it acts: wallets are created only if absent, coins
# mined only if a node is short, channels opened only if one of the right size
# is not already there.
#
# The step that matters most on a second run is the peer connect. LND channels
# go `active=false` after any restart of either side and stay that way until the
# peers reconnect — the funds are fine, the channel simply cannot route. A
# `docker compose restart` therefore leaves a stack that looks healthy and fails
# every drill, so connecting is unconditional and this script waits for the
# channels to come back before it says it is done.
#
# Two things the R4 spike found the hard way:
#
# - The btcpayserver/lnd image owns wallet creation through walletunlock.json.
#   Creating one yourself through the REST API leaves it unable to unlock later,
#   and the error it gives is "invalid password". So the wallet is created
#   through the same REST call the image would make, and the password is the one
#   the image writes down.
# - `lncli` cannot be used before the wallet exists; `/v1/state` can.
set -eu

case "$(uname -s 2>/dev/null || echo unknown)" in
MINGW* | MSYS*) MSYS_NO_PATHCONV=1 && export MSYS_NO_PATHCONV ;;
esac

COMPOSE="docker compose -f deploy/docker-compose.regtest.yml -f deploy/docker-compose.regtest.lightning.yml"

# Regtest, and worth nothing. The image writes this into walletunlock.json.
WALLET_PASSWORD="${LN_WALLET_PASSWORD:-hellorockstar}"
WALLET_PASSWORD_B64=$(printf '%s' "$WALLET_PASSWORD" | base64 | tr -d '\n')

# Our inbound: what a depositor can pay us over. Generous, because every drill
# deposit consumes some of it and the stack is meant to survive a day of runs.
DEPOSIT_CHANNEL_SATS=10000000
# Our outbound to the payee. Deliberately small: it is the ceiling drill 10
# exceeds, and a liquidity failure needs a liquidity limit to exist.
PAYOUT_CHANNEL_SATS=1000000

NODES="lnd-btcpay lnd-user lnd-payee"

log() { echo "[ln] $*" >&2; }
die() {
    echo "[ln] FAILED: $*" >&2
    exit 1
}

lncli() {
    node="$1"
    shift
    $COMPOSE exec -T "$node" lncli --network=regtest --lnddir=/data "$@"
}

curl_node() {
    node="$1"
    shift
    $COMPOSE exec -T "$node" curl -sk "$@"
}

bitcoin_cli() {
    $COMPOSE exec -T bitcoind bitcoin-cli -datadir=/data "$@"
}

# -- wallets ---------------------------------------------------------------

wait_for_rest() {
    node="$1"
    i=0
    while [ "$i" -lt 60 ]; do
        if curl_node "$node" -o /dev/null https://127.0.0.1:8080/v1/state 2>/dev/null; then
            return 0
        fi
        i=$((i + 1))
        sleep 2
    done
    die "$node never answered /v1/state"
}

wallet_state() {
    # NON_EXISTING | LOCKED | UNLOCKED | RPC_ACTIVE | SERVER_ACTIVE
    curl_node "$1" https://127.0.0.1:8080/v1/state 2>/dev/null |
        sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([A-Z_]*\)".*/\1/p'
}

ensure_wallet() {
    node="$1"
    state=$(wallet_state "$node")
    case "$state" in
    RPC_ACTIVE | SERVER_ACTIVE)
        log "$node: wallet is up ($state)"
        return 0
        ;;
    NON_EXISTING)
        log "$node: creating wallet"
        seed=$(curl_node "$node" https://127.0.0.1:8080/v1/genseed |
            sed -n 's/.*\("cipher_seed_mnemonic":\[[^]]*\]\).*/\1/p')
        [ -n "$seed" ] || die "$node: could not generate a seed"
        curl_node "$node" -X POST --data \
            "{\"wallet_password\":\"$WALLET_PASSWORD_B64\",$seed}" \
            https://127.0.0.1:8080/v1/initwallet >/dev/null
        ;;
    LOCKED)
        log "$node: unlocking wallet"
        curl_node "$node" -X POST --data "{\"wallet_password\":\"$WALLET_PASSWORD_B64\"}" \
            https://127.0.0.1:8080/v1/unlockwallet >/dev/null
        ;;
    *)
        log "$node: waiting, state is ${state:-unknown}"
        ;;
    esac

    i=0
    while [ "$i" -lt 45 ]; do
        state=$(wallet_state "$node")
        case "$state" in
        RPC_ACTIVE | SERVER_ACTIVE)
            log "$node: wallet ready ($state)"
            return 0
            ;;
        esac
        i=$((i + 1))
        sleep 2
    done
    die "$node: wallet never became usable (last state ${state:-unknown})"
}

wait_synced() {
    node="$1"
    i=0
    while [ "$i" -lt 60 ]; do
        if [ "$(lncli "$node" getinfo 2>/dev/null | grep -c '"synced_to_chain": true')" -gt 0 ]; then
            return 0
        fi
        i=$((i + 1))
        sleep 2
    done
    die "$node never synced to the chain"
}

# -- funding ---------------------------------------------------------------

confirmed_balance() {
    lncli "$1" walletbalance | sed -n 's/.*"confirmed_balance"[^0-9]*\([0-9]*\).*/\1/p' | head -1
}

ensure_funded() {
    node="$1"
    want="$2"
    have=$(confirmed_balance "$node")
    have=${have:-0}
    if [ "$have" -ge "$want" ]; then
        log "$node: funded ($have sat)"
        return 0
    fi
    log "$node: has $have sat, mining to it"
    address=$(lncli "$node" newaddress p2wkh | sed -n 's/.*"address"[^"]*"\([^"]*\)".*/\1/p')
    [ -n "$address" ] || die "$node: no address"
    bitcoin_cli generatetoaddress 25 "$address" >/dev/null
}

mine() {
    address=$(bitcoin_cli -rpcwallet=regtest getnewaddress)
    bitcoin_cli generatetoaddress "$1" "$address" >/dev/null
}

# -- peers and channels ----------------------------------------------------

pubkey() {
    lncli "$1" getinfo | sed -n 's/.*"identity_pubkey"[^"]*"\([^"]*\)".*/\1/p'
}

connect_peer() {
    # Unconditional and idempotent: "already connected" is a success here, and
    # this is the step that revives channels after a restart.
    lncli "$1" connect "$2@$3:9735" >/dev/null 2>&1 || true
}

channel_capacity_to() {
    # Total capacity of channels from $1 towards pubkey $2, 0 if none.
    lncli "$1" listchannels --peer "$2" 2>/dev/null |
        sed -n 's/.*"capacity"[^0-9]*\([0-9]*\).*/\1/p' |
        awk '{total += $1} END {print total + 0}'
}

ensure_channel() {
    from="$1"
    to_key="$2"
    to_host="$3"
    sats="$4"

    have=$(channel_capacity_to "$from" "$to_key")
    if [ "${have:-0}" -ge "$sats" ]; then
        log "$from -> $to_host: channel exists ($have sat)"
        return 0
    fi
    log "$from -> $to_host: opening a $sats sat channel"
    lncli "$from" openchannel --node_key="$to_key" --local_amt="$sats" >/dev/null ||
        die "$from could not open a channel to $to_host"
    mine 6
}

wait_active() {
    from="$1"
    to_key="$2"
    to_host="$3"
    i=0
    while [ "$i" -lt 45 ]; do
        actives=$(lncli "$from" listchannels --peer "$to_key" 2>/dev/null |
            grep -c '"active": true' || true)
        if [ "${actives:-0}" -gt 0 ]; then
            log "$from -> $to_host: channel active"
            return 0
        fi
        # A channel that exists but will not activate is almost always a peer
        # that has not reconnected since a restart.
        connect_peer "$from" "$to_key" "$to_host"
        i=$((i + 1))
        sleep 2
    done
    die "$from -> $to_host: the channel never became active"
}

# -- run -------------------------------------------------------------------

log "waiting for the nodes to answer"
for node in $NODES; do wait_for_rest "$node"; done
for node in $NODES; do ensure_wallet "$node"; done

log "making sure bitcoind has a wallet to mine into"
bitcoin_cli loadwallet regtest >/dev/null 2>&1 ||
    bitcoin_cli createwallet regtest >/dev/null 2>&1 || true

for node in $NODES; do ensure_funded "$node" 200000000; done
# Coinbase outputs need 100 confirmations before they can fund a channel.
mine 101
for node in $NODES; do wait_synced "$node"; done

BTCPAY_KEY=$(pubkey lnd-btcpay)
USER_KEY=$(pubkey lnd-user)
PAYEE_KEY=$(pubkey lnd-payee)
[ -n "$BTCPAY_KEY" ] && [ -n "$USER_KEY" ] && [ -n "$PAYEE_KEY" ] || die "could not read every pubkey"
log "btcpay=$BTCPAY_KEY"
log "user=$USER_KEY"
log "payee=$PAYEE_KEY"

log "connecting peers"
connect_peer lnd-user "$BTCPAY_KEY" lnd-btcpay
connect_peer lnd-btcpay "$PAYEE_KEY" lnd-payee
sleep 3

# user -> btcpay is our inbound: it is what a deposit is paid over.
ensure_channel lnd-user "$BTCPAY_KEY" lnd-btcpay "$DEPOSIT_CHANNEL_SATS"
# btcpay -> payee is our outbound, and its size is the liquidity ceiling that
# makes drill 10 mean something.
ensure_channel lnd-btcpay "$PAYEE_KEY" lnd-payee "$PAYOUT_CHANNEL_SATS"

mine 6
wait_active lnd-user "$BTCPAY_KEY" lnd-btcpay
wait_active lnd-btcpay "$PAYEE_KEY" lnd-payee

log "channels from the store's node:"
lncli lnd-btcpay listchannels |
    sed -n 's/.*"\(active\|local_balance\|remote_balance\|remote_pubkey\)"[^"0-9]*\("\?\)\([^",]*\).*/  \1=\3/p'
log "done"
