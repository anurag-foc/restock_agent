# Inventory Intelligence — Market Evidence & Phase 1 Direction

Research date: September 2026. Every main claim below carries its source.
Sources are labelled: [PRIMARY] independent study/government data · [SURVEY] disclosed methodology, may be vendor-commissioned · [VENDOR] marketing, weaker evidence.

---

## 1. THE GAP WE ARE SELLING INTO

**72% of manufacturers discover material shortages only after production delays become unavoidable.**
**73% say their ERP gives material visibility but cannot prevent execution failures.**
**51% need a week or more to determine corrective action.**
**84% had inventory shortages and 80%+ were affected by excess inventory — in the same 12 months.**
[SURVEY] LeanDNA / Wakefield Research, March 2026. n=150 senior decision-makers at discrete manufacturers with $250M+ revenue across US, CA, MX, UK, FR, DE. Margin of error ±8pp.
https://www.prnewswire.com/news-releases/its-not-a-forecasting-problem-new-research-pinpoints-where-manufacturing-readiness-and-revenue-actually-goes-missing-302737798.html

Takeaway: these companies are short and long at the same time. The problem is not forecasting. It is that the signal already exists in the data and arrives unusable. We should position as closing the EXECUTION GAP — the distance between "the data knew" and "someone acted" — not as better forecasting.

---

## 2. LATERAL TRANSFER IS THE STRONGEST OPPORTUNITY

**Of companies with the worst excess inventory (>20% of total), only 27% use warehouse redistribution — versus 74% who simply discount.**
**38% of all inventory is excess stock on average; bottom-quartile performers run 47–59%.**
[SURVEY] Netstock Inventory Management Benchmark Report, 2024. 2,400+ companies globally, manufacturing/retail/distribution.
https://www.netstock.com/research/inventory-management-report/

**Lateral transshipment reduces spare-parts provisioning cost by up to 50% with no loss of service level.**
[PRIMARY] Kranenburg (2006), peer-reviewed operations research, single-firm study at ASML.
https://www.sciencedirect.com/science/article/abs/pii/S1366554506000135

**Multi-item, multi-location models show savings up to 47% per SKU when lateral transshipments are included at the planning stage.**
[PRIMARY] European Journal of Operational Research / Transportation Research Part E literature stream.

Takeaway: a large, proven saving that almost nobody captures. It also has the cleanest ROI attribution in the product — "we moved 400 units from WH-7, you did not spend X" is verifiable the same week, needs no procurement cycle and no cash outlay. This is the demo and the first invoice.

---

## 3. WE MUST NOT SHIP ANOTHER ALERT SYSTEM

**A single planner's weekly MRP run produced 8,366 action messages per week, against ~150 in the legacy system — including self-contradicting order / cancel / reschedule cycles.**
[PRIMARY — practitioner report] Dynamics User Group forum thread. Note: dated 2009, but it is the only hard self-reported exception-volume figure available anywhere.
https://www.dynamicsuser.net/t/thousands-of-suggested-action-msgs-in-planning-worksheet/21750

**Planners spend roughly one-third of their time on data cleansing, exception handling and routine plan generation.**
[SURVEY] Deloitte, October 2025. 150+ supply chain planning professionals (food & beverage).
https://foodindustryexecutive.com/2025/10/40-of-fb-supply-chain-planners-say-data-and-process-gaps-are-blocking-progress/

**Supply chain professionals spend ~14 hours per week manually tracking data. 92% make gut decisions sometimes or more often, due to insufficient guidance.**
[SURVEY] LeanDNA / Wakefield Research, March 2024. n=250 supply chain, inventory and planning executives.
https://www.prnewswire.com/news-releases/new-survey-reveals-supply-chain-workers-spend-almost-two-days-a-week-manually-tracking-data-302079238.html

Takeaway: if our hourly job emits 40 part-lines, we are the incumbent problem with better prose. We need a HARD OUTPUT BUDGET — "top 5 actions this week, ranked by money" — not "everything that crossed a line." Scarcity is the feature.

NOTE — do not cite the common claim that "planners receive hundreds or thousands of exception messages daily." It traces to vendor marketing with no supporting data. No published study measures what fraction of MRP exceptions actually get actioned.

---

## 4. STALE MASTER DATA IS THE #1 PROJECT KILLER — AND A WEDGE FOR US

**73% of enterprise data leaders cite data quality and completeness as the primary barrier to AI.**
[SURVEY] Forrester Consulting for Capital One, 2024. n=500 enterprise data leaders.

**More than 80% of AI projects fail to reach meaningful production — roughly twice the failure rate of traditional IT projects.**
[PRIMARY] RAND Corporation.

**Bad ERP data drives planner workarounds, which further degrade ERP data — a self-reinforcing loop that undermines planning accuracy and digital transformation.**
[PRIMARY] Strüssmann, Andersson, Hvam & Haug, Production Engineering, 2025. Peer-reviewed multi-case study.
https://link.springer.com/article/10.1007/s11740-025-01379-2

**Market signal: Verusen — the vendor closest to the industrial MRO problem — leads its pitch with data cleansing, not optimization.**
[VENDOR positioning, but informative] https://verusen.com/faq/

Our opening: we already hold fact_procurement. We can compute OBSERVED lead time (PO date to receipt date) and diff it against the contracted lead time that nobody has updated since go-live. Same for MOQ drift. Cheap SQL, no ERP does it, and it explains to the customer WHY their safety stock is wrong.

---

## 5. WHAT SHOULD MAKE US CAUTIOUS

**Fewer than 30% of supply chain AI pilots reach production. Organisations underestimate deployment complexity by 300–500%.**
[ANALYST] Gartner Hype Cycle for Supply Chain Strategy, 2025 — generative AI has entered the trough of disillusionment while traditional ML sits on the slope of enlightenment.
https://www.traxtech.com/ai-in-supply-chain/gartner-report-generative-ai-crashes-into-reality

**Over 40% of agentic AI projects will be cancelled by end-2027 — escalating costs, unclear business value, inadequate risk controls.**
[ANALYST] Gartner, June 2025.
https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-predicts-over-40-percent-of-agentic-ai-projects-will-be-canceled-by-end-of-2027

**95% of GenAI pilots delivered no measurable P&L impact.**
[PRIMARY, with caveat] MIT Project NANDA, "The GenAI Divide", August 2025. 52 interviews, 153 leaders, 300 deployments. IMPORTANT: "no measurable impact" largely reflects ABSENT PRE-DEPLOYMENT BASELINES rather than technical failure. Cite it as "no measured return", never as "failed".

**Human judgmental adjustments to forecasts improved accuracy for only just over half of SKUs. Small adjustments are noise; upward adjustments usually make things worse.**
[PRIMARY] Fildes, Goodwin & De Baets, International Journal of Forecasting, Vol 41 Iss 2, 2025. Meta-analysis of ~147,000 forecasts across six studies.
https://www.sciencedirect.com/science/article/pii/S0169207024000736

**"It is easy to end up with very sophisticated reporting and very traditional human decision making. You see more, but you still decide as you did before."**
[PRACTITIONER] Lokad, December 2025 — the definitive critique of dashboard products.
https://www.lokad.com/blog/2025/12/22/a-reflection-on-lora-cecere-work/

Design consequences: every output must be a decision with a button, never a report. Our review app should discourage small discretionary edits and capture reasoning on large ones. Human-in-the-loop is governance and auditability — not "the planner catches what the model missed."

---

## 6. THE FINDING THAT FAVOURS OUR ARCHITECTURE

**By 2030, only 5% of organisations implementing planning automation will make even 10% of their planning decisions autonomously. Only "simple, explainable and accepted" decisions are suited to autonomy; complex optimisation still requires human judgment and governance.**
[ANALYST] Gartner, November 2025 — Autonomous Planning has passed the Peak of Inflated Expectations.
https://www.gartner.com/en/newsroom/press-releases/2025-11-12-gartner-says-autonomous-planning-has-passed-the-peak-of-inflated-expectations-on-supply-chain-planning-technology-hype-cycle

**No credible source shows organisations trusting autonomous replenishment POs at material value. Quoted "touchless rates" of 60–90% refer to invoice/AP three-way matching, not decisions to spend money on new stock.**
[ANALYST synthesis] Gartner, plus procurement automation literature.

Takeaway: every competitor is selling autonomy. We already built approval-gated, and that is the destination rather than a stepping stone. Our line: we do not auto-order — we make the approve/reject decision take 30 seconds instead of the week that 51% of manufacturers currently need.

---

## 7. PHASE 1 INTELLIGENCE — WHAT WE ARE BUILDING

Seven capabilities, ordered by evidence strength. Each row: the nuance, the problem it solves, the business impact, and the UC function(s) it's built on.

| # | Nuance | Problem it solves | Business impact | Built on |
|---|---|---|---|---|
| 1 | Network surplus / lateral transfer | Only 27% of companies with the worst excess (>20% of inventory) use warehouse redistribution; 74% just discount instead (Section 2) | Up to 50% lower spare-parts provisioning cost, no service-level loss (Kranenburg 2006); zero cash outlay, zero procurement cycle, verifiable same week — the fastest, cleanest ROI story we have | `network_surplus` |
| 2 | BOM cascade → production value at risk | 72% of manufacturers find out about a shortage only after the delay is unavoidable (Section 1) | Converts a part number into a rupee figure ("₹X of Engine-A output at risk") — the number a plant manager can act on and report upward | `bom_component_requirements`, `assembly_risk_report` |
| 3 | Exposure ranking with a hard output budget | Alert fatigue: one real MRP run hit 8,366 messages/week; planners spend ~⅓ of their time on exception handling and 92% end up deciding on gut feel (Section 3) | Directly attacks the reason planners stop trusting the system; scarcity is the feature, not a limitation | `classify_urgency`, `financial_tradeoff_summary` promoted into the scan |
| 4 | Seasonality-adjusted consumption | A flat average daily consumption is wrong for most manufactured parts (pre-holiday ramps, fiscal-year-end pushes) — which means item 3's ranking inherits that error. Under-flags before a ramp, over-flags in a lull | Corrects the number every other ranking depends on. The scan should say "12 days of cover, adjusted for the seasonal ramp" not a flat average — this is what makes a CRITICAL flag trustworthy instead of noisy | `seasonality_adjusted_consumption`, feeding `predicted_stockout_date` |
| 5 | Lead-time reality check (observed vs. contracted) | 73% of AI initiatives fail on data quality; stale master data causes both false shortages and false comfort (Section 4) | Near-zero build cost — `fact_procurement` already has PO date and receipt date. Explains WHY the customer's safety stock is wrong, not just that it is | New: diff against `dynamic_reorder_point`'s contracted lead-time input |
| 6 | Supplier reliability scoring | The cheap/unreliable supplier is invisibly expensive: Walmart charges up to 3% of PO value on late orders, Kroger fines $500/order 2+ days late; cost of poor quality runs 15–20% of sales | Turns a sourcing decision that's currently a gut call into a number procurement can defend | `supplier_reliability_score`, `ranked_suppliers` |
| 7 | MOQ / pack feasibility | A recommendation the customer can't execute is worth nothing — undermines trust in every other number we show | Table stakes for adoption; without it, everything above is theoretical | `feasible_order_qty` |

Ordering logic: items 1–2 are the demo (fast, provable, no spend required). Item 3 is what keeps the product usable at scale instead of becoming the next ignored dashboard — and item 4 is what keeps item 3 honest. Items 5–7 are what make the recommendation trustworthy and executable once a PM is actually on the approve/reject screen.

DEFERRED to phase 2: plant capacity checks, what-if simulation. The consumption anomaly detector (Z-score) is repurposed into item 5 as a master-data trust signal rather than a standalone feature.

PLUS ONE NON-ANALYTICAL FEATURE, now mandatory: A DECISION LEDGER. Log every recommendation, the human decision, and the realised outcome. Two payoffs — we can prove ROI at renewal, which the MIT finding says almost nobody can; and we can show the customer their own override track record, which the Fildes meta-analysis says is where value quietly leaks. No incumbent does this.

---

## 8. STATISTICS TO NEVER USE

These are widely quoted and will cost us credibility with an informed supply chain buyer.

- "$22,000 per minute of stopped automotive production" — a 2006 survey of 101 executives, twenty years stale, still recycled as current. (ATS/Nielsen, 2006)
- "Inventory carrying cost is 20–30% per year" — the only traceable published anchor is a 1995 trade magazine article assuming 1990s interest rates. Build carrying cost bottom-up from the customer's actual WACC, warehouse cost, insurance and observed shrinkage.
- "50–60% of MRO inventory is excess or obsolete" — no locatable primary study; traces to consultant estimates from self-selected client engagements.
- "50% of downtime is caused by parts stockouts" — originates in a June 2018 IBM press release citing an Aberdeen study that cannot be found.
Source for these audits: https://reliamag.com/guides/mro-spare-parts-inventory-statistics/

SAFE ANCHORS TO USE INSTEAD:
- $1.7 trillion trapped in excess working capital — 35% of gross working capital, 11% of aggregate revenue. Hackett Group 2025 US Working Capital Survey, computed from the filings of the top 1,000 US public non-financial companies. Strongest source available. https://www.thehackettgroup.com/2025-working-capital-survey-payables-rebound-receivables-inventory-lag/
- European days inventory outstanding rose 4% to 68.9 days, a decade high. Hackett Group 2025 Europe Working Capital Survey.
- 38% of inventory is excess stock. Netstock 2024, 2,400+ companies.
- 11% of US manufacturing plants cite raw material shortages as a key impediment to capacity utilisation — roughly double the ~5% baseline of 2014–2016, down from ~40% at the 2021–22 peak. US Census / Federal Reserve capacity utilisation data — government statistical series, the most rigorous figure in this report. https://www.scmr.com/article/us-manufacturing-raw-material-shortages-2025-sector-trends-analysis
- 61% of manufacturers experienced unplanned downtime in the past year, averaging $1.7M per hour. Fluke / Censuswide, October 2025, n=600 across four industries and three countries. https://reliability.fluke.com/unplanned-downtime-costs-manufacturers-up-to-852m-weekly/

---

## 9. ONE CONCLUSION WORTH STATING PLAINLY

There is currently no independent evidence that an LLM improves inventory outcomes. Gartner places traditional machine learning on the slope of enlightenment specifically because it delivers "specific, measurable improvements on structured data within controlled parameters", while generative AI sits in the trough.

The money is in the deterministic analysis. Our architecture happens to be right on this — the analysis already lives in governed SQL functions and the agent only orchestrates and narrates. But we should be honest internally: Genie makes the output ADOPTABLE, not RIGHT. We sell the outcome. The agent is how it gets explained and approved, not how the money is found.

---

NEXT STEP: verify the four data dependencies this rests on —
(a) multi-warehouse stock coverage, for lateral transfer
(b) BOM data and parent assembly unit value, for value at risk
(c) sufficient trailing-year history for seasonal multipliers, for the seasonality adjustment
(d) PO receipt dates in fact_procurement, for the lead-time reality check
