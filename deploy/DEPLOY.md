# Deploying Pignus

This is the runbook for the testnet deployment at `sequentiatestnet.com`, and
"the box" below is that server. Anyone running their own `pignusd` and oracle
needs the *`pignusd` configuration*, *Threshold oracles*, *The attestation
log*, *Backups* and *Checking it* sections, the `ReadWritePaths` caveat under
*First install*, and the systemd units in this directory; substitute your own
paths for `/root/sequentia/`.

The box pulls from GitHub and runs from there. Never edit source on the box and
never copy binaries onto it: build and check on a laptop, push, then `git pull`
on the box.

## What runs

| process | port | talks to | if it stops |
|---|---|---|---|
| `pignus-oracle` | 8740 | the price feed on :8088 | attestations stop within one tick; loans cannot be liquidated until it returns |
| further oracles | 8742, 8743 | the price feed on :8088 | an m-of-n loan loses a signer |
| `pignusd` | 8741 | node RPC :18200 (the box's node runs with `-rpcport=18200`; `pignusd` has no default, and the CLI and the liquidator default to 18776 when `PIGNUS_RPC_URL` is unset), registry :3005, the oracles | the page and the book go with it; nothing on chain is affected |
| `pignus-liquidator` | none (a client) | `pignusd` :8741, the oracles, node RPC :18200 | liquidatable loans stay open until somebody else runs a bot or a person liquidates; the timer's check says so |
| `pignus-btc-responder` | none (a client) | `pignusd` :8741, node RPC :18200, Bitcoin testnet4 RPC :48332 | cross-chain borrows stall: takes go unsigned, funded ones unpaid |
| `bitcoind` (testnet4) | 48332 | the Bitcoin network | cross-chain deadlines go unchecked, `/healthz` says `btc_node: false`, the page refuses to originate, and the responder cannot pay or start a loan |
| the price server | 8088 | upstream quotes | every oracle stops signing once its feed has come back byte-identical for `flat_rounds` rounds, or sooner if `feed_max_age` is set |
| `sequentia-registry` | 3005 | — | tickers fall back to the node's own asset labels |
| `pignus-alert@<unit>` | none (a oneshot) | the ntfy topic in `pignus-alert.env` | a unit's failure, or a change in the check's verdict, reaches nobody's phone; the journal still has it |

The price server is the node repository's `contrib/price-server`, the same one
that feeds the any-asset fee market; Pignus deliberately does not run a second
price pipeline.

The responder is a lender's own process, not part of the service: it holds the
key that signs releases and the wallet that pays principals. It is listed here
because the testnet runs one, and because a cross-chain offer nobody responds to
is an offer nobody can take.

Two systemd timers run beside those processes: `pignus-backup.timer` archives
daily everything that is not on a chain, and `pignus-check.timer` runs
`deploy/pignus-check.sh` every five minutes to ask whether every oracle is
signing, the book is being told, the responder and the liquidator are keeping
up, and no loan has been left liquidatable. Neither serves anything; both are
described below.

All of it is pure Python and needs no build step. It does need a Sequentia
**source** checkout, because the loan covenant lives in the node repository and
Pignus imports the proven builder rather than carrying a copy — the README's
*Getting it* section says where the checkout is looked for and how
`SEQUENTIA_SRC` names one.

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
cp /root/sequentia/pignus/deploy/oracle2.example.json   /root/sequentia/pignus-oracle-2.json
cp /root/sequentia/pignus/deploy/oracle3.example.json   /root/sequentia/pignus-oracle-3.json
cp /root/sequentia/pignus/deploy/pignusd.example.json   /root/sequentia/pignusd.json
cp /root/sequentia/pignus/deploy/responder.example.json /root/sequentia/pignus-responder.json
# edit them: the node RPC credentials go in pignusd.json and
# pignus-responder.json only, never on a command line. The Bitcoin node's
# cookie is <bitcoin datadir>/testnet4/.cookie; that path is btc_rpc.cookie
# in both files.
chmod 600 /root/sequentia/pignusd.json /root/sequentia/pignus-responder.json

/root/sequentia/pignus/bin/pignus-cli btc-keygen \
  --out /root/sequentia/pignus-data/lender.key

# pignus-responder.json's rpc.wallet must be loaded on the node (name it in
# the node's wallet=) and hold the debt asset of every lot on offer; the
# responder exits at start otherwise.

# the push channel a unit's failure and the check's verdict go to
# (*Alerting* below): a topic name nobody guesses, in a root-only file
printf 'NTFY_TOPIC=%s\n' "pignus-$(openssl rand -hex 8)" > /root/sequentia/pignus-alert.env
chmod 600 /root/sequentia/pignus-alert.env

systemctl daemon-reload
systemctl enable --now pignus-oracle pignus-oracle@2 pignus-oracle@3 pignusd pignus-btc-responder
systemctl enable --now pignus-backup.timer pignus-check.timer
systemctl start pignus-alert@test.service    # one test line reaches the topic
```

The three oracles are enabled together because the check timer names all
three; *Threshold oracles* below says what the second and third are for. The
liquidator is installed separately, under *The liquidator*.

Every unit runs as root with its state under `/root`, which is exactly where
`ProtectSystem=` does not reach — so each one mounts `/root` read-only instead
and hands back the single directory it has to write: `/root/sequentia/pignus-data`
for every service that writes (`pignusd`, each oracle instance, the responder;
the liquidator writes nothing), and `/var/lib/pignus-backup` for the backup, which
systemd creates from `StateDirectory=`. **Move one of those
paths and the matching `ReadWritePaths=` moves with it.** Otherwise the service
starts cleanly and fails at its first write, which for `pignusd` is hours later
and reads as a bug rather than as a mount.

The services' directory has to exist before they start, which is what the `mkdir`
above is for: the mount namespace is built before the unit's first command runs,
so a `ReadWritePaths=` naming a directory that is not there fails the unit
outright and nothing inside it can create one.

The oracle key is created 0600 at the `keyfile` path on a start where neither
the key nor the attestation log exists yet, and is never logged or served. A
keyfile missing beside an existing log is treated as a lost key and the
service refuses to start; a key restored without its log prints a warning that
every earlier co-signature will read as a stranger's. Restore key, log,
`.chain` and `.seizures` from the same archive. **Back it up.** What losing it
costs is under *Backups*.

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
    $(test -d /root/sequentia/pignus-btc-keys \
      && echo /root/sequentia/pignus-btc-keys) \
    /root/sequentia/pignusd.json \
    /root/sequentia/pignus-oracle*.json \
    $(test -f /root/sequentia/pignus-responder.json \
      && echo /root/sequentia/pignus-responder.json) \
    $(test -f /root/sequentia/pignus-liquidator.env \
      && echo /root/sequentia/pignus-liquidator.env) \
    $(test -f /root/sequentia/pignus-alert.env \
      && echo /root/sequentia/pignus-alert.env)
```

`pignus-btc-keys` is named as well as `pignus-data` because a lender key kept
there rather than in `pignus-data` would otherwise be the one file the archive
lacks. It, the responder's config and the liquidator's environment file are
named only if they exist: tar fails the whole archive on a missing path rather
than skipping it, and a box that runs no responder or no liquidator has
neither file. `pignus-backup.service` carries the same command.

Repairing a responder that has lost the answer to a send is under *Native
BTC collateral*.

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
Nothing here deletes old archives; prune `/var/lib/pignus-backup` on whatever
schedule the disk allows, after the copy-off has been checked.

What each piece costs to lose:

- **`oracle.key`** (and each extra oracle's) — every open loan against it
  becomes unliquidatable until maturity. See above.
- **`attestations.log`** and its rotated files — the audit trail. No coin is at
  risk, but a liquidation can no longer be checked afterwards, which is the
  whole of what bounds the oracle's trust.
- **`lender.key`** — the lender can no longer sign a release, disburse, start a
  loan or claim a repayment. A borrower whose loan never started aborts their
  pre-vault and loses nothing. A borrower whose loan IS live loses the
  collateral for good: RECLAIM needs the secret only a claim by this key
  publishes, and TIMEOUT needs this key's signature, so nothing can ever spend
  that vault again. They do get the debt back, at the repayment's own refund
  leaf. Back this key up like the money it is.
- **the responder's state file** — losing it while the key survives risks paying
  a principal twice, because the state file is the only record of what has
  already been sent. Restore it only from a copy newer than the last
  disbursement, and check the lender wallet's `listtransactions` against
  `/v1/btc/takes?status=disbursed` before you do.
- **`pignus-alert.env`** — the push channel's name. No coin is at risk; make a
  new topic, put it in a new file and subscribe again, or failures reach the
  journal only until you do.
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
    handle_path /pignus-oracle-2/* {
        reverse_proxy 127.0.0.1:8742
    }
    handle_path /pignus-oracle-3/* {
        reverse_proxy 127.0.0.1:8743
    }
    handle {
        reverse_proxy 127.0.0.1:8080   # the explorer: everything else
    }
}
```

`handle_path` strips the prefix, so the page's relative `v1/...` fetches reach
`pignusd` at `/v1/...`; the page's `/explorer/tx/<txid>` and
`/testnet4/tx/<txid>` links and `/pignus-oracle/v1/log` rely on this exact
layout.

**Every oracle gets a route, not only the primary.** A seizure is meant to be
checkable by anyone — the attestation behind it is at that oracle's own
`/v1/seizures` — and an m-of-n seizure is signed by oracles that are not the
primary. One with no public route is one whose seizures nobody outside can
check, which is most of what bounds an oracle's trust. Put the same addresses
in `oracle_public_urls` in `pignusd.json`, in the order `oracle` then
`oracles`, so `/v1/oracles` can tell a reader where to look; the book's own
loopback addresses are never served, because a browser told to fetch
`127.0.0.1` fetches from the reader's own machine.

`pignusd` sends its own security headers on every response -- a content
security policy that forbids scripts and connections from anywhere but itself
and forbids framing, `nosniff`, and no referrer -- so nothing here has to add
them. What Caddy should add is what a reverse proxy alone can: read and header
timeouts, so a client dribbling one byte a minute cannot hold a `pignusd`
thread open for ever (`servers { timeouts { read_header 10s read_body 60s } }`
in the global options block).

`caddy validate --config /etc/caddy/Caddyfile`, then `caddy reload --config
/etc/caddy/Caddyfile` — never restart, which would drop the other sites.

**`pignusd` must not be reachable except through Caddy.** It believes an
`X-Forwarded-For` header only from a peer in `trusted_proxies`, and keys its
rate limits on that header's last hop. A request that arrives from a trusted
peer *without* the header is taken for the box's own tooling — the responder
and the CLI on loopback — and is not rate-limited at all. Exposed directly,
every public client would arrive looking like that.

There are three limits, and they meter different costs. Writes are metered
because they change the book. Reads that touch the NODE — `/v1/spend`, which
walks blocks backwards, `/v1/outpoint`, `/v1/scan` and `/v1/btc/outpoint`,
which ask per call — are metered because they cost the node work, and all must
stay unauthenticated: a borrower recovering their own collateral has no account
here. The four whole-book listings are metered at two a second per client
because each is a full render when the cache misses; the page asks for each
once every thirty seconds. Every other read comes out of memory and is not
limited. `docs/api.md` carries the figures.

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
systemctl restart pignus-oracle pignus-oracle@2 pignus-oracle@3 pignusd pignus-btc-responder pignus-liquidator
curl -s localhost:8741/healthz | jq -r .git_rev; git rev-parse --short HEAD
```

Every Pignus process runs from this one checkout, so every one of them is
restarted; a unit left on the old code keeps the old relay protocol and the old
covenant pin. The last line must print the same short hash twice.

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
/root/sequentia/pignus/deploy/pignus-check.sh   # the whole answer, yes or no;
                                                # by hand it records no verdict and pages nobody
curl -s -o /dev/null -w '%{http_code}\n' localhost:8740/healthz   # 503 if the oracle is not signing
curl -s localhost:8741/healthz | jq '{ok, error, git_rev, covenant_vectors}'
curl -s localhost:8741/v1/markets | head -40
curl -s "localhost:8741/v1/btc/takes?status=pending"  # and ?status=requested; both are empty if the responder keeps up
journalctl -u pignus-btc-responder -f
SEQUENTIA_SRC=/root/sequentia/Sequentia \
  /root/sequentia/pignus/tests/cli_drill.sh
```

`pignus-check.sh` is those checks, and more, as one command that exits 0 or 1:
every oracle's `/healthz` and the book's must say `ok`, every market that is
not cross-chain must have a price signed within `PIGNUS_MAX_PRICE_AGE`
seconds, no market may disagree with the registry about its assets' decimals,
every key the book quotes must belong to a listed oracle, a liquidator unit
that is installed must be running, and no loan may sit under its strike and
still open. `pignus-check.timer` runs it every five minutes, and
`pignus-check.service` carries its settings as environment. `PIGNUS_ORACLES`
is every oracle's URL, space separated and quoted; the shipped unit names
8740, 8742 and 8743. `PIGNUS_MAX_PRICE_AGE` stays equal to `max_price_age` in
`pignusd.json`: a check looser than the book's own limit reports healthy
while the book is already withholding prices. `PIGNUS_MIN_FREE_MB` (512) is
the floor for free space on the disk under `PIGNUS_DATA_DIR`
(`/root/sequentia/pignus-data`), where every service writes; a disk that
fills stops the book, the attestation logs and the responder's state file
before anything else says so. `PIGNUS_RESPONDER_CONFIG` is
commented out in the shipped unit; on a box that runs a responder, set it in
a drop-in (`systemctl edit pignus-check.service`, then `[Service]` /
`Environment=PIGNUS_RESPONDER_CONFIG=/root/sequentia/pignus-responder.json`).
With it set, the check fails when `pignus-btc-responder` is not running, when
`btc-responder-status` exits 4 (a take waiting on a person, or an offer whose
signature no longer verifies), when the book could not be read to check the
offers, and when a take has sat `requested` for over a minute; it skips the
responder when the named file does not exist.

Cross-chain rows are left out of the *age* half deliberately. A native-BTC
seizure is co-signed by an operator running a command, and the oracle refuses on
the spot against a stale price, so the person acting sees it. On a covenant
market nobody is looking: every liquidator in the race stops, and nothing says
so. The decimals are checked on every row, cross-chain included, because a
market whose oracle and registry disagree about them is priced out by a power of
ten and no signature check anywhere can see it.

The oracle's `/healthz` answers **503**, not 200, when it has not completed a
signing round within two intervals (thirty seconds where that is longer), with
`stale`, `errors` and `round_error` saying which market and why. A green oracle
answers 200. The process staying up while its signing thread is dead is exactly
the outage that otherwise goes unnoticed until it reaches the RECOVER backstop.

`pignusd`'s `/healthz` reports `ok: false` with the reason when there is no
node, when the node or the **primary** oracle is unreachable, while the first
sync runs, when the poll thread has not finished for `max(120s, 3 × poll)`,
when any market's newest verified attestation is older than `max_price_age`,
and for the other causes `docs/api.md` lists under `/healthz`: a record that
cannot be rendered, an unapplied offer backlog, a node that stopped answering
partway through a poll, a rescan owed.
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

`flat_rounds` is the third guard and needs nothing from the feed: when every
market has come back byte-identical for that many rounds (30 by default, 0 to
turn it off, a static source exempt), the oracle calls the feed frozen and
stops signing until it moves, and `/healthz` turns 503. A feed whose upstream
died keeps answering 200 with last week's numbers, and nothing else here can
tell.

`feed_max_age` is **off unless you set it**, and it needs the feed to publish
`_meta.updated`. A feed that does not cannot be checked at all — it could be
frozen at a price from a week ago and look perfectly current — so an oracle
asked for that check against such a feed refuses to sign rather than treat an
unanswerable question as a yes. The node repository's
`contrib/price-server/mock-price-api.py` does not publish the field, so the
shipped example configs leave the setting out; set it only against a feed that
does.

## `pignusd` configuration

`deploy/pignusd.example.json` is the starting point. The keys:

| key | default | what it does |
|---|---|---|
| `listen` | `127.0.0.1:8741` | where to serve |
| `book` | — | the book file. Rewritten atomically (temporary file, fsync, rename). A book that is not valid JSON stops `pignusd` rather than being replaced with an empty one: restore it from a backup, or move it aside to start empty on purpose |
| `oracle` | — | the PRIMARY oracle: its key is the one this book hands lenders, and its prices are the ones shown |
| `oracles` | `[]` | further independent oracles, quoted for m-of-n loans. They never stand in for the primary, because a vault verifies against the key baked into it |
| `oracle_keys` | `[]` | the key each oracle is expected to serve, in the same order as `oracle` then `oracles` (an empty entry pins nothing). Unpinned, the book adopts whatever an oracle's `/v1/pubkey` serves on every poll, so a lost-and-recreated key or one substituted in transit is accepted while every loan baked to the old key goes unpriced. Pinned, a different key is an `oracle_errors` entry and its attestations are ignored |
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
| `btc_rpc` | — | the parent chain's node: `url` and one of `cookie` or `user`/`password`; it never spends, so no wallet is needed and an empty `wallet` is ignored. Only for cross-chain loans. Without it the Bitcoin half of every cross-chain deadline goes unchecked here, `/healthz` says `btc_node: false`, no `btc_height` is published, and the page refuses to originate rather than check half the timelocks. One that is configured and stops answering makes `ok` false, with that reason |
| `explorer_url` | — | where the page's breadcrumb points. A self-hosted book is not behind the testnet's reverse proxy, so it can say where its explorer really is |
| `explorer_tx_url` | `/explorer/tx/{txid}` | the link a Sequentia transaction id becomes on the page |
| `btc_explorer_tx_url` | `/testnet4/tx/{txid}` | the same for a parent-chain transaction |
| `oracle_public_url` | — | the primary oracle's public ADDRESS, the same shape as an entry in `oracle_public_urls`; the page links to its `/v1/log` |
| `oracle_public_urls` | `[]` | where each oracle can be reached from OUTSIDE, in the same order as `oracle` then `oracles`. Served by `/v1/oracles`; the loopback addresses above never are, since a browser told to fetch `127.0.0.1` fetches from the reader's own machine. An m-of-n seizure is signed by oracles that are not the primary, and the attestation behind it is at that oracle's `/v1/seizures`, so a threshold oracle with no public address is one whose seizures nobody outside can check |

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

(The same lines are in *First install*; they are repeated here for a box
that started with one oracle.)

Those two files are `oracle.example.json` with a different `keyfile`, `logfile`
and `listen`; `diff` them to see what else differs. The primary keeps
its own unit, `pignus-oracle.service`, because its key is the one the book hands
lenders — it is named rather than numbered.

**Distinct keyfiles are what makes the set independent**: three instances
sharing one key are a 1-of-1 wearing a 2-of-3 label. Print each key with
`--print-pubkey`, make sure `PIGNUS_ORACLES` covers them -- the shipped unit
already names 8740, 8742 and 8743; anything else goes in a drop-in
(`systemctl edit pignus-check.service`) -- and confirm `/v1/oracles` on the
book lists them all.
The backup already covers the extra keys and configs, which are under
`pignus-data` and match the `pignus-oracle*.json` glob.

**Every new offer made with `--oracles book` names every oracle the book
quotes, n-of-n unless `--oracle-threshold` says otherwise.** So the moment a
URL lands in `oracles`, that oracle's uptime is part of every such loan's
liquidation: a frozen feed, an unanswerable `feed_max_age`, a clock out by a
minute, and no lender can liquidate until it is back. Tell a joining
operator that, and set a threshold below n when the set is bigger than two.

## Joining a book as a signer

An operator running an oracle on their own machine, to be quoted by a book
they do not run:

1. Copy `oracle.example.json`, point `source.url` at a feed of your own
   (https, or loopback), set `precisions` and `price_scale` to exactly what
   the book's `/v1/markets` shows for the markets you will sign
   (`collateral_precision`, `debt_precision`, `price_scale`): an attestation
   at another scale or precision is a good signature over a number off by a
   power of ten, and the book drops it or, worse, believes it. Run NTP.
2. Start the service; on a fresh install it creates the key and says so. Back
   the key up, and the attestation log with it, from that moment.
3. Send the book's operator your public https URL and the key from
   `--print-pubkey`. They add the URL to `oracles` and `oracle_public_urls`
   in `pignusd.json` and to `PIGNUS_ORACLES` (in a drop-in on
   `pignus-check.service` if the shipped default does not cover it), and
   restart `pignusd`.
4. Confirm `/v1/oracles` on the book lists your key and URL, and that
   `/healthz` there shows no `oracle_errors` naming you. The book reads your
   `/v1/pubkey`, `/v1/markets` and `/v1/attestation/*` every `poll` seconds;
   a rate limit or a firewall between you and it is an outage.

## A compromised oracle key

A rotation keeps the old key signing until the last loan that bakes it
matures. A compromised key must not: whoever holds it can sign any price and
liquidate every covenant loan baked to it, and co-sign any cross-chain
seizure with a cooperating lender.

1. Stop the instance.
2. Start the successor with the old key in both `previous_keys` and
   `compromised_keys` -- a book honours a declaration only for keys the
   declarer lists as its own, and the oracle refuses to start otherwise;
   `/v1/pubkey` publishes it, and every book that quotes the successor
   refuses attestations under it from the next poll.
3. Tell the book's operator, so they drop the old URL from `oracles`.
4. Borrowers of loans that bake the key REPAY: the page flags those loans,
   and nothing else protects them.

Independence here is of **keys**, not of prices. Every oracle in the example
configuration reads the same price server, so a stale or manipulated feed
produces the same wrong number under all three keys and a 2-of-3 loan liquidates
on it. Feed independence means giving each instance a different `source.url`,
pointing at genuinely different upstreams.

## The liquidator

`pignus-liquidator.service` runs one liquidation bot against the book, from
the `treasury2026` wallet, so a loan that crosses its strike is closed and
the timer's "no loan has crossed its strike and been left there" check stays
green. It is one participant among however many choose to run one: the
covenant does not know which liquidator wins. The unit takes its credentials
and its taker address from `/root/sequentia/pignus-liquidator.env` (mode
0600, box-only, never in git), and names all three oracles so an m-of-n
loan's price is verified against every key it may bake. Run it by hand with
`--once --dry-run` first after any change to the wallet or the oracles. The
bot refuses to decide a seizure when the node publishes no fee rate for the
loan's debt asset, because it cannot restate a fee paid in another asset;
`/v1/fees` on the book lists the rates the node has. At start it keeps
trying for two minutes (`--start-grace`) when the book or an oracle does not
answer yet, since every Pignus process is restarted together after an update
and the bot must not fail, and page, for coming up first.

Installing it is four steps. `cp
/root/sequentia/pignus/deploy/pignus-liquidator.service.example
/etc/systemd/system/pignus-liquidator.service` -- it ships as an `.example`
so that the `cp deploy/*.service` in *First install* does not install it
unasked; write `/root/sequentia/pignus-liquidator.env` with
`PIGNUS_RPC_URL=http://127.0.0.1:18200`, either `PIGNUS_RPC_COOKIE=<node
datadir>/.cookie` or `PIGNUS_RPC_USER=` and `PIGNUS_RPC_PASSWORD=`,
`PIGNUS_RPC_WALLET=treasury2026` and `PIGNUS_TAKER_ADDRESS=<an address of that
wallet>`, then `chmod 600` it; `systemctl daemon-reload && systemctl enable
--now pignus-liquidator`. The wallet must be loaded on the node (name it in
the node's `wallet=`) and the taker address must belong to it: the bot exits
1 at start on either. `--oracle` may be repeated, and must be: a loan is
judged against the key it bakes, so a loan naming an oracle this bot does not
watch is skipped.

## Alerting

Every long-running Pignus unit carries `OnFailure=pignus-alert@%n.service`,
and the timer's check calls the same script itself, when its verdict changes
-- the first failing run, and the run that passes again -- rather than through
`OnFailure=`, which would repeat the news every five minutes; a person hears
once. A crash of the check before it reaches a verdict is alerted from its
exit trap. `deploy/pignus-alert.sh` posts one line to an ntfy topic, a push
channel a phone or a browser subscribes to with no account: the box needs
`curl` and the topic's name, nothing else.

```bash
cat > /root/sequentia/pignus-alert.env <<EOF
NTFY_TOPIC=<a name nobody guesses>
EOF
chmod 600 /root/sequentia/pignus-alert.env
systemctl daemon-reload
systemctl start pignus-alert@test.service     # one test line arrives
```

Subscribe at `https://ntfy.sh/<topic>` (the ntfy app, or the page itself in a
browser tab). `NTFY_URL` in the same file points at a self-hosted ntfy;
`ALERT_PREFIX` sets the message's title. The same message -- judged by its
first three words, since a check's line carries counts that change every run
-- is not sent twice within ten minutes (`PIGNUS_ALERT_MUTE` seconds), so a
unit that crash-loops
every thirty seconds pages once per ten minutes and a flapping check does the
same; a different message -- a recovery, another unit -- always goes out. The
topic is the whole of the channel's secrecy, so it is in the backup and
nowhere in git. Without the
file the script prints to the journal and exits 0: a box that has not set a
topic up loses the push and nothing else.

## Rotating the oracle key

A vault bakes the key it was originated against, so a rotation is never a swap:
the old key must keep signing until the last vault that names it has matured.

1. Run the new key as another `pignus-oracle@<n>` instance, on its own port with
   its own keyfile and logfile.
2. Add it to `pignusd.json` as the new primary: `oracle` becomes its URL, the
   old URL moves to the front of `oracles`, and `oracle_keys`,
   `oracle_public_urls` and `oracle_public_url` are reordered to match. Give
   it a Caddy route and make sure `PIGNUS_ORACLES` covers its port. Restart
   `pignusd`. The old instance stays enabled and goes on signing.
3. List what still depends on the old key. That is **both** tiers, and the
   cross-chain one matters more: there the oracle's signature *is* the
   liquidation, with no script that can stand in for it, so retiring a key
   still named by a live cross-chain loan takes that loan's liquidation away
   from the lender for good.

   ```bash
   curl -s "http://127.0.0.1:8741/v1/loans?oracle_x=<old key>" | jq '.loans|length'
   curl -s "http://127.0.0.1:8741/v1/btc/takes?oracle_x=<old key>" \
     | jq '[.takes[]|select(.status|IN("live","disbursed","signed"))]|length'
   ```

   Every one of those must reach maturity, be repaid, or be settled before the
   old instance stops.
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

Its state file defaults to `<lender_key>.state.json`, beside the key. That
only works because the shipped layout keeps `lender.key` inside `pignus-data`,
the one directory the unit may write; a key kept anywhere else
(`pignus-btc-keys`, say) needs `state` pointed back into `pignus-data`, or the
responder refuses to start and says so. That file
is what stops a principal being paid twice after a crash: back it up with the
key, and never delete it while a loan is open.

`deploy/responder.example.json` is the starting point. The keys:

| key | default | what it does |
|---|---|---|
| `lender_key` | — | the key that signs every release, report and claim. This is the lender's whole side of every open loan |
| `state` | `<lender_key>.state.json` | what this responder has already done, written *before* each send |
| `book` | — | the relay it reads takes from and reports to |
| `interval` | 5 | seconds between passes over the queue |
| `disburse_conf` | 2 | Bitcoin confirmations on the borrower's collateral before the principal is paid. Two is the shortest depth that survives an ordinary one-block reorg, the same reasoning every other cross-chain step here applies |
| `claim_depth` | 6 | confirmations on the borrower's claim of that principal before their collateral is moved into the loan |
| `scan_interval` | 300 | seconds between chain scans for a repayment whose borrower never said where it landed |
| `fee_asset` | the debt asset | which asset pays the Sequentia-side fees. Any asset the node publishes a rate for; there is no privileged fee coin |
| `rpc` | — | the Sequentia node: `url`, one of `cookie` or `user`/`password`, and `wallet`. The wallet must be loaded on the node -- name it in the node's `wallet=` configuration so a restart reloads it -- and hold the debt asset for every lot on offer plus an asset with a published fee rate. The responder refuses to start when it cannot reach it |
| `btc_rpc` | — | the Bitcoin node: `url` and one of `cookie` or `user`/`password`; it never spends, so the example's empty `wallet` is ignored. Without it the commands fall back to `http://127.0.0.1:8332`, which is mainnet's port; the example's `48332` is testnet4 |

`disburse_conf` and `claim_depth` are the lender's exposure to a reorg on the
other chain, which is why they are configuration rather than constants.

Where a loan stands, on both chains at once and from either side, from the
ticket file that names it:

```bash
pignus-cli btc-check loan.json
```

**Back up the lender key AND the state file** -- see "What each piece costs to
lose" under *Backups*.

A take this key can do nothing more about -- one from before the format that
lets its loan be rebuilt, a principal that went to a plain address no covenant
ever bound -- is written off, on the record: `pignus-cli btc-responder-clear
--config … --take <id> --write-off "<why>"`, with the responder stopped, since
the command takes its lock. The take is then reported as written off rather
than as needing a person, the responder acts on it no more, and an offer
served under an old id stops counting it as money in flight. A take with a
paid principal and no claim of its repayment needs `--force` as well: written
off, no pass will claim that repayment, and walking away from it is a
decision, not a side effect. The relay knows nothing of a write-off: a
written-off take that had reached `disbursed` goes on holding one lot of its
offer there, so if the offer is still open and its lots matter, withdraw it
and publish it again.

A take that is simply over needs no write-off. One signed but never paid into,
whose `d_refund` has passed, will never be paid into by this key, and the
borrower takes their collateral back at their own height: the responder clears
its wait itself, whatever its offer verifies as today, and the status stops
listing it.

### Reading and repairing a responder

**Reading and repairing a responder.** `pignus-cli btc-responder-status
--config /root/sequentia/pignus-responder.json` prints every take that key has
touched, what stage it reached, what it is waiting on and for how long. It only
reads, so it is safe against the running unit, and it exits 4 when something
needs a person.

Most reasons a take waits on clear on their own within a block or two — a
pre-vault not yet confirmed, an anchor not yet buried — so one that has sat on
the same reason for hours is on a reason that will not clear: an upgrade fee
fixed below what the parent chain now charges, a deadline already too close, a
secret this key never had. Each has a borrower's collateral committed behind
it, so those are reported too, with `--waiting-hours` setting how patient to
be. A reason like that repeats for every take of the same offer, so the answer
is usually to withdraw that offer and publish a new one, and to tell the
borrower they may abort their pre-vault and take their Bitcoin back.

The one thing a responder cannot repair for itself is a send it recorded as
in-flight and then lost the answer to — the flag is deliberately left set,
because clearing it blind would pay a second principal. `btc-responder-clear
--take <id>` is how that is told: it takes the responder's own lock, so it
refuses while the unit is running (stop it first), and it looks at the chain
before it changes anything. If the payment IS there, it records it rather than
clearing, with `--found <txid>:<vout>`.

An offer served under an id that is not the hash of its terms is reported
separately, as `offers_under_an_old_id`: the responder skips it for the same
reason, and re-signing cannot repair it, since the id is the record's key. A
principal already paid under one is refunded with `btc-refund-principal` once
its `d_refund` opens; with nothing in flight, withdraw the offer and publish
the coin again. Neither the waiting-takes report nor `offers_under_an_old_id`
names an offer that is withdrawn with nothing in flight under it, which is
history rather than something to act on.

The other repair is an offer whose signature no longer verifies. A responder
checks that signature before acting on any take of the offer, so one that stops
verifying stops every loan under it — live ones included, whose borrowers then
never get their collateral back — and the responder cannot tell that apart from
an idle night. Add `--book http://127.0.0.1:8741` to `btc-responder-status` and
it names any such offer and exits 4; `pignus-cli btc-offer-resign --offer <id>
--config /root/sequentia/pignus-responder.json` repairs it by handing the book
a fresh signature over the terms it already holds. That is the one thing it can
change: the book verifies the new signature over the stored payload before
accepting it, so no term can move and nobody without the key can use it.

Publishing an offer, from the lender's machine:

```bash
pignus-cli btc-offer-publish --config /root/sequentia/pignus-responder.json \
    --market BTC/USDX --oracle-x <x> --strike <strike from quote> \
    --btc-amount 100000 --debt-asset <id> --debt 5250000000 \
    --principal 5000000000 --lender-prog <hex> --lots 3 \
    --recover-after <btc-height> --abort-after <btc-height> \
    --repay-deadline <seq-height> --d-refund <seq-height>
```

`<x>` is the first entry of `curl $BOOK/v1/oracles`, `<id>` the debt asset id
from `curl $BOOK/v1/markets`, `<hex>` from `pignus-cli payout-program
--rpc-wallet <name>`, and the strike from `pignus-cli quote`. The four heights
are judged against the tips in `/v1/markets` (`height`, `btc_height`) by the
command, by the relay and by every responder: `d_refund` at least two hours
ahead, `abort_after` a day after it, `repay_deadline` a day after `d_refund`
plus the two-hour claim margin, `recover_after` a day after `repay_deadline`
and past `abort_after`. An offer leaves the board two hours before `d_refund`.

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
# lender: build the request, which carries the loan, the offer signature that
# fixed its strike, and the borrower's own acceptance of that offer (fetched
# from the take on the book, which is why --book is given)
pignus-cli btc-seize-sighash loan.json --dest <btc address> --out seizure.json \
    --book http://127.0.0.1:8741

# oracle operator: rebuild the sighash from the terms, check the strike against
# the offer the BORROWER signed for -- the lender's own signature alone pins
# nothing, since the lender could sign again over any strike -- and co-sign
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

On the box the node RPC credentials live in three 0600 files --
`pignusd.json`, `pignus-responder.json` and `pignus-liquidator.env` -- and
nowhere else. Pass them through the environment, or with `--rpc-cookie` where
the node runs with cookie authentication; never type them onto a command line,
which every process on the box can read, or into a shell history you will paste.

Every command derives the address it acts on from the terms and checks it
against the coin first, prices the fee from the node's exchange rates in
whatever the wallet holds, and prepares explicit (unblinded) coins when the
wallet only has blinded change, which a covenant cannot spend. The book follows
the offer's coin and discovers the vault from the take witness, so nothing needs
to be told twice.
