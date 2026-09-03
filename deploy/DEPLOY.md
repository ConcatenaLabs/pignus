# Deploying Pignus

The box pulls from GitHub and runs from there. Never edit source on the box and
never copy binaries onto it: build and check on a laptop, push, then `git pull`
on the box.

## What runs

| process | port | talks to | if it stops |
|---|---|---|---|
| `pignus-oracle` | 8740 | the price feed on :8088 | attestations stop within one tick; loans cannot be liquidated until it returns |
| further oracles | 8742, 8743 | the price feed on :8088 | an m-of-n loan loses a signer |
| `pignusd` | 8741 | node RPC :18200, registry :3005, the oracles | the page and the book go with it; nothing on chain is affected |
| `pignus-btc-responder` | none (a client) | `pignusd` :8741, node RPC :18200, Bitcoin testnet4 RPC :48332 | cross-chain borrows stall: takes go unsigned, funded ones unpaid |
| the price server | 8088 | upstream quotes | every oracle errors within `feed_max_age` |
| `sequentia-registry` | 3005 | — | tickers fall back to the node's own asset labels |

The price server is the node repository's `contrib/price-server`, the same one
that feeds the any-asset fee market; Pignus deliberately does not run a second
price pipeline.

The responder is a lender's own process, not part of the service: it holds the
key that signs releases and the wallet that pays principals. It is listed here
because the testnet runs one, and because a cross-chain offer nobody responds to
is an offer nobody can take.

Two systemd timers run beside those processes: `pignus-backup.timer` archives
daily everything that is not on a chain, and `pignus-check.timer` runs
`deploy/pignus-check.sh` every five minutes to ask whether the oracle is still
signing and the book is still being told. Neither serves anything; both are
described below.

All of it is pure Python and needs no build step. It does need a Sequentia
**source** checkout, because the loan covenant lives in the node repository and
Pignus imports the proven builder rather than carrying a copy — see `CLAUDE.md`.

`pignusd` reads the asset registry (`registry` in its config) to turn each
market's tickers into asset ids and precisions, and the node's
`getfeeexchangerates` to price fees in whatever asset a wallet holds. Without
the registry the node's own asset labels are used; without the node there are no
fee rates and no chain state, and `/healthz` says so.

## First install

```bash
ssh root@<the testnet box>
cd /root/sequentia
git clone https://github.com/ConcatenaLabs/pignus.git
mkdir -p /root/sequentia/pignus-data

# the node source the covenant is imported from (NOT the committee run
# directory; nothing here needs the running nodes)
cd /root/sequentia/Sequentia && git fetch origin && git checkout master \
  && git pull --ff-only

cp /root/sequentia/pignus/deploy/*.service /etc/systemd/system/
cp /root/sequentia/pignus/deploy/*.timer   /etc/systemd/system/
cp /root/sequentia/pignus/deploy/oracle.example.json    /root/sequentia/pignus-oracle.json
cp /root/sequentia/pignus/deploy/pignusd.example.json   /root/sequentia/pignusd.json
cp /root/sequentia/pignus/deploy/responder.example.json /root/sequentia/pignus-responder.json
# edit all three: the node RPC credentials go in pignusd.json and
# pignus-responder.json only, never on a command line
chmod 600 /root/sequentia/pignusd.json /root/sequentia/pignus-responder.json

/root/sequentia/pignus/bin/pignus-cli btc-keygen \
  --out /root/sequentia/pignus-data/lender.key

systemctl daemon-reload
systemctl enable --now pignus-oracle pignusd pignus-btc-responder
systemctl enable --now pignus-backup.timer pignus-check.timer
```

Every unit runs as root with its state under `/root`, which is exactly where
`ProtectSystem=` does not reach — so each one mounts `/root` read-only instead
and hands back the single directory it has to write: `/root/sequentia/pignus-data`
for the three services, and `/var/lib/pignus-backup` for the backup, which
systemd creates from `StateDirectory=`. **Move one of those
paths and the matching `ReadWritePaths=` moves with it.** Otherwise the service
starts cleanly and fails at its first write, which for `pignusd` is hours later
and reads as a bug rather than as a mount.

The services' directory has to exist before they start, which is what the `mkdir`
above is for: the mount namespace is built before the unit's first command runs,
so a `ReadWritePaths=` naming a directory that is not there fails the unit
outright and nothing inside it can create one.

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

## Backups

Everything Pignus keeps that is not on a chain: the oracle keys and their
attestation logs, the book, the lender key a cross-chain responder signs with,
the state file that stops it paying a principal twice, and the config files
beside them. All of it holds secrets, so keep the archive 0600 and copy it off
the box:

```bash
mkdir -p /var/lib/pignus-backup
tar czf /var/lib/pignus-backup/pignus-$(date +%F).tgz \
    /root/sequentia/pignus-data \
    /root/sequentia/pignusd.json \
    /root/sequentia/pignus-oracle*.json \
    /root/sequentia/pignus-responder.json
```

What each piece costs to lose is worth knowing before it happens. An oracle key
that is gone leaves every loan baked to it unliquidatable until it matures --
nobody is robbed, because a vault whose oracle is dead still has its oracle-free
repayment exit and its lender's backstop, but it is a slow, ugly failure. A
lender key that is gone costs the BORROWERS: their collateral can only be
released by the secret that key's holder publishes, so it sits until the
timeout. A responder state file that is gone can cost a principal paid twice.

**Put that state file where the service may write.** The responder's unit runs
with `ProtectHome=read-only` and one `ReadWritePaths=`, which is
`/root/sequentia/pignus-data`. Its `state` must point inside that directory,
not into the key directory beside `lender.key`: the keys are read-only on
purpose, and the state file is the one thing the responder writes before every
send. It refuses to start rather than run without being able to, and says where
to move it.

`pignus-backup.service` is that command as a unit, at `UMask=0077` so the
archive is 0600. It writes to `/var/lib/pignus-backup`, which systemd creates
and keeps writable for it; `pignus-backup.timer` runs it daily and catches up a day
the box was switched off for. `systemctl start pignus-backup` takes one now and
`journalctl -u pignus-backup` says how the last one went. The unit counts tar's
exit 1 as success: the oracle appends to its attestation log while the archive
is being written, and a prefix of an append-only log is what a backup of a live
one can be. A fatal error is exit 2 and still fails the unit.

The `pignus-oracle*.json` glob covers every threshold oracle's config as well as
the primary's. **An archive still on the box is not a backup** — copy it off.

What each piece costs to lose:

- **`oracle.key`** (and each extra oracle's) — every open loan against it
  becomes unliquidatable until maturity. See above.
- **`attestations.log`** and its rotated files — the audit trail. No coin is at
  risk, but a liquidation can no longer be checked afterwards, which is the
  whole of what bounds the oracle's trust.
- **`lender.key`** — the lender can no longer sign a release, disburse, start a
  loan or claim a repayment. Borrowers can still abort or take their collateral
  back once the timeouts open, so nobody is robbed; the lender is.
- **the responder's state file** — losing it while the key survives risks paying
  a principal twice, because the state file is the only record of what has
  already been sent. Restore it only from a copy newer than the last
  disbursement, and check the lender wallet's `listtransactions` against
  `/v1/btc/takes?status=disbursed` before you do.
- **`book.json`** — no coin is at risk: every vault and offer is a covenant on
  chain and reconstructs from its terms. But the book is the only index of them,
  and the cross-chain half (offers, takes, releases, statuses) is not
  rediscoverable from the chain at all. Restore it and restart `pignusd`, which
  re-tracks every loan and open offer and reconciles them against the chain.

Without a backup of `book.json`, each record is re-registered by whoever holds
its own copy: `POST /v1/offers` with the terms and outpoint (the id is derived
from those, so a re-post lands under the same id), `POST /v1/loans` with the
terms and funding outpoint (the book checks it against the chain first), and
`pignus-cli btc-offer-publish` for cross-chain offers. `pignus-data/` is not
touched by `git pull`.

## The attestation log

The oracle writes one JSON line per signed price to `logfile`: about 300 bytes
each, so six markets on a 60-second interval add roughly 2.5 MB a day per
oracle. It is not a debug log. It is the audit record behind `/v1/log`,
`/v1/attestation/{market}/at/{ts}` and `/v1/digest`, and it is what lets anyone
check a liquidation after the fact.

**Never rotate or truncate it from outside the daemon.** No `logrotate`, no
`copytruncate`, no `>` from a shell. The oracle carries a running SHA-256
forward as it appends and answers the recent view from memory; a file that
changes underneath it desynchronises both.

Set `log_max_bytes` and it rotates itself. The current file is renamed
`<logfile>.<timestamp>`, its final digest is written beside it as
`<logfile>.<timestamp>.sha256` and seeded into `<logfile>.chain`, and the new
file's running hash starts from that seed. So `/v1/digest` still commits to
every attestation the key has ever signed, and a downloader walks the chain
backwards file by file. Publishing that digest somewhere durable is what turns
"the log is append-only" from a promise into something checkable: a rewritten or
truncated file stops matching it.

Keep every rotated file, and back them up with the key.

Co-signed seizures go in a separate file (`<logfile>.seizures`, or `seizures` in
the config) with its own format, deliberately not mixed into the attestation
log, so the digest chain keeps meaning exactly "every price this oracle signed".
It is not itself digest-chained; back it up all the same, because it is the only
record of why a cross-chain seizure was allowed.

## Caddy

The whole site block, not a fragment — placed at top level these directives
belong to no site and do nothing:

```
sequentiatestnet.com {
    handle_path /lending/* {
        reverse_proxy 127.0.0.1:8741
    }
    redir /lending /lending/ permanent
    handle_path /pignus-oracle/* {
        reverse_proxy 127.0.0.1:8740
    }
    handle {
        reverse_proxy 127.0.0.1:8080   # the explorer: everything else
    }
}
```

`handle_path` strips the prefix, so the page's relative `v1/...` fetches reach
`pignusd` at `/v1/...`; the page's `/tx/<txid>` links and
`/pignus-oracle/v1/log` rely on this exact layout.

`caddy validate --config /etc/caddy/Caddyfile`, then `caddy reload --config
/etc/caddy/Caddyfile` — never restart, which would drop the other sites.

**`pignusd` must not be reachable except through Caddy.** It believes an
`X-Forwarded-For` header only from a peer in `trusted_proxies`, and keys its
rate limits on that header's last hop. A request that arrives from a trusted
peer *without* the header is taken for the box's own tooling — the responder
and the CLI on loopback — and is not rate-limited at all. Exposed directly,
every public client would arrive looking like that.

There are two limits, and they meter different costs. Writes are metered
because they change the book. Reads that touch the NODE — `/v1/spend`, which
walks blocks backwards, and `/v1/outpoint`, which asks per call — are metered
because they cost the node work, and both must stay unauthenticated: a borrower
recovering their own collateral has no account here. Everything else is served
from memory and is not worth limiting.

**`pignus-oracle` takes the same `trusted_proxies` setting**, and for a sharper
reason. Its log endpoints read off disk, so they are rate-limited — and keyed on
the socket peer behind a proxy, that limit gives the whole internet one bucket:
one flooder locks every auditor out of the log that exists to be audited. Set
it in each oracle's config, the same way.

## Updating

```bash
cd /root/sequentia/pignus && git fetch origin && git status -sb   # 'behind' if it lags
git pull --ff-only
SEQUENTIA_SRC=/root/sequentia/Sequentia tests/cli_drill.sh
systemctl restart pignus-oracle pignusd pignus-btc-responder
curl -s localhost:8741/healthz | jq -r .git_rev; git rev-parse --short HEAD
```

Every Pignus process runs from this one checkout, so every one of them is
restarted; a unit left on the old code keeps the old relay protocol and the old
covenant pin. The last line must print the same short hash twice. Threshold
oracles are restarted with the rest — `systemctl restart pignus-oracle@2
pignus-oracle@3`, naming the instances that are enabled.

A unit file that changed in the checkout is not the one systemd is running: the
copies under `/etc/systemd/system` are what it reads, so copy them over again
and `systemctl daemon-reload` before restarting. `pignus-check.sh` is the
exception — it is executed out of the checkout, so a pull is all it needs.

If the node repository's covenant changed, pull that too and re-run the drills
**before** restarting. `pignusd` loads the covenant and checks it against
`pignus/vectors.json` before it serves anything, and exits 3 with the reason
rather than deriving addresses from a builder that has drifted.

## Checking it

```bash
/root/sequentia/pignus/deploy/pignus-check.sh   # the whole answer, yes or no
curl -s localhost:8740/healthz          # 503 if the oracle is not signing
curl -s localhost:8741/healthz | jq '{ok, error, git_rev, covenant_vectors}'
curl -s localhost:8741/v1/markets | head -40
curl -s "localhost:8741/v1/btc/takes?status=pending"  # empty if the responder keeps up
journalctl -u pignus-btc-responder -f
SEQUENTIA_SRC=/root/sequentia/Sequentia \
  /root/sequentia/pignus/tests/cli_drill.sh
```

`pignus-check.sh` is the three `curl` lines under it as one command that answers
yes or no: every oracle's `/healthz` and the book's must say `ok`, and every
market that is not cross-chain must have a price signed within
`PIGNUS_MAX_PRICE_AGE` seconds, with no market disagreeing with the registry
about how many decimals its assets have. `pignus-check.timer` runs it every
five minutes and `pignus-check.service` carries the endpoints and that age as
environment: add each threshold oracle's port to `PIGNUS_ORACLES` there, and
keep `PIGNUS_MAX_PRICE_AGE` equal to `max_price_age` in `pignusd.json`, since a
check looser than the book's own limit reports healthy while the book is already
withholding prices. A failed run shows in `systemctl list-units --failed` and in
the journal, and nowhere else until an alerting unit is named in the commented
`OnFailure=` at the top of `pignus-check.service`.

Cross-chain rows are left out of the *age* half deliberately. A native-BTC
seizure is co-signed by an operator running a command, and the oracle refuses on
the spot against a stale price, so the person acting sees it. On a covenant
market nobody is looking: every liquidator in the race stops, and nothing says
so. The decimals are checked on every row, cross-chain included, because a
market whose oracle and registry disagree about them is priced out by a power of
ten and no signature check anywhere can see it.

The oracle's `/healthz` answers **503**, not 200, when it has not completed a
signing round within two intervals, with `stale`, `errors` and `round_error`
saying which market and why. A green oracle answers 200. The process staying up
while its signing thread is dead is exactly the outage that otherwise goes
unnoticed until it reaches the RECOVER backstop.

`pignusd`'s `/healthz` reports `ok: false` with the reason when there is no
node, when the node or the **primary** oracle is unreachable, while the first
sync runs, when the poll thread has not finished for `max(120s, 3 × poll)`, or
when any market's newest verified attestation is older than `max_price_age`.
`error`, `stale_markets` and `oracle_errors` name what is wrong; the page shows
"degraded" rather than stale numbers dressed as live ones. `covenant_vectors`
is how many golden vector cases the tripwire checked in that process — zero
would mean the builder was never loaded.

There are two age limits and they do different jobs. `feed_max_age` at the
oracle is how old the price feed's own `_meta.updated` may be before the oracle
refuses to re-sign its numbers. `max_price_age` at the book, and
`--max-attestation-age` at the liquidator and the CLI, are how old a *signed*
attestation may be before it is treated as no price at all. Nothing in tapscript
can check recency (section 5 of the design doc), so those two are the only
places it is checked anywhere.

## `pignusd` configuration

`deploy/pignusd.example.json` is the starting point. The keys:

| key | default | what it does |
|---|---|---|
| `listen` | `127.0.0.1:8741` | where to serve |
| `book` | — | the book file. Rewritten atomically (temporary file, fsync, rename). A book that is not valid JSON stops `pignusd` rather than being replaced with an empty one: restore it from a backup, or move it aside to start empty on purpose |
| `oracle` | — | the PRIMARY oracle: its key is the one this book hands lenders, and its prices are the ones shown |
| `oracles` | `[]` | further independent oracles, quoted for m-of-n loans. They never stand in for the primary, because a vault verifies against the key baked into it |
| `registry` | — | the asset registry, for tickers and precisions |
| `markets` | `[]` | which markets to show |
| `poll` | 30 | seconds between chain and oracle refreshes; keep it under `block_seconds` |
| `block_seconds` | 60 | the chain's block time, for turning heights into dates on the page |
| `reference_ticker` | `USDX` | the numeraire the page quotes value in. It is a display choice: no asset is privileged |
| `max_price_age` | 600 | a price older than this is not a price. Health, LTV and liquidatable are withheld rather than computed from it, and `/healthz` turns unhealthy |
| `min_depth` | 2 | how deep a funding must be before a loan reads as LIVE. Depth is the lender's risk appetite, so it is configuration; the page shows the number in use |
| `rescan_depth` | 1500 | how far back a poll may walk to find a spend it missed |
| `back_scan_cap` | 200 | how many blocks one poll may fetch for those backward walks. The walk is the expensive half of a poll, so it is rationed and a search that runs out of budget resumes on the next one. The forward scan is budgeted separately and generously: falling behind the tip is the one failure that compounds |
| `prune_after` | 2592000 | how long a *finished* record is kept: a spent offer, a closed vault, an ended cross-chain take. Nothing pruned is lost — the chain is the record and this book is an index of it — but every list is rendered end to end on every read. Anything still open is kept whatever its age; 0 keeps everything |
| `trusted_proxies` | `["127.0.0.1", "::1"]` | the peers whose `X-Forwarded-For` this daemon believes |
| `rpc` | — | the node. Optional: without one the book still serves offers and whatever it last knew, and says so in `/healthz` |
| `btc_rpc` | — | the parent chain's node, same four fields as `rpc`. Only for cross-chain loans, and it never spends, so no wallet is needed. Without it the Bitcoin half of every cross-chain deadline goes unchecked here, `/healthz` says `btc_node: false`, no `btc_height` is published, and the page refuses to originate rather than check half the timelocks |
| `explorer_url` | — | where the page's breadcrumb points. A self-hosted book is not behind the testnet's reverse proxy, so it can say where its explorer really is |
| `explorer_tx_url` | `/explorer/tx/{txid}` | the link a Sequentia transaction id becomes on the page |
| `btc_explorer_tx_url` | `/testnet4/tx/{txid}` | the same for a parent-chain transaction |
| `oracle_public_url` | — | where the page's link to the oracle log points |

A watcher that was down for longer than `rescan_depth` blocks cannot reach the
gap at all. Start it once with `--rescan-from <height>` and the forward scan
reads those blocks again, a capped number per poll, reopening offers stuck at
`gone` and naming exits that would otherwise stay `SPENT_UNKNOWN`.

`docs/api.md` documents every endpoint `pignusd` serves.

## Threshold oracles

The book can quote against several oracles, which is what an m-of-n loan needs.
Run more than one `pignus-oracle` and list the extra ones in `pignusd.json`:

```json
{ "oracle": "http://127.0.0.1:8740",
  "oracles": ["http://127.0.0.1:8742", "http://127.0.0.1:8743"],
  "reference_ticker": "USDX", "block_seconds": 60 }
```

`/v1/oracles` then lists the set and `/v1/attestations/{market}` returns one
attestation per oracle; a lender opens a 2-of-3 with `offer-fund --oracles book
--oracle-threshold 2`.

`pignus-oracle@.service` is the unit for every instance after the first, and
First install has already put it in `/etc/systemd/system` — it is one of the
`deploy/*.service` files. It reads `/root/sequentia/pignus-oracle-<n>.json`, so
a second and a third oracle are two configs and one command:

```bash
cp /root/sequentia/pignus/deploy/oracle2.example.json /root/sequentia/pignus-oracle-2.json
cp /root/sequentia/pignus/deploy/oracle3.example.json /root/sequentia/pignus-oracle-3.json
systemctl enable --now pignus-oracle@2 pignus-oracle@3
```

Those two files are `oracle.example.json` with a different `keyfile`, `logfile`
and `listen` and nothing else changed; `diff` them to see it. The primary keeps
its own unit, `pignus-oracle.service`, because its key is the one the book hands
lenders — it is named rather than numbered.

**Distinct keyfiles are what makes the set independent**: three instances
sharing one key are a 1-of-1 wearing a 2-of-3 label. Print each key with
`--print-pubkey`, add the new ports to `PIGNUS_ORACLES` in
`pignus-check.service`, and confirm `/v1/oracles` on the book lists them all.
The backup already covers the extra keys and configs, which are under
`pignus-data` and match the `pignus-oracle*.json` glob.

Independence here is of **keys**, not of prices. Every oracle in the example
configuration reads the same price server, so a stale or manipulated feed
produces the same wrong number under all three keys and a 2-of-3 loan liquidates
on it. Feed independence means giving each instance a different `source.url`,
pointing at genuinely different upstreams.

## Rotating the oracle key

A vault bakes the key it was originated against, so a rotation is never a swap:
the old key must keep signing until the last vault that names it has matured.

1. Run the new key as another `pignus-oracle@<n>` instance, on its own port with
   its own keyfile and logfile.
2. Add it to `pignusd.json` and make it the primary; the old instance stays in
   `oracles` and goes on signing.
3. List what still depends on the old key with
   `/v1/loans?oracle_x=<old key>`. Every one of those must reach maturity, or be
   repaid, before the old instance stops.
4. Retire the old instance, and move its key into the new instance's
   `previous_keys` so `/v1/pubkey` still names it. That is what lets a
   borrower's page tell a rotation from a stranger.

Keep the old instance's attestation log for as long as any liquidation made
against that key might be questioned.

## Native BTC collateral (Tier B)

This tier is cross-chain and, unlike the covenant tiers, needs the lender as an
active counterparty: Bitcoin has no covenants, so liquidation needs the oracle
to co-sign and origination is an exchange rather than one transaction. A
borrower does the whole of their side in the page; the lender's side is a
process that has to be running.

`pignus-btc-responder` runs `pignus-cli btc-respond --watch` from
`/root/sequentia/pignus-responder.json`. It draws each loan's secret and
publishes the hash both chains commit to, signs the release the borrower checks
before committing any collateral, pays the principal once that collateral is
confirmed, starts the loan once the borrower has claimed the principal, and
takes back a principal nobody claimed. Nothing secret goes on its command line —
`ps` is readable by every user on the box — so the key path and both nodes'
credentials live in that file, at mode 600.

It keeps its state in one file beside the key (`state` in the config). That file
is what stops a principal being paid twice after a crash: back it up with the
key, and never delete it while a loan is open.

`deploy/responder.example.json` is the starting point. The keys:

| key | default | what it does |
|---|---|---|
| `lender_key` | — | the key that signs every release, report and claim. This is the lender's whole side of every open loan |
| `state` | beside the key | what this responder has already done, written *before* each send |
| `book` | — | the relay it reads takes from and reports to |
| `interval` | 5 | seconds between passes over the queue |
| `disburse_conf` | 1 | Bitcoin confirmations on the borrower's collateral before the principal is paid |
| `claim_depth` | 6 | confirmations on the borrower's claim of that principal before their collateral is moved into the loan |
| `scan_interval` | 300 | seconds between chain scans for a repayment whose borrower never said where it landed |
| `fee_asset` | the debt asset | which asset pays the Sequentia-side fees. Any asset the node publishes a rate for; there is no privileged fee coin |
| `rpc` | — | the Sequentia node: `url`, one of `cookie` or `user`/`password`, and `wallet` |
| `btc_rpc` | — | the Bitcoin node, the same four fields |

`disburse_conf` and `claim_depth` are the lender's exposure to a reorg on the
other chain, which is why they are configuration rather than constants.

Where a loan stands, on both chains at once and from either side, from the
ticket file that names it:

```bash
pignus-cli btc-check loan.json
```

**Back up the lender key AND the state file.** Losing the key loses the ability
to claim repayments — borrowers can still take their collateral back once the
loans time out, so nobody is robbed, but the lender is. Losing the state file
without losing the key risks paying a principal twice.

Publishing an offer, from the lender's machine:

```bash
pignus-cli btc-offer-publish --config /root/sequentia/pignus-responder.json \
    --market BTC/USDX --oracle-x <x> --strike <price> \
    --btc-amount 100000 --debt-asset <id> --debt 5250000000 \
    --principal 5000000000 --lender-prog <hex> --lots 3 \
    --recover-after <btc-height> --abort-after <btc-height> \
    --repay-deadline <seq-height> --d-refund <seq-height>
```

Every offer is signed by the lender key it names, and the relay refuses one that
is not: an unsigned offer would let anyone publish in a lender's name and have
that lender's own responder pay it out. The same holds in the other direction —
every report the responder makes about a take is signed, and the borrower's page
checks it.

See the README for the whole command sequence, and design doc section 7 for the
trust model and what each party is exposed to at each step.

### Seizing cross-chain collateral

There is no covenant on the Bitcoin side, so a seizure is a 2-of-2 between the
lender and the oracle, and the oracle's signature is the decision. It is an
operator's command rather than an endpoint, and it refuses unless the oracle's
own published price is under the strike:

**What the oracle is really checking.** The strike is the number a seizure is
judged by, and it is in no Bitcoin script — Bitcoin cannot read it. So
rebuilding the sighash from the terms, which catches a lender who edits an
amount or a payout, does *not* catch one who raises the strike: the sighash
comes out byte for byte identical. The lender's signature over the offer the
loan was taken from is the only thing that pins it, so the request carries that
signature and the oracle refuses without it. A loan arranged entirely by hand
has no offer, and an operator who is willing to vouch for the terms themselves
can pass `--allow-unpinned-strike` — after which nothing holds the lender to
any strike at all.

```bash
# lender: build the request, which carries the loan and the offer signature
# that fixed its strike, not just a number
pignus-cli btc-seize-sighash loan.json --dest <btc address> --out seizure.json

# oracle operator: rebuild the sighash from the terms, check the strike against
# the lender's own signed offer, and co-sign
pignus-oracle --config /root/sequentia/pignus-oracle.json \
    --sign-seize --request seizure.json

# lender: broadcast, against the SAME script the request named
pignus-cli btc-seize loan.json --lender-key lender.key \
    --oracle-sig <hex> --dest-spk <hex> --btc-rpc ...
```

The co-signature and the attestation that justified it are appended to the
oracle's seizure log and published at `/v1/seizures`, so anyone can check
afterwards that the price really was under the strike. That publication is the
whole of this tier's accountability: nothing can refuse the signature at the
time it is made.

## Seeding the book

The page is only as useful as what is on it. Offers and loans can be put on the
live chain from a node wallet with the CLI, against the running book:

```bash
P=/root/sequentia/pignus/bin/pignus-cli
export PIGNUS_RPC_URL=http://127.0.0.1:18200
export PIGNUS_RPC_COOKIE=<node datadir>/.cookie   # or PIGNUS_RPC_USER/_PASSWORD
$P offer-fund --market GOLD/USDX --principal 100 --lots 3 --interest 3 \
    --open-ltv 50 --liq-ltv 75 --term-days 30 --rpc-wallet <lender wallet>
$P offer-take --offer <offer id> --rpc-wallet <borrower wallet>
$P repay --loan <loan id> --rpc-wallet <borrower wallet>
```

The node RPC credentials live in `pignusd.json` (mode 600) and nowhere else in
this runbook. Pass them through the environment, or with `--rpc-cookie` where
the node runs with cookie authentication; never type them onto a command line,
which every process on the box can read, or into a shell history you will paste.

Every command derives the address it acts on from the terms and checks it
against the coin first, prices the fee from the node's exchange rates in
whatever the wallet holds, and prepares explicit (unblinded) coins when the
wallet only has blinded change, which a covenant cannot spend. The book follows
the offer's coin and discovers the vault from the take witness, so nothing needs
to be told twice.

## What is NOT deployed, and why

No liquidator bot runs here. Liquidation is a permissionless race and anyone may
enter it; a platform whose liquidations depend on the operator's bot has an
operator. `pignus-liquidator` ships for anyone who wants to run one — the README
has the flags — and it is deliberately not part of the service.

`deploy/pignus-liquidator.service.example` is the unit for anyone who does want
one. Copy it to `/etc/systemd/system/pignus-liquidator.service` and edit the
paths, the taker address and the oracles; it is an `.example` rather than a
`.service` precisely so that the `cp deploy/*.service` in First install does not
install it. Run it by hand with `--dry-run --once` first:

```bash
/root/sequentia/pignus/bin/pignus-liquidator --book http://127.0.0.1:8741 \
    --oracle http://127.0.0.1:8740 --taker-address <your address> \
    --rpc-wallet liquidator --once --dry-run
```

It prints every loan it would close and the profit each one leaves, and moves
nothing. Its node credentials come from `PIGNUS_RPC_URL` and `PIGNUS_RPC_COOKIE`
in the unit rather than from the command line, because `ps` is readable by every
user on the box and this process runs for the life of the book. `--oracle` may
be repeated, and must be: a loan is judged against the key it bakes, so a loan
naming an oracle this bot does not watch is skipped.
