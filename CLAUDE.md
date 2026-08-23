# Working on Pignus

Pignus is non-custodial collateralised lending on Sequentia. Borrow USDX against
GOLD, SILVR, OILX, tSEQ, native BTC or any other unrestricted issued asset; the
loan's terms are compiled into a covenant and enforced by the script interpreter
rather than by an operator.

## The one thing to understand before changing anything

**The covenant is NOT in this repository.** It lives in the node repository, at
`test/functional/pignus_covenant.py`, and is proven against a real node by
`test/functional/feature_pignus_vault.py`. This is the same split SeqOB uses:
the covenant is a consensus-level artifact and ships with the node; the daemon
that drives it ships separately.

`pignus/compat.py` imports that builder and checks it against
`pignus/vectors.json` before use. It will refuse to derive an address from a
builder that has drifted. **Do not "fix" that by writing a second Python
implementation.** A port that differs by one byte derives a different taproot
address, and the failure mode of a wrong vault address is collateral that nobody
can ever spend. The vectors exist for implementations that genuinely cannot
import Python -- `web/pignus.js` for the browser is one, and it pins itself to
the same vectors.

So this repo needs a Sequentia **source** checkout. Set `SEQUENTIA_SRC`, or keep
one at `../Sequentia` or `~/Sequentia`.

## Regenerating the vectors

Only when the covenant itself changes, and in the same change as the node-repo
commit that changes it:

    cd <sequentia> && PYTHONPATH=test/functional \
        python3 test/functional/gen_pignus_vectors.py > <pignus>/pignus/vectors.json

Then re-run both repos' tests. A vectors change that is not accompanied by a
node-repo covenant change means someone has broken the pinning.

## Tests

    tests/cli_drill.sh          offline, no node, ~2s -- run this first
    tests/run-tests.sh          everything, including the node integration tests

`tests/test_platform.py` and `tests/test_btc_collateral.py` run on the node's
functional-test framework but live here, because they test this library rather
than the node. They need `SEQUENTIA_SRC` and a built `sequentiad`;
`test_btc_collateral.py` also needs a Bitcoin Core `bitcoind` (`PIGNUS_BITCOIND`,
default `~/bitcoin-28.0/bin/bitcoind`), and the `tests/*.mjs` browser checks need
Node.

`tests/test_lifecycle.py` is the one to run after touching the daemon, the
watcher, the CLI or `pignus/vault.py`: it drives the CLI through a whole
lifecycle against a real node with the real oracle and daemon running, and
checks the book's view at every step -- including an under-water seizure,
which the node's own covenant test does not cover.

Three things the chain does that are easy to get wrong here, all learned the
hard way:

- **A node wallet's change is blinded.** Every input of a covenant transaction
  must be explicit, and the wallet blinds change the moment it has ever held a
  blinded coin. `select_funding()` therefore pays the wallet's own
  unconfidential address the exact amount first. Never "fix" a `short` error
  by passing blinded coins through.
- **Under water, output `2k+1` must not be the collateral asset.** The
  covenant's return probe treats any collateral-asset output there as a return
  to the borrower and then demands the borrower's program. Both composers put
  the fee output there; keep them in step.
- **`borrower_ver` is part of an offer's address.** The take leaf rebuilds a
  vault for a 20- or 32-byte borrower program, so dropping the version when
  deriving an offer address makes the book disagree with the browser for
  every extension-wallet lender.

Every test asserts the **refusals**, not just the successes. A lending covenant
that accepts the honest case is worth nothing on its own; what matters is that
it refuses underpayment, redirected payouts, forged attestations, replayed
attestations, and one payment settling two loans. Keep it that way: a new
feature lands with the ways it must fail, or it does not land.

## Money-safety rules that are not negotiable

- **`verify_funding()` before signing, always.** Rebuild the vault address from
  the terms and compare it to the output being funded, and assert the internal
  key is NUMS. Every non-custodial claim reduces to this check. Any code path
  that asks a user to sign an origination without it has silently reintroduced a
  trusted party.
- **Verify attestations locally.** The oracle is trusted for a number, not for
  the transport that carried it. Never act on a price fetched over HTTP without
  checking the signature against the vault's own baked oracle key.
- **Never treat a loan as originated until its funding is buried.** Sequentia
  reorgs when Bitcoin reorgs, in real time. The watcher reports a vault whose
  funding was undone as GHOST, and that is correct, not a bug to paper over.
- **Explicit amounts only.** The covenant asserts every introspected asset and
  value prefix is `0x01`. A blinded output it cannot read is refused rather than
  guessed at.

## Secrets

Repos here are public. Never commit oracle private keys, `wallet.dat`, RPC
credentials, `.env` files or tokens. The oracle key file is created 0600 and its
mode is re-checked on every start; keep it that way. Scan the diff before every
commit.

## Deployment

`deploy/DEPLOY.md`. The box pulls from GitHub and builds there -- never edit
source on the box, never copy binaries onto it.

## Related

- Design and security analysis: `docs/pignus-design.md` here. It is the
  authority on the trust surface, the replay window, the 64-bit bound and the
  collateral tiers; this repo's README summarises it.
- `openamp-design.md` and `opendamp-design.md` (restricted assets, Tiers C and
  D) in the node repository's `doc/sequentia/`, and
  `simplicity-dex-covenant-offers-design.md` (where the output-map and
  self-replication techniques came from) in the `seqdex` repository's `docs/`.

<!-- BEGIN SHARED AGENT CONVENTIONS: identical in every Sequentia repo. Change it in all of them together. -->
## Working with git and GitHub here

These rules are the same in every Sequentia repository. They are repeated in each
one because this file is the only thing an agent is guaranteed to read, whatever
machine it is working from.

**Nothing pushed to GitHub credits Claude, Anthropic, or any AI tool.** No
`Co-Authored-By: Claude` trailer, no `Claude-Session:` trailer or `claude.ai`
link, no "Generated with Claude Code" in a commit message or a pull request body,
no `claude/*` branch names or session ids, and no mention in source, comments,
docs or issue text. Agent tooling offers several of these by default; compose the
message without them rather than stripping them afterwards.

**Author every commit as**
`GracedEternalKingCabbageMan <151803062+GracedEternalKingCabbageMan@users.noreply.github.com>`.
Never a personal address.

**Every change lands through a pull request that you merge yourself, at once.**
There is no reviewer on this project; the pull request exists so the reasoning is
recorded beside the diff. Branch, push, open it, merge it, delete the branch, all
in one sitting. Pushing straight to the default branch is the rule most often
broken here, and it is the one that costs the record. A pull request stays open
only when the repository owner asks for that specific one, and that never carries
over to the next.

**Name branches `area/short-description`**: `fix/`, `doc/`, `feature/`, `test/`,
`build/`, or the component being changed. Never a tool name, a session id, or
`worktree-*`.

**Write the subject as `area: what changed`**, one line, 72 characters at the
outside and 50 where you can manage it. Put the reasoning in the body, and
explain why rather than what.

**These repositories are public and world-readable.** Never commit private keys,
seeds, `wallet.dat`, RPC credentials, `.env` files or API tokens. Read the diff
before every commit. Secrets belong on the server and in offline backups.

**A file belongs to the repository whose code it describes.** Decide which repo
owns it before writing it; if it landed in the wrong one, move it rather than
deleting it.

**Push the same day you commit.** The testnet server pulls only from GitHub, so a
branch left on one laptop is invisible to every other machine and to the box.
<!-- END SHARED AGENT CONVENTIONS -->

## Native BTC collateral is a different animal

Tier B (borrow a Sequentia asset against real Bitcoin) is NOT a covenant loan
and does not behave like one. Bitcoin has no introspection, so none of the loan
covenant runs there: the collateral is a plain Bitcoin taproot UTXO, the debt is
on Sequentia, and the two are bound by a BIP340 adaptor signature. Consequences
worth keeping straight:

- **The lender cannot be offline.** A funded covenant offer lets the lender walk
  away because the script reconstructs everything; the Bitcoin side cannot, so
  origination is a two-party handshake (the ticket in `pignus/btc_collateral.py`)
  and liquidation needs the oracle to co-sign a Bitcoin transaction. This is why
  Tier B is CLI-first: a two-party interactive protocol is what a CLI is for.
- **The browser does the keyless half.** `web/btc.js` (Bitcoin taproot: address,
  BIP341 sighash, witness) and `web/adaptor.js` (adaptor verify + decrypt) are
  faithful ports of `pignus/btcscript.py` and `pignus/adaptor.py`, pinned
  byte-for-byte to `web/btc_vectors.json` / `web/adaptor_vectors.json` (emitted
  by the proven Python) before they derive anything. A borrower can rebuild the
  funding and repayment addresses and verify the lender's release entirely in
  the page. Regenerate the vectors only when the Python changes, in the same
  commit, and re-run `test_btc_web.mjs` / `test_adaptor_web.mjs`.
- **The SWK wasm's `adaptorSign` is a DIFFERENT scheme** (the DEX swap spec),
  not interoperable with `pignus/adaptor.py`. Do not wire it into the Pignus
  flow. A browser LENDER (adaptor-sign + the Sequentia claim) would need either
  Pignus's adaptor added to the wasm or the CLI; the borrower path needs only a
  plain BIP340 signer, which the current bundled wasm does not expose either --
  exposing Bitcoin signing to dapps is a wasm rebuild, tracked separately.
- **`anchor_safe` before acting on a revealed `t`.** The claim that reveals `t`
  is a Sequentia transaction, and Sequentia reorgs when Bitcoin reorgs; a
  borrower must not spend BTC on the strength of a `t` that a reorg could undo.
