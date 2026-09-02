# Deploying Pignus

The box pulls from GitHub and runs from there. Never edit source on the box and
never copy binaries onto it: build and check on a laptop, push, then `git pull`
on the box.

## What runs

| unit | port | what it does |
|---|---|---|
| `pignus-oracle` | 8740 | signs prices on a timer and publishes every one |
| `pignusd` | 8741 | the loan book, the chain watcher, and the page |
| `pignus-btc-responder` | none | a lender's side of every open cross-chain loan |

The responder is a lender's own process, not part of the service: it holds the
key that signs releases and the wallet that pays principals. It is listed here
because the testnet runs one, and because a cross-chain offer nobody responds to
is an offer nobody can take.

Both are pure Python and need no build step. They do need a Sequentia **source**
checkout, because the loan covenant lives in the node repository and Pignus
imports the proven builder rather than carrying a copy — see `CLAUDE.md`.

`pignusd` also reads the asset registry (`registry` in its config, the local
`sequentia-registry` on :3005) to turn each market's tickers into asset ids and
precisions, and the node's `getfeeexchangerates` to price fees in whatever
asset a wallet holds. Without the registry the node's own asset labels are used;
without the node there are no fee rates and no chain state, and `/healthz` says
so.

## First install

```bash
ssh seq
cd /root/sequentia
git clone https://github.com/ConcatenaLabs/pignus.git
mkdir -p /root/sequentia/pignus-data

# the node source the covenant is imported from (NOT the committee run
# directory; nothing here needs the running nodes)
cd /root/sequentia/Sequentia && git fetch origin && git checkout master \
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
SEQUENTIA_SRC=/root/sequentia/Sequentia \
  /root/sequentia/pignus/tests/cli_drill.sh
```

`pignusd`'s `/healthz` reports `ok: false` with the reason when it cannot reach
the node or the oracle, and the page shows "degraded" rather than stale numbers
dressed as live ones.

## Threshold oracles

The book can quote against several INDEPENDENT oracles, which is what an m-of-n
loan needs. Run more than one `pignus-oracle` (each its own keyfile and port)
and list the extra ones in `pignusd.json`:

```json
{ "oracle": "http://127.0.0.1:8740",
  "oracles": ["http://127.0.0.1:8742", "http://127.0.0.1:8743"],
  "reference_ticker": "USDX", "block_seconds": 60 }
```

`/v1/oracles` then lists the set and `/v1/attestations/{market}` returns one
attestation per oracle; a lender opens a 2-of-3 with `offer-fund --oracles book
--oracle-threshold 2`. On the testnet box the extra oracles are
`pignus-oracle2` / `pignus-oracle3` (systemd units, ports 8742/8743).

## Native BTC collateral (Tier B)

This tier is cross-chain and, unlike the covenant tiers, needs the lender as an
active counterparty: Bitcoin has no covenants, so liquidation needs the oracle to
co-sign and origination is a two-party handshake. A borrower does the whole of
their side in the page; the lender's side is a process that has to be running.

```bash
cp /root/sequentia/pignus/deploy/responder.example.json \
   /root/sequentia/pignus-responder.json
chmod 600 /root/sequentia/pignus-responder.json
# edit it: the lender key, the Sequentia wallet that pays principals, and the
# Bitcoin node. Nothing secret goes on the command line -- `ps` is readable by
# every user on the box.
cp /root/sequentia/pignus/deploy/pignus-btc-responder.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pignus-btc-responder
```

The responder signs releases, pays principals once the collateral is confirmed,
starts loans as borrowers claim their principal, and takes back what nobody
claimed. It keeps its state in one file beside the key (`state` in the config):
that file is what stops a principal being paid twice after a crash, so back it
up with the key and never delete it while a loan is open.

**Back up the lender key AND the state file.** Losing the key loses the ability
to claim repayments -- borrowers can still take their collateral back once the
loans time out, so nobody is robbed, but the lender is. Losing the state file
without losing the key risks paying a principal twice.

Publishing an offer, from the lender's machine:

```bash
pignus-cli btc-offer-publish --config /root/sequentia/pignus-responder.json \
    --oracle-x <x> --btc-amount 100000 --debt-asset <id> --debt 5250000000 \
    --principal 5000000000 --lots 3 --market BTC/USDX
```

Every offer is signed by the lender key it names, and the relay refuses one that
is not: an unsigned offer would let anyone publish in a lender's name and have
that lender's own responder pay it out. See the README for the whole command
sequence and the design doc section 7 for the trust model and what each party is
exposed to at each step.

## Seeding the book

The page is only as useful as what is on it. Offers and loans can be put on the
live chain from a node wallet with the CLI, against the running book:

```bash
P=/root/sequentia/pignus/bin/pignus-cli
RPC="--rpc http://127.0.0.1:18200 --rpc-user seq --rpc-password seq"
$P offer-fund --market GOLD/USDX --principal 100 --lots 3 --interest 3 \
    --open-ltv 50 --liq-ltv 75 --term-days 30 --rpc-wallet treasury2026 $RPC
$P offer-take --offer <offer id> --rpc-wallet recovered $RPC
$P repay --loan <loan id> --rpc-wallet recovered $RPC
```

Every command derives the address it acts on from the terms and checks it
against the coin first, prices the fee from the node's exchange rates, and
prepares explicit (unblinded) coins in the wallet when it only holds blinded
change, which a covenant cannot spend. The book follows the offer's coin and
discovers the vault from the take witness, so nothing needs to be told twice.

## What is NOT deployed, and why

No liquidator bot runs here. Liquidation is a permissionless race and anyone may
enter it; a platform whose liquidations depend on the operator's bot has an
operator. `pignus-liquidator` ships for anyone who wants to run one, and it is
deliberately not part of the service.
