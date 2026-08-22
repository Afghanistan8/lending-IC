# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
LendingProtocol - collateralized lending whose credit limits and liquidation
threshold are settled by a live GenLayer validator committee vote, not a
static formula.

WHY THIS IS A GENLAYER INTELLIGENT CONTRACT
  assess_risk() runs a NON-DETERMINISTIC block. Each validator independently
  FETCHES A LIVE OFF-CHAIN SIGNAL - the crypto Fear & Greed index, over the
  web from api.alternative.me - and judges it together with the same
  on-chain market facts, then the committee reaches consensus on ONE
  verdict: CALM / CAUTION / STRESS / CRISIS.

  Because the external reading is live (it moves over time and between
  fetches) and the judgement is subjective, the validators are settling a
  genuine disagreement, not replaying identical inputs - this is meaningful
  GenLayer consensus, not something a deterministic chain could reproduce.

  That verdict is CONSEQUENTIAL: it sets a collateral haircut that is wired
  through every credit decision in the contract:
      - how much you may borrow          (max_borrow, borrow)
      - whether you may withdraw         (withdraw)
      - whether you may be liquidated    (is_liquidatable, liquidate)
  A CRISIS verdict cuts recognized collateral value by 40%, which shrinks
  borrowing power and can make an otherwise-healthy position liquidatable.
  No credit decision can be made without a committee-settled verdict, and
  liquidate() refuses to run until one exists.

DISCIPLINE - where non-determinism is allowed to touch:
  The web fetch and LLM judgement happen ONLY inside the closure passed to
  gl.eq_principle.prompt_non_comparative. The committee returns a LABEL
  only. The haircut for each label is a fixed table in deterministic
  Python, and all arithmetic (health factors, debt, proceeds, settlement)
  is ordinary deterministic code. Every storage write, cross-contract call
  and emit() runs AFTER the non-deterministic block returns. The LLM
  decides POLICY; it never produces a number that has to reconcile.

CUSTODY MODEL - per-user vaults, not a shared deposit address
  A lending contract that receives everyone's deposits at its OWN address
  can only observe "the balance went up by X" - it has no way to bind that
  increase to a specific sender, because plain ERC20-style transfer() gives
  the receiving contract no sender metadata beyond the aggregate delta.
  Any accounting built on top of a shared address (balance-delta checks,
  commit/reveal schemes, ticket queues) inherits this ambiguity: a caller
  who did nothing can still end up credited for a transfer someone else
  made, as long as they call before the intended recipient does.

  This contract avoids the problem architecturally instead of patching
  around it. On first use, LendingProtocol deploys the caller a PERSONAL
  vault contract at a deterministic CREATE2 address (see LendingVault
  below, embedded as VAULT_CODE). From then on:
    - the user transfers tGEN (or tUSDC, for repay/fund) to THEIR OWN
      vault - never to the lending contract's address
    - supply()/fund()/repay() read the CALLER'S vault balance directly.
      Tokens sitting in Alice's vault are Alice's by construction, so
      credit is bound to msg.sender by the custody model itself, not by a
      heuristic layered on top of a shared balance
    - withdraw() and liquidate() instruct the OWNER'S vault to forward
      tokens onward (to the user's wallet, or to the pool)
    - only the lending contract may drain a vault; LendingVault.forward()
      rejects every other caller

  Borrowing pays out directly from lending's own liquidity balance to the
  borrower's wallet - the outbound leg needs no attribution, since the
  contract itself chooses the recipient.

EVERYTHING ELSE
  - collateral is tGEN, debt is tUSDC; collateral is priced by a live
    cross-contract view() quote from a trusted external AMM pool
  - liquidation proceeds are measured at the POOL'S OWN reserve delta, not
    at this contract's balance, and capped at the swap's committed minimum
    output - so nothing unrelated moving this contract's balance during
    the swap window, and no concurrent third-party swap on the same pool,
    can inflate what gets credited as proceeds
  - zero interest, no LP shares, no yield, no withdrawal of funded
    liquidity
  - liquidation is full-position only, no liquidator bonus, guarded by a
    3% slippage tolerance; surplus above the debt is retained as protocol
    liquidity; settlement is permissionless and idempotent per stage
"""

from genlayer import *

import json


# ================= LIVE EXTERNAL SENTIMENT (non-deterministic input) =========
# Kept OUTSIDE the contract class on purpose: the eq_principle closure in
# assess_risk() captures only a plain string plus these module-level
# functions, so nothing about contract storage rides into the
# non-deterministic block.
def _parse_fear_greed(text: str) -> str:
    """Pure parser for the alternative.me Fear & Greed payload.

    Reads ONLY two fixed fields, range-checks the 0-100 score, and
    allowlists the classification label - so no free-form text from the
    feed ever reaches the LLM prompt (minimal prompt-injection surface).
    Returns 'unavailable' on any malformed input. Pure (no web, no gl) so
    it is directly unit-testable.
    """
    try:
        item = json.loads(text)["data"][0]
        value = int(str(item["value"]).strip())
        if value < 0 or value > 100:
            return "unavailable"
        label = str(item["value_classification"]).strip()
        known = ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")
        safe = label if label in known else "Unknown"
        return "Fear & Greed index " + str(value) + "/100 (" + safe + ")"
    except Exception:
        return "unavailable"


def _fetch_fear_greed() -> str:
    """LIVE, NON-DETERMINISTIC external evidence: the crypto Fear & Greed
    index.

    Only ever invoked from inside the assess_risk() eq_principle closure,
    where EVERY validator fetches it independently - the value moves over
    time and between fetches, which is exactly what makes the committee's
    agreement a real consensus rather than a replay of identical inputs. A
    feed outage degrades to 'unavailable' (the committee then judges on
    on-chain facts alone) rather than bricking assess_risk().
    """
    try:
        resp = gl.nondet.web.get("https://api.alternative.me/fng/?limit=1")
        body = resp.body
        if not body:
            return "unavailable"
        return _parse_fear_greed(bytes(body).decode("utf-8", errors="replace"))
    except Exception:
        return "unavailable"


@gl.contract_interface
class Pool:
    """External AMM pool that prices the collateral asset. Trusted: its
    quotes and reserves are read directly with no independent verification,
    so it must be a contract this deployment genuinely trusts."""
    class View:
        def quote_a_for_b(self, amount_in: u256) -> u256: ...
        def get_reserve_a(self) -> u256: ...
        def get_reserve_b(self) -> u256: ...
    class Write:
        def swap_a_for_b(self, amount_in: u256, min_out: u256, to: Address) -> u256: ...


@gl.contract_interface
class Vault:
    class View:
        def get_owner(self) -> Address: ...
        def get_lending(self) -> Address: ...
    class Write:
        def forward(self, token: Address, to: Address, amount: u256) -> None: ...


# LendingVault's source, embedded so LendingProtocol can deploy a fresh
# per-user vault via CREATE2 on demand. Kept byte-for-byte identical to
# contracts/vault.py - GenLayer's CREATE2 address derivation depends on
# (deployer, salt, chain_id) rather than the initcode hash, so drift here
# would not move the vault address, but keeping the two in sync makes it
# obvious what a user's vault actually runs.
VAULT_CODE = b'''# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""LendingVault - a minimal per-user token escrow. Holds one user's tokens
and forwards them only on the deploying LendingProtocol's instruction. See
contracts/vault.py for the full rationale."""

from genlayer import *


class LendingVault(gl.Contract):
    owner: Address
    lending: Address

    def __init__(self, owner: Address, lending: Address):
        self.owner = owner
        self.lending = lending

    @gl.public.view
    def get_owner(self) -> Address:
        return self.owner

    @gl.public.view
    def get_lending(self) -> Address:
        return self.lending

    @gl.public.write
    def forward(self, token: Address, to: Address, amount: u256) -> None:
        assert gl.message.sender_address == self.lending, "only lending"
        gl.get_contract_at(token).emit().transfer(to, amount)
'''


ZERO_ADDRESS = Address(b"\x00" * 20)


class LendingProtocol(gl.Contract):
    owner: Address
    trusted_dex: Address
    trusted_pool: Address
    collateral_token: Address
    debt_token: Address
    ltv_bps: u256
    liquidation_bps: u256
    collateral_of: TreeMap[Address, u256]
    debt_of: TreeMap[Address, u256]
    tracked_collateral: u256
    tracked_liquidity: u256
    # vaults[user] = the CREATE2 address of that user's personal token
    # escrow. See the module docstring's CUSTODY MODEL section.
    vaults: TreeMap[Address, Address]
    # ---- liquidation saga ----
    next_liq_id: u256
    liq_pending: bool
    liq_user: TreeMap[u256, Address]
    liq_collateral: TreeMap[u256, u256]
    liq_stage: u256                 # 1 transfer emitted, 2 swap emitted, 3 settled
    settled: TreeMap[u256, bool]
    # The pool's own debt-asset reserve right before each liquidation's
    # swap, plus the exact minimum output the swap was emitted with -
    # together they isolate a liquidation's proceeds from anything
    # unrelated happening on the pool or on this contract's own balance.
    # See stage 2 of _advance().
    liq_reserve_b_before: TreeMap[u256, u256]
    liq_expected_out: TreeMap[u256, u256]
    # ---- validator-settled risk state ----
    risk_tier: str            # "" until first assessment
    risk_haircut_bps: u256    # applied to collateral value
    risk_note: str            # deterministic on-chain evidence put to the committee
    risk_signal: str          # committee's one-line ruling; cites the live feed it read
    risk_epoch: u256          # increments on every settled assessment
    risk_ref_price: u256      # quote observed at the last assessment

    # Reference size used to sample the pool price (raw units).
    PRICE_PROBE = u256(100)

    def __init__(
        self,
        dex: Address,
        pool: Address,
        collateral_token: Address,
        debt_token: Address,
        ltv_bps: u256,
        liquidation_bps: u256,
    ):
        self.owner = gl.message.sender_address
        self.trusted_dex = dex
        self.trusted_pool = pool
        self.collateral_token = collateral_token
        self.debt_token = debt_token
        self.ltv_bps = ltv_bps
        self.liquidation_bps = liquidation_bps
        self.tracked_collateral = 0
        self.tracked_liquidity = 0
        self.next_liq_id = 0
        self.liq_pending = False
        self.liq_stage = 0
        self.risk_tier = ""
        self.risk_haircut_bps = 0
        self.risk_note = ""
        self.risk_signal = ""
        self.risk_epoch = 0
        self.risk_ref_price = 0

    # ---------------- owner wiring ----------------
    @gl.public.write
    def set_trusted_dex(self, dex: Address) -> None:
        assert gl.message.sender_address == self.owner, "only owner"
        self.trusted_dex = dex

    @gl.public.write
    def set_trusted_pool(self, pool: Address) -> None:
        assert gl.message.sender_address == self.owner, "only owner"
        self.trusted_pool = pool

    # ================= NON-DETERMINISTIC RISK ASSESSMENT =================
    # Permissionless. Anyone may ask the validator committee to re-judge
    # market conditions. On-chain facts are gathered deterministically
    # BEFORE the non-deterministic block (cross-contract calls are
    # forbidden inside it). Inside the block, every validator independently
    # fetches a LIVE off-chain signal - the crypto Fear & Greed index - and
    # judges it alongside those facts. What they must agree on is the
    # JUDGEMENT, which is subjective and rests on live external data, so
    # the consensus is genuine.
    @gl.public.write
    def assess_risk(self) -> str:
        facts = self._market_facts()          # cross-contract views: BEFORE the nondet block

        def _judge() -> str:
            # The ONLY non-deterministic step: a live web fetch, run
            # independently by the leader and by every validator.
            sentiment = _fetch_fear_greed()
            return "LIVE MARKET SENTIMENT: " + sentiment + "\nON-CHAIN EVIDENCE: " + facts

        verdict = gl.eq_principle.prompt_non_comparative(
            _judge,
            task=(
                "You are the risk committee of a lending protocol. You are given "
                "a LIVE crypto Fear & Greed sentiment reading (0 = extreme fear, "
                "100 = extreme greed) and on-chain market facts for the "
                "collateral asset. Weigh BOTH and classify current conditions. "
                "Answer on a SINGLE line as: <TIER> - <one short sentence citing "
                "the Fear & Greed number and the on-chain evidence>. TIER must be "
                "exactly one of: CALM, CAUTION, STRESS, CRISIS."
            ),
            criteria=(
                "Fear & Greed at or below ~25 (fear / extreme fear) signals a "
                "fragile market and should push toward STRESS or CRISIS, "
                "especially when collateral is large relative to pool depth or "
                "price is falling. Around 45-55 is neutral. High greed (>75) is "
                "NOT automatically calm - it can mask fragility, so do not relax "
                "when on-chain depth is thin. CALM: stable price, deep liquidity, "
                "non-extreme sentiment. CAUTION: mild price decline, moderate "
                "borrowing, or sentiment drifting fearful. STRESS: clear price "
                "decline, collateral large vs pool depth, or fearful sentiment. "
                "CRISIS: severe price decline, collateral dwarfing liquidity, or "
                "extreme-fear sentiment. If the sentiment reading is "
                "'unavailable', judge on the on-chain facts alone. When the "
                "evidence is ambiguous choose the safer, more cautious tier."
            ),
        )

        tier = self._normalize_tier(verdict)

        # Deterministic mapping. The committee chooses the LABEL; the
        # protocol chooses what the label costs.
        if tier == "CALM":
            haircut = u256(0)
        elif tier == "CAUTION":
            haircut = u256(1000)      # 10%
        elif tier == "STRESS":
            haircut = u256(2500)      # 25%
        else:
            haircut = u256(4000)      # 40%

        self.risk_tier = tier
        self.risk_haircut_bps = haircut
        self.risk_note = facts        # deterministic on-chain evidence
        self.risk_signal = verdict    # committee's ruling, cites the live sentiment it read
        self.risk_epoch = self.risk_epoch + u256(1)
        self.risk_ref_price = Pool(self.trusted_pool).view().quote_a_for_b(self.PRICE_PROBE)
        return tier

    def _market_facts(self) -> str:
        """Deterministic on-chain evidence, rendered for the committee."""
        pool = Pool(self.trusted_pool)
        price = pool.view().quote_a_for_b(self.PRICE_PROBE)
        reserve_collateral = pool.view().get_reserve_a()
        reserve_debt = pool.view().get_reserve_b()
        prev = self.risk_ref_price
        if prev == u256(0):
            move = "no previous reading"
        elif price < prev:
            move = "DOWN from " + str(prev) + " to " + str(price)
        elif price > prev:
            move = "UP from " + str(prev) + " to " + str(price)
        else:
            move = "unchanged at " + str(price)
        return (
            "Collateral price (per " + str(self.PRICE_PROBE) + " units): " + str(price)
            + ". Movement since last assessment: " + move
            + ". Pool depth: " + str(reserve_collateral) + " collateral / "
            + str(reserve_debt) + " debt-asset."
            + " Protocol holds " + str(self.tracked_collateral) + " collateral"
            + " and has " + str(self.tracked_liquidity) + " debt-asset lendable."
        )

    def _normalize_tier(self, verdict: str) -> str:
        v = verdict.strip().upper()
        # The committee is asked to LEAD with the tier word, so trust the
        # first token: it avoids mis-reading a rationale that merely
        # mentions another tier (e.g. "CALM - no CRISIS-level thinness").
        head = v.replace("—", " ").replace("-", " ").split()
        if head:
            for t in ("CRISIS", "STRESS", "CAUTION", "CALM"):
                if head[0] == t:
                    return t
        # Fallback: severity-first scan so a longer answer that mentions
        # several tiers still resolves to the most cautious one.
        if "CRISIS" in v:
            return "CRISIS"
        if "STRESS" in v:
            return "STRESS"
        if "CAUTION" in v:
            return "CAUTION"
        if "CALM" in v:
            return "CALM"
        # Unreadable answer must never silently become "safe".
        raise gl.vm.UserError("risk committee returned an unusable verdict")

    # ---------------- views ----------------
    @gl.public.view
    def get_risk_tier(self) -> str:
        return self.risk_tier

    @gl.public.view
    def get_risk_haircut_bps(self) -> u256:
        return self.risk_haircut_bps

    @gl.public.view
    def get_risk_note(self) -> str:
        return self.risk_note

    @gl.public.view
    def get_risk_signal(self) -> str:
        return self.risk_signal

    @gl.public.view
    def get_risk_epoch(self) -> u256:
        return self.risk_epoch

    @gl.public.view
    def get_collateral(self, user: Address) -> u256:
        return self.collateral_of.get(user, u256(0))

    @gl.public.view
    def get_debt(self, user: Address) -> u256:
        return self.debt_of.get(user, u256(0))

    @gl.public.view
    def get_tracked_liquidity(self) -> u256:
        return self.tracked_liquidity

    @gl.public.view
    def get_tracked_collateral(self) -> u256:
        return self.tracked_collateral

    @gl.public.view
    def get_vault_of(self, user: Address) -> Address:
        """The user's registered vault address, or the zero address if the
        user has not called register() (or supply/fund/repay) yet."""
        return self.vaults.get(user, ZERO_ADDRESS)

    @gl.public.view
    def predict_vault_of(self, user: Address) -> Address:
        """The CREATE2 address the user's vault WOULD have once deployed.
        Safe to compute before register() - a client can show the deposit
        address before the user ever registers. register() is idempotent,
        so once tokens are sent there, supply()/fund()/repay() (which
        auto-register) complete the deposit."""
        from genlayer.py._internal import create2_address
        return create2_address(self.address, self._vault_salt(user), gl.message.chain_id)

    @gl.public.view
    def get_liq_pending(self) -> bool:
        return self.liq_pending

    @gl.public.view
    def live_collateral_value(self, amount: u256) -> u256:
        """RAW market value, before the committee's haircut."""
        return Pool(self.trusted_pool).view().quote_a_for_b(amount)

    @gl.public.view
    def recognized_collateral_value(self, amount: u256) -> u256:
        """Value the protocol will actually lend against: market value
        reduced by the haircut the validator committee settled on."""
        raw = Pool(self.trusted_pool).view().quote_a_for_b(amount)
        return (raw * (u256(10000) - self.risk_haircut_bps)) // u256(10000)

    def _recognized(self, amount: u256) -> u256:
        raw = Pool(self.trusted_pool).view().quote_a_for_b(amount)
        return (raw * (u256(10000) - self.risk_haircut_bps)) // u256(10000)

    @gl.public.view
    def max_borrow(self, user: Address) -> u256:
        collat = self.collateral_of.get(user, u256(0))
        if collat == u256(0):
            return u256(0)
        value = self._recognized(collat)
        cap = (value * self.ltv_bps) // u256(10000)
        debt = self.debt_of.get(user, u256(0))
        if debt >= cap:
            return u256(0)
        return cap - debt

    @gl.public.view
    def is_liquidatable(self, user: Address) -> bool:
        debt = self.debt_of.get(user, u256(0))
        if debt == u256(0):
            return False
        value = self._recognized(self.collateral_of.get(user, u256(0)))
        return debt * u256(10000) > value * self.liquidation_bps

    # ---------------- vault registration (CREATE2) ----------------
    # Each user gets exactly one personal vault at a deterministic address.
    # Idempotent: calling register() twice returns the same vault.
    # supply()/fund()/repay() auto-register, so most users never call this
    # directly - a client can call it (or predict_vault_of) to fetch the
    # deposit address before asking the user to transfer.
    def _vault_salt(self, user: Address) -> u256:
        # The 20-byte user address, read as a 256-bit salt: deterministic
        # per user, unique across users, fits comfortably in u256.
        return u256(int.from_bytes(user.as_bytes, "big"))

    def _ensure_vault(self, user: Address) -> Address:
        existing = self.vaults.get(user, ZERO_ADDRESS)
        if existing.as_bytes != ZERO_ADDRESS.as_bytes:
            return existing
        salt = self._vault_salt(user)
        addr = gl.deploy_contract(
            code=VAULT_CODE,
            args=[user, self.address],
            salt_nonce=salt,
        )
        self.vaults[user] = addr
        return addr

    @gl.public.write
    def register(self) -> Address:
        """Deploy the caller's personal vault (via CREATE2) if it doesn't
        exist yet, and return its address. Idempotent."""
        return self._ensure_vault(gl.message.sender_address)

    # ---------------- supply collateral (vault-attributed) ----------------
    # No shared-address balance-delta race is possible here: this reads the
    # CALLER'S OWN vault. Tokens sitting there were transferred to that
    # deterministic per-user address, so they are - by construction - the
    # caller's, regardless of who calls supply(). Alice's vault never holds
    # Bob's tokens, so Alice's supply() call can never be credited from
    # Bob's transfer.
    @gl.public.write
    def supply(self, amount: u256) -> u256:
        assert amount > u256(0), "amount must be positive"
        user = gl.message.sender_address
        vault = self._ensure_vault(user)
        vault_balance = gl.get_contract_at(self.collateral_token).view().balance_of(vault)
        already_credited = self.collateral_of.get(user, u256(0))
        assert vault_balance >= already_credited + amount, (
            "not enough tGEN in your vault: transfer to your vault address first "
            "(see predict_vault_of / get_vault_of)"
        )
        self.collateral_of[user] = already_credited + amount
        self.tracked_collateral = self.tracked_collateral + amount
        return self.collateral_of[user]

    # ---------------- liquidity provisioning (vault-attributed) ----------------
    @gl.public.write
    def fund(self, amount: u256) -> u256:
        assert amount > u256(0), "amount must be positive"
        user = gl.message.sender_address
        vault = self._ensure_vault(user)
        vault_balance = gl.get_contract_at(self.debt_token).view().balance_of(vault)
        assert vault_balance >= amount, (
            "not enough tUSDC in your vault: transfer to your vault address first"
        )
        self.tracked_liquidity = self.tracked_liquidity + amount
        # Move the tokens from the funder's vault into lending's own
        # balance so they are available to lend out. Only the vault owner
        # (via lending as the sole authorized caller) can drain the vault,
        # so this amount is guaranteed to arrive with no race against
        # anyone else's transfer.
        Vault(vault).emit().forward(self.debt_token, self.address, amount)
        return self.tracked_liquidity

    # ---------------- borrow (async payout, risk-gated) ----------------
    # Payout goes DIRECTLY to the borrower's wallet from lending's own
    # liquidity balance - the borrower's vault is not involved. Nothing
    # needs attribution on this leg, since the contract itself chooses the
    # recipient.
    @gl.public.write
    def borrow(self, amount: u256) -> u256:
        assert amount > u256(0), "amount must be positive"
        assert self.risk_epoch > u256(0), "no risk assessment yet: call assess_risk() first"
        user = gl.message.sender_address
        collat = self.collateral_of.get(user, u256(0))
        assert collat > u256(0), "no collateral supplied"
        assert amount <= self.tracked_liquidity, "insufficient protocol liquidity"
        value = self._recognized(collat)          # committee-adjusted
        new_debt = self.debt_of.get(user, u256(0)) + amount
        assert new_debt * u256(10000) <= value * self.ltv_bps, "exceeds LTV limit at the current risk tier"
        self.debt_of[user] = new_debt
        self.tracked_liquidity = self.tracked_liquidity - amount
        gl.get_contract_at(self.debt_token).emit().transfer(user, amount)
        return new_debt

    # ---------------- repay (vault-attributed) ----------------
    # Reads the caller's vault balance to determine what they can repay,
    # then instructs the vault to forward the tokens into lending's own
    # balance where they replenish protocol liquidity.
    @gl.public.write
    def repay(self, amount: u256) -> u256:
        assert amount > u256(0), "amount must be positive"
        user = gl.message.sender_address
        debt = self.debt_of.get(user, u256(0))
        assert debt > u256(0), "no outstanding debt"
        assert amount <= debt, "amount exceeds outstanding debt"
        vault = self._ensure_vault(user)
        vault_balance = gl.get_contract_at(self.debt_token).view().balance_of(vault)
        assert vault_balance >= amount, (
            "not enough tUSDC in your vault: transfer to your vault address first"
        )
        self.debt_of[user] = debt - amount
        self.tracked_liquidity = self.tracked_liquidity + amount
        Vault(vault).emit().forward(self.debt_token, self.address, amount)
        return self.debt_of[user]

    # ---------------- withdraw collateral (async payout, risk-gated) --------
    # Instructs the CALLER'S OWN vault to send collateral back to their
    # wallet. A user can never withdraw someone else's collateral because
    # their vault doesn't hold anyone else's tokens.
    @gl.public.write
    def withdraw(self, amount: u256) -> u256:
        assert amount > u256(0), "amount must be positive"
        user = gl.message.sender_address
        collat = self.collateral_of.get(user, u256(0))
        assert amount <= collat, "amount exceeds supplied collateral"
        vault = self.vaults.get(user, ZERO_ADDRESS)
        assert vault.as_bytes != ZERO_ADDRESS.as_bytes, "no vault registered"
        remaining = collat - amount
        debt = self.debt_of.get(user, u256(0))
        if debt > u256(0):
            assert self.risk_epoch > u256(0), "no risk assessment yet: call assess_risk() first"
            value = self._recognized(remaining)   # committee-adjusted
            assert debt * u256(10000) <= value * self.ltv_bps, "withdrawal would breach LTV at the current risk tier"
        self.collateral_of[user] = remaining
        self.tracked_collateral = self.tracked_collateral - amount
        Vault(vault).emit().forward(self.collateral_token, user, amount)
        return remaining

    # ================= liquidation saga (risk-gated) =================
    # WHY THIS IS A STEP-AT-A-TIME SAGA:
    #   GenLayer cross-contract writes are ASYNCHRONOUS messages. Two
    #   messages emitted from the same transaction are NOT guaranteed to
    #   execute in the order they were emitted, so emitting "send collateral
    #   to the pool" and "swap it" together risks the swap running before
    #   the collateral has actually landed, failing the pool's own receipt
    #   check and leaving collateral stuck with the debt still open.
    #
    #   This design emits exactly ONE message per transaction and VERIFIES
    #   its effect on-chain before emitting the next. advance_liquidation()
    #   is permissionless, idempotent per stage, and RETRIES a swap that
    #   did not take effect - so a stalled leg is recoverable, not
    #   terminal.
    #
    # Stages:  1 = collateral transfer emitted   2 = swap emitted   3 = settled

    @gl.public.write
    def liquidate(self, user: Address) -> u256:
        assert not self.liq_pending, "another liquidation is pending settlement"
        # A liquidation may only proceed on a committee-settled view of the
        # market. Without a verdict there is no recognized value, so there
        # is no basis to seize anyone's collateral.
        assert self.risk_epoch > u256(0), "no risk assessment yet: call assess_risk() first"
        debt = self.debt_of.get(user, u256(0))
        assert debt > u256(0), "no outstanding debt"
        collat = self.collateral_of.get(user, u256(0))
        assert collat > u256(0), "no collateral"
        vault = self.vaults.get(user, ZERO_ADDRESS)
        assert vault.as_bytes != ZERO_ADDRESS.as_bytes, "no vault registered"

        raw_quote = Pool(self.trusted_pool).view().quote_a_for_b(collat)
        recognized = (raw_quote * (u256(10000) - self.risk_haircut_bps)) // u256(10000)
        assert debt * u256(10000) > recognized * self.liquidation_bps, "position is healthy at the current risk tier"

        liq_id = self.next_liq_id
        self.next_liq_id = liq_id + u256(1)
        self.liq_user[liq_id] = user
        self.liq_collateral[liq_id] = collat
        self.liq_stage = u256(1)
        self.liq_pending = True

        self.collateral_of[user] = u256(0)
        self.tracked_collateral = self.tracked_collateral - collat

        # Instruct the user's own vault to send collateral straight to the
        # pool. The vault holds this collateral under per-user custody, so
        # there is no cross-user contamination on the outbound leg either.
        Vault(vault).emit().forward(self.collateral_token, self.trusted_pool, collat)
        return liq_id

    def _pool_unbooked_collateral(self) -> u256:
        """Collateral sitting at the pool that the pool has not yet booked
        into its reserves - i.e. our transfer landed but no swap consumed
        it."""
        held = gl.get_contract_at(self.collateral_token).view().balance_of(self.trusted_pool)
        booked = Pool(self.trusted_pool).view().get_reserve_a()
        if held <= booked:
            return u256(0)
        return held - booked

    def _advance(self, liq_id: u256) -> u256:
        assert self.liq_pending, "no liquidation pending"
        assert not self.settled.get(liq_id, False), "already settled"
        assert liq_id + u256(1) == self.next_liq_id, "not the pending liquidation id"
        collat = self.liq_collateral.get(liq_id, u256(0))

        # ---- stage 1: the collateral transfer must have landed at the pool ----
        if self.liq_stage == u256(1):
            unbooked = self._pool_unbooked_collateral()
            assert unbooked >= collat, "collateral has not reached the pool yet"
            # Snapshot the pool's OWN debt-asset reserve right before the
            # swap. Combined with the exact minimum output committed to
            # below, this gives stage 2 an isolated, source-specific
            # measurement of what the swap actually returned (see the
            # comment there).
            self.liq_reserve_b_before[liq_id] = Pool(self.trusted_pool).view().get_reserve_b()
            quote = Pool(self.trusted_pool).view().quote_a_for_b(collat)
            min_out = (quote * u256(97)) // u256(100)
            self.liq_expected_out[liq_id] = min_out
            Pool(self.trusted_pool).emit().swap_a_for_b(collat, min_out, self.address)
            self.liq_stage = u256(2)
            return u256(0)

        # ---- stage 2: measure the EXACT swap return, source-isolated ----
        # Two independent signals bound the credited proceeds to this
        # specific swap and nothing else:
        #
        # (a) the pool's own debt-asset reserve has to have dropped by at
        #     least our committed min_out - the pool paying us for this
        #     swap is what causes reserve_b to fall. A stray tUSDC
        #     transfer to this contract, or to any other party, moves a
        #     different balance and does NOT show up in reserve_b at all,
        #     so it can never be counted as our proceeds.
        # (b) the credited amount is min_out itself, not the raw reserve_b
        #     delta. If a concurrent swap by someone else on the same pool
        #     runs between our stage 1 and stage 2, reserve_b drops by
        #     MORE than our min_out - we still credit only min_out, so
        #     that other swap's output can't be double-counted as ours.
        #
        # A concurrent swap in the OTHER direction would reduce our
        # observable reserve_b delta, in which case the check below fails
        # and this retries: either our swap ran and satisfied min_out, or
        # it did not.
        expected = self.liq_expected_out.get(liq_id, u256(0))
        reserve_b_before = self.liq_reserve_b_before.get(liq_id, u256(0))
        reserve_b_now = Pool(self.trusted_pool).view().get_reserve_b()
        pool_paid_out = reserve_b_before - reserve_b_now if reserve_b_now < reserve_b_before else u256(0)

        if pool_paid_out < expected:
            # Not enough drain to prove our swap ran. If the collateral is
            # still sitting unbooked at the pool, the swap message did not
            # take effect; re-emit it.
            unbooked = self._pool_unbooked_collateral()
            assert unbooked >= collat, "swap has not settled yet - try again shortly"
            self._emit_swap(collat, liq_id)
            return u256(0)

        # The pool has demonstrably paid out AT LEAST `expected`. Credit
        # exactly that as proceeds. Any surplus above min_out simply stays
        # in lending's liquidity balance as protocol surplus - it is
        # neither attributed to this liquidation nor lost.
        proceeds = expected

        user = self.liq_user.get(liq_id, ZERO_ADDRESS)
        debt = self.debt_of.get(user, u256(0))
        repaid = proceeds if proceeds < debt else debt
        self.debt_of[user] = debt - repaid
        self.tracked_liquidity = self.tracked_liquidity + proceeds
        self.settled[liq_id] = True
        self.liq_stage = u256(3)
        self.liq_pending = False
        return repaid

    def _emit_swap(self, collat: u256, liq_id: u256) -> None:
        # min_out is recomputed from a FRESH quote each time - storing it
        # at trigger time would let a stale figure make the swap
        # permanently unfillable after the price moves. liq_expected_out
        # is overwritten so stage 2's proceeds measurement stays consistent
        # with whatever was actually last emitted.
        quote = Pool(self.trusted_pool).view().quote_a_for_b(collat)
        min_out = (quote * u256(97)) // u256(100)
        self.liq_expected_out[liq_id] = min_out
        Pool(self.trusted_pool).emit().swap_a_for_b(collat, min_out, self.address)

    @gl.public.write
    def advance_liquidation(self, liq_id: u256) -> u256:
        """Permissionless driver. Call repeatedly until it returns the
        repaid amount: it emits the swap once the collateral has arrived,
        retries a swap that did not take effect, and books proceeds once
        the pool has demonstrably paid out."""
        return self._advance(liq_id)

    @gl.public.view
    def get_liq_stage(self) -> u256:
        return self.liq_stage

    @gl.public.view
    def get_pending_liq_id(self) -> u256:
        """The id of the liquidation currently awaiting settlement, or 0
        when none is pending. Callers should pass this to
        advance_liquidation() rather than guessing an id, since _advance
        asserts liq_id + 1 == next_liq_id."""
        if not self.liq_pending:
            return u256(0)
        return self.next_liq_id - u256(1)
