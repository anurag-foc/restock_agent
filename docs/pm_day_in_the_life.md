# The PM's day, with and without this — the deliverable, defined before the pipeline

This is the design starting point for the phase-1 redesign: what does the person on the
other end of this system actually get, independent of signal board / Genie / Supervisor
implementation. Everything below is grounded in `docs/market_evidence_phase1.md` — no new
personas or numbers invented. Read that doc's §1–6 first if the citations below are unfamiliar.

Working title for the role: **PM** (materials/procurement planner responsible for restock
decisions across a set of parts and warehouses — matches the term already used in CLAUDE.md
and the Review App). Company profile: discrete manufacturer, $250M+ revenue, multi-warehouse,
BOM-driven production (per the LeanDNA/Wakefield survey population this whole thesis rests on).

---

## Without this system — a Tuesday today

- 72% of manufacturers like this one find out about a material shortage only after a
  production delay is already unavoidable — the data held the signal, nobody surfaced it in
  time (§1).
- The PM is simultaneously short on some parts and long on others: 84% had shortages *and*
  80%+ were hit by excess inventory in the same 12 months (§1). Two different problems,
  same desk, no single view connects them.
- When something does trip a threshold, ERP gives visibility but not a next step: 51% of
  these PMs need a week or more to land on a corrective action (§1). In that week, exposure
  keeps accruing.
- Getting to that week-long decision costs real time: ~14 hours/week manually tracking data
  across systems, roughly a third of total time on cleansing/exception handling (§3).
  92% end up deciding on gut feel because the guidance they have isn't good enough (§3).
- If the ERP or MRP tool *does* try to help, it more plausibly floods rather than focuses —
  one real-world MRP run produced 8,366 action messages in a week against ~150 in the legacy
  system (§3). The PM's actual coping mechanism is to ignore most of it.
- Warehouse-to-warehouse transfer — the cheapest, fastest fix available, zero cash outlay,
  same-week verifiable — is sitting unused: only 27% of companies with the worst excess
  redistribute stock; 74% just discount instead (§2). Not because it doesn't work — Kranenburg
  2006 shows up to 50% lower provisioning cost with no service loss — but because nobody's
  system tells the PM warehouse B has surplus while warehouse A is about to stock out.

**The gap being sold into, in one line: the signal already exists in the data. It arrives
unusable.** (§1's framing — this is an execution gap, not a forecasting gap.)

---

## With this system — the same Tuesday

Twice today (07:00, 15:00), the board recomputes. If nothing crossed a line, nothing happens
— no message, no dashboard the PM has to check just to confirm quiet. At 15:04 the PM's phone
buzzes: a Teams card, "3 action items," each one bold line — a recommendation, not a raw
number: *Transfer 150 units of P1015 from WH-040 to WH-012*, *Buy 150 units of P1002 from
SUP-031 — ₹28.9L of Engine-A output at risk*, *Re-check 40-day-old open PO for P1044, still
short*. One rupee-at-stake figure per line, one line of why-now. That's the entire mobile
surface — nothing else to triage.

The PM taps through to the Review App on one of the three. What they see isn't a summary,
it's the actual arithmetic: `evaluate_feasibility: moq 150, pack_size 1, feasible_qty 150,
excess_qty 90, excess_holding_cost ₹6,120` — a verbatim field dump, not prose that might be
rounding or guessing. The "if approved and wrong" line is spelled out left to right as a
computation ending in the total, not a headline number they'd have to trust blind. Options
considered are shown, including the one not picked and why. For the transfer line, it says
plainly there's no purchase cost — moving owned stock costs nothing — and instead names the
real downside: the donor warehouse is left within `donor_cover_after_units` of its own safety
stock.

The PM stages a decision on each of the three lines — approve, reject, or a one-line note —
and hits Final Submit once. Elapsed time from notification to submitted decision: under a
minute, not the week §1 measures today. Nothing auto-orders; the PM's approval is still the
action. That's deliberate — §6 is explicit that fewer than 5% of organizations will trust
even 10% of planning decisions to run autonomously by 2030, and no credible source shows
autonomous POs happening at real spend today. The pitch isn't "the system decided," it's
"the decision that used to take a week took under a minute, and I can see exactly why."

A week later, on the Fulfilling Orders page, the PM marks the P1002 line delivered. That
closes the loop into a decision ledger — every recommendation, the human call, and the
realized outcome — the one thing named as mandatory-but-not-yet-built in §7: proof of ROI at
renewal, and the PM's own override track record made visible, which is where the Fildes
meta-analysis says human-adjustment value quietly leaks today.

---

## The unit of value, stated explicitly

Not "a notification" and not "a quote." The thing the PM is actually buying is:

1. **A number they don't have to re-derive or gut-check.** Every figure on screen traces to
   a verbatim tool output, not a summary — that's what makes it usable in 30 seconds instead
   of a week of cross-checking.
2. **A decision surfaced before the exposure is unavoidable**, twice a day, not discovered
   after the fact — closing the §1 execution gap, not adding another forecast.
3. **A cross-warehouse option they currently can't see** — the transfer case specifically,
   because it's the single highest-ROI action in the evidence (§2) and the one nobody's
   current tooling surfaces at all.
4. **A bounded number of asks.** Scarcity is load-bearing, not a UI nicety — the alternative
   is 8,366 messages a week and the PM tuning the tool out entirely (§3).
5. **A record that survives the moment** — the ledger — so six months from now there's proof
   this saved money rather than just a stream of cards nobody remembers approving.

If a redesign changes cadence, bundling, or the notification format, it should still deliver
all five of these — they're the actual product, not the pipeline that produces them.

---

## Open question this doc deliberately does not resolve

Is "twice-daily, bundled by signal type, into one Teams card + Review App" still the *right*
packaging for those five things, or just the packaging arrived at incrementally? Candidates
worth weighing against it explicitly, not assumed superior:

- Per-signal-type cadence instead of one global twice-a-day tick (a BOM cascade risk may be
  worth surfacing the moment it's detected; a slow-moving lead-time drift is not).
- A running list/queue the PM triages on their own schedule instead of a push notification
  twice a day — trades "we chose the moment" for "you choose the moment," which cuts against
  the interruption-fatigue argument in §3 but may fit some PMs' workflow better.
- Whether "one quote bundling N signal types" is the right grouping at all, versus one
  notification per signal type with its own micro-cadence.

This is the next thing to settle — with the five value points above as the test any option
has to pass — before touching signal board / Genie / Supervisor implementation.
