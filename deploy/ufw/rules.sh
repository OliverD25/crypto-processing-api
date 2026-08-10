#!/bin/sh
# Firewall baseline for a VPS running btcpayserver-docker and this service.
#
#   sudo sh deploy/ufw/rules.sh
#
# Read this before running it. It ends with `ufw --force enable`, and if you are
# connected over SSH on a non-standard port that this script has not allowed,
# you will lock yourself out. Set SSH_PORT below first.
#
# ---------------------------------------------------------------------------
# The Docker footgun
#
# `ports:` on a container writes its own iptables rules in the DOCKER chain,
# which is consulted BEFORE ufw's. A container publishing 5432 is reachable
# from the internet no matter what ufw says, and `ufw status` will happily
# report "deny (incoming)" while the port is open.
#
# This is why deploy/docker-compose.yml publishes nothing at all and everything
# goes through nginx. If you add a `ports:` line, bind it to 127.0.0.1
# explicitly — `127.0.0.1:5432:5432`, never `5432:5432`.
# ---------------------------------------------------------------------------
set -eu

SSH_PORT="${SSH_PORT:-22}"
CF_V4_URL="https://www.cloudflare.com/ips-v4"
CF_V6_URL="https://www.cloudflare.com/ips-v6"

if [ "$(id -u)" -ne 0 ]; then
    echo "run this as root" >&2
    exit 1
fi

echo "resetting ufw to a default-deny baseline"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing

# SSH. Key-only authentication is assumed — set PasswordAuthentication no in
# /etc/ssh/sshd_config. This rule is the reason to read the script first.
ufw limit "${SSH_PORT}"/tcp comment 'ssh, rate limited'

# Bitcoin p2p. Open on purpose: the node needs inbound peers, and this port
# already reveals the origin IP, which is why Cloudflare in front of 443 is
# defence in depth rather than concealment.
ufw allow 8333/tcp comment 'bitcoin p2p'

# 443 only from Cloudflare. An attacker who learns the origin IP still cannot
# reach the API directly.
echo "fetching Cloudflare ranges"
for range in $(curl -fsS "$CF_V4_URL") $(curl -fsS "$CF_V6_URL"); do
    ufw allow from "$range" to any port 443 proto tcp comment 'cloudflare'
done

ufw --force enable
ufw status verbose

cat <<'NOTE'

Cloudflare's ranges change. Re-run this script from cron so the allow-list does
not go stale and lock out real traffic:

  # /etc/cron.d/ufw-cloudflare
  0 4 * * 1 root SSH_PORT=22 sh /opt/crypto-processing-api/deploy/ufw/rules.sh >/var/log/ufw-refresh.log 2>&1

Verify from somewhere else that nothing is exposed:

  nmap -Pn -p 443,5432,8000,23000,49392 <your-ip>

443 filtered from a non-Cloudflare address is the answer you want. A database
port answering means a container published it and ufw never saw the packet.
NOTE
