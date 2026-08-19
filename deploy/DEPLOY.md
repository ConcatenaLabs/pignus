# Deploying Pignus

The box pulls from GitHub and runs from there. Never edit source on the box and
never copy binaries onto it: build and check on a laptop, push, then `git pull`
on the box.

## What runs

| unit | port | what it does |
|---|---|---|
| `pignus-oracle` | 8740 | signs prices on a timer and publishes every one |
| `pignusd` | 8741 | the loan book, the chain watcher, and the page |

Both are pure Python and need no build step. They do need a Sequentia **source**
checkout, because the loan covenant lives in the node repository and Pignus
imports the proven builder rather than carrying a copy — see `CLAUDE.md`.

## First install

```bash
ssh seq
cd /root/sequentia
git clone https://github.com/GracedEternalKingCabbageMan/pignus.git
mkdir -p /root/sequentia/pignus-data

# the node source the covenant is imported from (NOT the committee run
# directory; nothing here needs the running nodes)
cd /root/sequentia/SequentiaByClaude && git fetch origin && git checkout master \
  && git pull --ff-only

cp /root/sequentia/pignus/deploy/*.service /etc/systemd/system/
cp /root/sequentia/pignus/deploy/oracle.example.json  /root/sequentia/pignus-oracle.json
cp /root/sequentia/pignus/deploy/pignusd.example.json /root/sequentia/pignusd.json
# edit both: the node RPC credentials go in pignusd.json only
chmod 600 /root/sequentia/pignusd.json

systemctl daemon-reload
systemctl enable --now pignus-oracle pignusd
```

The oracle key is created 0600 on first start at the `keyfile` path and is never
logged or served. **Back it up.** Losing it does not lose anyone's money — a
vault whose oracle key is gone still has its oracle-free REPAY exit, and the
lender still has RECOVER — but every loan already open against that key becomes
unliquidatable until it matures, which is a slow, ugly failure.

Publish the key so borrowers and lenders can pin it:

```bash
/root/sequentia/pignus/bin/pignus-oracle \
  --config /root/sequentia/pignus-oracle.json --print-pubkey
```

## Caddy

```
handle_path /lending/* {
    reverse_proxy 127.0.0.1:8741
}
redir /lending /lending/ permanent
handle_path /pignus-oracle/* {
    reverse_proxy 127.0.0.1:8740
}
```

`caddy reload --config /etc/caddy/Caddyfile` (never restart; a reload does not
drop the other sites).

## Updating

```bash
cd /root/sequentia/pignus && git pull --ff-only
systemctl restart pignus-oracle pignusd
```

If the node repository's covenant changed, pull that too and re-run the drills
**before** restarting — `pignus/compat.py` refuses to derive addresses from a
builder that no longer matches `pignus/vectors.json`, so a mismatched pair fails
loudly at start rather than quietly producing wrong addresses.

## Checking it

```bash
curl -s localhost:8740/healthz
curl -s localhost:8741/healthz
curl -s localhost:8741/v1/markets | head -40
SEQUENTIA_SRC=/root/sequentia/SequentiaByClaude \
  /root/sequentia/pignus/tests/cli_drill.sh
```

`pignusd`'s `/healthz` reports `ok: false` with the reason when it cannot reach
the node or the oracle, and the page shows "degraded" rather than stale numbers
dressed as live ones.

## What is NOT deployed, and why

No liquidator bot runs here. Liquidation is a permissionless race and anyone may
enter it; a platform whose liquidations depend on the operator's bot has an
operator. `pignus-liquidator` ships for anyone who wants to run one, and it is
deliberately not part of the service.
