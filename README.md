# LendingProtocol

A collateralized lending Intelligent Contract for [GenLayer](https://genlayer.com), where the credit limit and liquidation threshold on every position are set by a **live vote of the validator committee**, not a static formula.

- **Contract:** `0x96A553d2d7B677b5F6ec510134D5C5f30E20f2e9` (studionet, chain 61999)
- **Constructor args used:** dex `0x9D5D33AF40781B6A41E3865df7B9bEF36adc6005`, pool `0x6A732A632972fC3cF8a76b3CfeE3356C549c761C`, collateral token (tGEN) `0xd978F743Ce2Bad27c00A329F44f8F16b401F556C`, debt token (tUSDC) `0xa04E4F945d941eD491C194E2BD29A4da06c37f07`, `ltv_bps=7500`, `liquidation_bps=8000`

## What it does

Users supply `tGEN`-style collateral and can borrow a debt asset (`tUSDC`-style) against it, up to a loan-to-value cap. Collateral is priced by a live cross-contract quote from an external AMM pool. On top of that raw market price, the protocol applies a **haircut** decided by GenLayer's validator committee — and that haircut is what makes this a genuine Intelligent Contract rather than a deterministic lending formula ported to a new chain.

- `supply(amount)` — deposit collateral
- `borrow(amount)` — borrow against supplied collateral, gated on a settled risk verdict
- `repay(amount)` — pay down debt
- `withdraw(amount)` — withdraw collateral, re-checked against LTV
- `fund(amount)` — add lendable liquidity (no LP shares, no yield — see [Scope](#scope) below)
- `liquidate(user)` / `advance_liquidation(liq_id)` — seize and sell an underwater position

## Why this needs GenLayer

`assess_risk()` runs inside a **non-deterministic block**. Every validator independently:

1. Fetches the **live** crypto Fear & Greed index over the web from `api.alternative.me`
2. Weighs that live reading against deterministic on-chain evidence (collateral price, price movement, pool depth, protocol exposure)
3. Returns a verdict: `CALM`, `CAUTION`, `STRESS`, or `CRISIS`

Because the off-chain reading moves over time and between fetches, and the judgement is subjective, the committee is settling a genuine disagreement — not replaying identical inputs. That's real GenLayer consensus, and it's impossible to reproduce on a deterministic chain: there is no single canonical price or headline the validators are agreeing to relay, only a live signal each of them reads independently and a shared judgement call about what it means.

The verdict is **consequential**, not advisory. It sets a collateral haircut (`0%` / `10%` / `25%` / `40%` for CALM / CAUTION / STRESS / CRISIS) that is wired through every credit decision:

| Verdict | Haircut | Effect |
|---|---|---|
| CALM | 0% | Full market value recognized |
| CAUTION | 10% | Borrowing power reduced |
| STRESS | 25% | Meaningfully reduced borrowing power |
| CRISIS | 40% | A position healthy at market price can become liquidatable |

`borrow()` and `liquidate()` both refuse to run until at least one verdict has been settled.

**Discipline boundary:** the web fetch and the LLM judgement happen *only* inside the closure passed to `gl.eq_principle.prompt_non_comparative`. The committee returns a label — nothing else crosses out of the non-deterministic block. The haircut table, and all downstream arithmetic (health factors, debt, liquidation proceeds), are ordinary deterministic Python. The model decides *policy*; it never produces a number the contract has to reconcile.

## Custody model: per-user vaults

A lending contract that receives every user's deposit at its own address can only ever observe "the balance grew by X" — it has no way to know *whose* transfer produced that growth, because a plain token `transfer()` carries no sender metadata to the receiving contract beyond the aggregate balance delta. Any accounting layered on top of a shared deposit address (balance-delta checks, commit/reveal, ticket queues) inherits this ambiguity: a caller who transferred nothing can still end up credited for someone else's transfer, simply by calling first.

This contract avoids the problem architecturally. On first use, `LendingProtocol` deploys the caller a **personal escrow contract** — `LendingVault` — at a deterministic `CREATE2` address (`predict_vault_of(user)` computes it before it even exists). From then on:

- users transfer collateral or debt-asset tokens to **their own vault**, never to the lending contract directly
- `supply()` / `fund()` / `repay()` read the **caller's own vault balance**. Tokens sitting in Alice's vault are Alice's by construction, so credit is bound to `msg.sender` by the custody model itself
- `withdraw()` and `liquidate()` instruct the position owner's vault to forward tokens onward (to the wallet, or to the pool)
- only the deploying `LendingProtocol` may drain a vault — `LendingVault.forward()` rejects every other caller

`borrow()` pays out directly from the protocol's own liquidity balance to the borrower's wallet; that leg needs no attribution, since the contract itself chooses the recipient.

### Vault debt-token reservation

`fund()` and `repay()` both read a vault's *live* debt-token balance to decide what they may credit, then instruct the vault to forward that amount out via `Vault.forward()` — an **asynchronous** message. The vault's balance does not actually drop until that forward lands, which can be well after the call that emitted it returns. Without protection, calling `fund()`/`repay()` again for the **same vault** before the first forward settles would see the identical, not-yet-decremented balance and get credited a second time for tokens that are already committed elsewhere.

`vault_debt_reserved[vault]` tracks exactly how much of a vault's balance is already spoken for by an outstanding forward. `fund()`/`repay()` only ever credit the balance **above** that reservation (`_reconcile_vault_debt()`), and the reservation is shared between the two functions since both draw on, and forward out of, the same vault balance. Once an earlier forward actually lands, the very next `fund()`/`repay()` call self-heals the reservation down to match reality before computing what's newly available; `reconcile_vault_debt(vault)` is also exposed as a standalone permissionless call, for the narrow case where a genuine new deposit lands in the same window and happens not to push the balance strictly above a still-stale reservation.

`supply()` needs no equivalent: it never moves tokens *out* of a vault (collateral just sits there until `withdraw()`/`liquidate()`), so there is no async gap for a repeated call to race.

## Liquidation

Liquidation is a **multi-step saga**, not a single transaction, because GenLayer cross-contract writes are asynchronous messages with no ordering guarantee between two messages emitted from the same call.

```
liquidate(user)
  └─ stage 1: user's vault forwards collateral to the pool

advance_liquidation(liq_id)      [permissionless, call repeatedly]
  ├─ stage 1 → 2: once the collateral has landed at the pool, emit the swap
  │              (snapshot the pool's reserve_b and the swap's min_out first)
  └─ stage 2 → settled: once the pool's reserve_b has drained by at least
                min_out, credit exactly min_out as proceeds and clear debt
```

Proceeds are measured at the **pool's own reserve delta**, not at this contract's balance, and capped at the swap's committed minimum output. That means:

- a stray or unrelated transfer landing on the lending contract during the swap window cannot be counted as proceeds (the pool's reserve doesn't move because of it)
- a concurrent, unrelated swap on the same pool during the same window cannot inflate what gets credited (credited proceeds are the committed `min_out`, never the raw reserve delta)

A stalled swap is retried automatically by calling `advance_liquidation()` again; the driver is idempotent per stage.

## Key functions

| function | type | notes |
|---|---|---|
| `assess_risk()` | write | **non-deterministic** — settles a live committee verdict and haircut |
| `get_risk_tier` / `get_risk_haircut_bps` / `get_risk_epoch` / `get_risk_signal` / `get_risk_note` | view | current ruling, its cost, how many rulings have settled, the committee's citation, and the on-chain evidence it judged |
| `live_collateral_value(amount)` / `recognized_collateral_value(amount)` | view | raw market value, and value after the committee's haircut |
| `max_borrow(user)` / `is_liquidatable(user)` | view | remaining borrowing power / liquidation eligibility |
| `get_collateral(user)` / `get_debt(user)` | view | per-user position |
| `register()` | write | deploys the caller's personal vault; idempotent; also auto-called by `supply`/`fund`/`repay` |
| `get_vault_of(user)` / `predict_vault_of(user)` | view | the user's vault address, registered or predicted |
| `supply(amount)` | write | credits collateral from the caller's vault |
| `fund(amount)` | write | adds lendable liquidity from the caller's vault, net of any still-unsettled reservation |
| `borrow(amount)` | write | **requires a ruling**; pays out to the caller's wallet |
| `repay(amount)` | write | reduces debt from the caller's vault, net of any still-unsettled reservation |
| `get_vault_debt_reserved(vault)` | view | debt-asset amount already committed to an outstanding, not-yet-landed `fund()`/`repay()` forward |
| `reconcile_vault_debt(vault)` | write | permissionless; trues up a vault's reservation against its current balance |
| `withdraw(amount)` | write | returns collateral from the caller's vault to their wallet |
| `liquidate(user)` | write | **requires a ruling**; permissionless; starts the liquidation saga |
| `advance_liquidation(liq_id)` | write | permissionless driver; call until it returns the repaid amount |
| `get_liq_pending` / `get_liq_stage` / `get_pending_liq_id` | view | liquidation saga state |
| `set_trusted_dex` / `set_trusted_pool` | write | owner-only |

## External dependencies

This contract does not deploy or own the tokens or the pricing pool — it trusts addresses supplied at construction:

- **`collateral_token`**, **`debt_token`** — any contract exposing `balance_of(Address) -> u256` and `transfer(Address, u256) -> None`
- **`pool`** — a trusted external AMM exposing:
  ```python
  class View:
      def quote_a_for_b(self, amount_in: u256) -> u256: ...
      def get_reserve_a(self) -> u256: ...
      def get_reserve_b(self) -> u256: ...
  class Write:
      def swap_a_for_b(self, amount_in: u256, min_out: u256, to: Address) -> u256: ...
  ```
- **`dex`** — stored for reference; not called by this contract

The pool is a **trust boundary**: its quotes and reserves are read directly with no independent verification. Point `trusted_pool` at a contract you control or otherwise trust.

## Risk parameters (constructor args)

```python
def __init__(
    self,
    dex: Address,
    pool: Address,
    collateral_token: Address,
    debt_token: Address,
    ltv_bps: u256,          # e.g. 7500 = 75% max loan-to-value, of RECOGNIZED value
    liquidation_bps: u256,  # e.g. 8000 = 80% liquidation threshold, of RECOGNIZED value
): ...
```

The 3% slippage guard on liquidation swaps is fixed in code (`min_out = quote * 97 / 100`), against the **raw** quote — the haircut governs solvency policy, not execution price.

## Deploy

Requires the [GenLayer CLI](https://github.com/genlayerlabs/genlayer-cli) (`npm i -g genlayer`).

```bash
genlayer network studionet     # or your target network

genlayer deploy --contract contracts/lending.py \
  --args addr#<dex_address> \
         addr#<pool_address> \
         addr#<collateral_token_address> \
         addr#<debt_token_address> \
         7500 8000
```

Address arguments must use the `addr#` form. After deploying, call `assess_risk()` at least once — `borrow()` and `liquidate()` are gated on a settled verdict.

Because `assess_risk()` is a genuine live vote, not a formality, it can land as `MAJORITY_DISAGREE` if validators reading the live Fear & Greed feed at slightly different moments (or reasoning about it differently) don't converge — no ruling is applied in that case (`risk_epoch` stays unchanged) and the call should simply be retried. This happened on the deployed instance above: the first `assess_risk()` call disagreed, the second settled `CALM`.

`LendingVault` (`contracts/vault.py`) is **not deployed separately** — `LendingProtocol` deploys one per user on demand via `gl.deploy_contract`, embedding the vault's source as `VAULT_CODE`. Keep `contracts/vault.py` and the `VAULT_CODE` literal in `lending.py` in sync if you modify the vault; GenLayer's `CREATE2` address derivation depends on `(deployer, salt, chain_id)` rather than the vault's bytecode hash, so drift between the two won't move addresses, but it will make the deployed vault's actual behavior diverge from what `contracts/vault.py` documents.

## Test

```bash
pip install -r requirements.txt
PYTHONUTF8=1 python -m pytest tests/direct/test_lending.py -v
```

Tests run in **direct mode** (`gltest.direct`) against a hand-built test double for the pool, the tokens, and vault deployment — no local GenVM node required. The first run downloads a pinned SDK/runner into `~/.cache/gltest-direct`.

Coverage:

- **Fear & Greed parsing / tier normalization** — the pure, deterministic pieces around the non-deterministic block
- **`assess_risk()` end-to-end** — a live-reading swap between two assessments changes the settled haircut, proving the external signal actually drives the result
- **Vault registration** — `register()` is idempotent and matches `predict_vault_of()`
- **Full lending lifecycle** — supply → borrow → repay → withdraw, correctly attributed through the vault model
- **Attacker-first custody attack** — an attacker who registers before a victim transfers still cannot claim the victim's deposit; stray balance on the lending contract's own address is never readable by `supply()`
- **Concurrent multi-user activity** — interleaved supply/fund/borrow/repay across several users stays correctly attributed
- **Liquidation proceeds isolation** — an unrelated transfer landing during a liquidation's swap window is not counted as proceeds, and a concurrent third-party swap on the same pool cannot inflate what's credited
- **In-flight payout isolation** — an unsettled borrow payout sitting on the lending contract's own balance cannot be mistaken for another user's deposit
- **Vault debt-token reservation** — repeated `fund()`/`repay()` calls on the same vault before an earlier forward has settled cannot double-count the same balance, whether racing against themselves or against each other

## Scope

What this contract deliberately does **not** do:

- No interest — debt repaid equals debt borrowed
- No yield, no LP shares — funders earn nothing, and funded liquidity cannot be withdrawn independently of the pool it was lent from
- No liquidator bonus — triggering a liquidation pays nothing extra
- Full-position liquidations only — no partial liquidations
- Surplus from a liquidation sale beyond the borrower's debt is retained as protocol liquidity, not refunded
- The committee's live signal is the crypto Fear & Greed index only — no other off-chain data — and it can only move the haircut tier, never a number the accounting depends on for correctness
