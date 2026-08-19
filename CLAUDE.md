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
than the node. They need `SEQUENTIA_SRC` and a built `sequentiad`.

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

- Design and security analysis: `doc/sequentia/pignus-design.md` in the node
  repository. It is the authority on the trust surface, the replay window, the
  64-bit bound and the collateral tiers; this repo's README summarises it.
- `openamp-design.md` (restricted assets, Tier C) and
  `simplicity-dex-covenant-offers-design.md` (where the output-map and
  self-replication techniques came from), both in the node repository.
