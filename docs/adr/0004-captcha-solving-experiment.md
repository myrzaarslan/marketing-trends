# Solving captchas as a contained, egress-isolated experiment — reversing back-off-only

**Context.** Settled discipline — [ADR-0001](./0001-diy-scraping-under-zero-budget.md),
[`docs/handoffs/robust-harvest.md`](../handoffs/robust-harvest.md), and all four adapter READMEs —
treats any captcha / bot-check as the **honest ceiling**: detect it, back off, return `blocked=True`,
never solve. The reasoning is coherent: a captcha signals a *flagged* IP/session, so **solving spends
trust and escalates a soft block toward a hard (permanent) ban**, whereas a soft-throttled IP recovers
by resting. The cheaper alternatives — spaced-run cadence (built) and rotating residential/mobile IPs
([OPEN-QUESTIONS Q-3](../OPEN-QUESTIONS.md)) — make the captcha *not appear* rather than fighting it.

**Decision.** Against that recommendation, we build a captcha **solver** as a **measured, contained
experiment**, to learn empirically whether solving is viable on our traffic. Shape:

### Mechanism — bootstrap with a strong agent, then distill to a cheap model
- **Phase 1 (≈week 1) — a strong agent (Opus 4.8) builds the playbook.** It encounters live
  challenges, *understands and classifies* each puzzle type, records every puzzle it sees (DOM +
  screenshot + the move-set it tried + the outcome), and writes human-readable bypass instructions.
  The output is an accumulating **puzzle playbook + dataset**, not just one-off solves.
- **Phase 2 — distill to a cheap model.** Once the puzzle types stabilize and the playbook is
  trusted, replace the expensive agent with a **cheap model** that executes the recorded strategies.
  (The expensive model is a one-time bootstrapping cost, not a recurring paid-scraping dependency, so
  the $0 mandate of ADR-0001 holds for steady state.)

### Agent tools to build
1. **`apply_captcha_moves(moves)`** — execute a set of browser moves (drags with a humanized
   velocity profile, clicks, holds) against the live challenge to attempt a pass.
2. **`record_puzzle(...)`** — persist the puzzle type, DOM/screenshot, attempted move-sets, and
   outcome → the context/dataset the future cheap model is distilled from.
3. **Puzzle tracking / registry** — a catalog of puzzle types + occurrence counts, so we can tell
   (a) when coverage is complete ("have we seen all the types?") and (b) when the agent is **stuck**
   on something and a human should look.

### Guardrails + blast-radius containment (the part that makes it safe)
- **Egress isolation is the core safety mechanism.** The experiment's egress is **never** the spine's
  ingestion egress. Production daily ingestion stays on the **clean home broadband**; the experiment
  runs on a **disposable** egress, so even a worst-case hard ban cannot poison the spine's data source.
- **Two-phase egress (decided 2026-06-26):** a **VPN** (datacenter, pre-flagged — effectively a cheap
  *captcha farm*) for **Phase-1 puzzle collection**, then a **phone 4G/5G tether** (residential-grade
  + disposable, airplane-mode toggle to rotate) for **Phase-2 solve-rate measurement that
  generalizes**. A paid residential/mobile proxy (Q-3) is the clean-but-paid version of the tether.
- **Attempt cap:** up to **10 solve attempts per obstacle — ONLY on the disposable egress.** On the
  home IP the rule is unchanged: **back-off-only, no solving.** A re-challenge or 429 after a solve =
  stop + **rotate the disposable IP** before resuming. Never grind on a hot IP.
- **Three disposable layers** keep every exposure replaceable: **identity** (burner account +
  `reset_persona()` for a fresh fingerprint, both $0), **egress** (VPN / mobile, above), and **data**
  (persistent dedup accumulators — a blocked run checkpoints partial progress and resumes, so harvested
  data is *never lost*; you recover *access* by swapping a disposable layer, never by resting a burnt one).
- Lives in the harness (e.g. `core/harness/captcha_solver.py`), **default OFF**; harvesters keep
  backing off unless explicitly opted in.

**Why this is non-obvious / the trade-off.** It directly contradicts ADR-0001 and `robust-harvest.md`.
We accept fragility (behavioral detection re-challenges solved sliders) and elevated ToS / anti-
circumvention exposure. The hard-ban risk is **contained to disposable egresses**, deliberately kept
off the shared home IP that the parallel spine track depends on. We take it only to **measure** real
solve-success and ban-escalation rates with our own data, behind a circuit breaker that stops it from
silently becoming a grinder.

**Consequences.**
- New harness module + agent tools + a puzzle registry; default-off flag preserves existing back-off.
- **Success is judged by measurement, not by the feature working:** solve-success rate, re-challenge
  rate, and whether enabling it correlates with **hard bans** on the disposable egress.
- **This ADR is explicitly reversible.** If solving escalates bans even on disposable egresses, revert
  to back-off-only and invest in the Q-3 IP-rotation path instead.
- The honest back-off contract in the adapter READMEs and `robust-harvest.md` stays the **default**;
  solving is an opt-in experimental override, not the new norm.
- Phase 1 partially *exercises* Q-3 (the disposable-egress story); revisit Q-3 with the measured data.
