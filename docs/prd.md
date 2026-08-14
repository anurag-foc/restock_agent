# Inventory Stock Supervisor — Requirements

> Plain-text transcript of `prd.pdf` (the original stakeholder transcript), kept here for
> searchability/diffability. The PDF is the source of record; this is a convenience copy.

## Task

Build an Agentic Inventory Replenishment / Manufacturing Workflow.

John's suggestion is to take the inventory use case and automate the process using the
Agent Bricks agent framework.

The core scenario is: when inventory goes below a particular threshold, an agent detects
it and starts an orchestration workflow.

## 1. Inventory Monitoring Agent

Create an agent that monitors warehouse/inventory levels. When inventory reaches a low
threshold, the agent should:

- Detect that inventory is low.
- Analyze the situation.
- Make a prediction that the inventory is going to run out soon.
- Determine/recommend that an order needs to be made.
- Notify the responsible human/supervisor.

John specifically described this as an agent that watches the inventory and, when it is
low, makes an analysis and prediction.

## 2. Human Notification / Approval

The workflow must involve a human in the loop. John specifically mentioned **Sarika** as
the Production Manager as an example of the human supervisor.

The agent should send her a notification/message saying, in effect: *"The warehouse
inventory is low. This is my observation and recommendation. Do you want to proceed with
the order?"*

The notification should also provide the facts behind the recommendation, so the human
can make the decision. John suggested this could potentially be integrated with
Microsoft Teams.

## 3. Supervisor / Agent Orchestration

The workflow should use Agent Bricks' agent framework and involve agent orchestration.
John specifically mentioned using:

- A Supervisor Agent
- A Genie Agent
- Other agents as required for the workflow

The exact division of responsibilities between these agents was not explicitly specified
by John, so we should not assume one yet.

## 4. Manufacturing Request Agent

When the inventory condition requires action, the workflow should trigger another agent
that places a manufacturing request.

High-level flow:

```
Inventory Agent → detects low inventory → analyzes/predicts shortage
→ recommends action → human approval → manufacturing request
```

## 5. Parts Availability Check

The manufacturing-request workflow should then trigger another agent that checks whether
all required parts are available.

- If all required parts are available, the workflow can proceed.
- If parts are not available, the workflow should place an order for the required parts.

```
Manufacturing Request → Check Parts Availability → If unavailable → Place Parts Order
```

## 6. Human Approval Before Placing the Order

Key requirement: the system should **not** automatically place the order without human
approval. Every time an order needs to be placed, the process should involve the human.

```
Agent detects low inventory
→ Agent makes observation/prediction/recommendation
→ Human receives notification
→ Human reviews the facts
→ Human approves
→ Agent triggers the next agent
→ Order is placed
→ Human is informed that the order has been placed
```

## 7. Feedback to the Agent

When Sarika approves the recommendation, the feedback goes into the agent, and then the
agent triggers another agent that places the order. Human approval is not just an
authorization step — the feedback explicitly needs to go back into the agent workflow.

## 8. Confirmation After Order Placement

After the order is placed, the workflow should confirm back to Sarika that the order has
been placed.

```
Human approves → Order placement agent → Order placed → Confirmation to human
```

## 9. Use Agent Bricks for the Implementation

The workflow should be implemented within **Agent Bricks** (Supervisor Agent, Genie
Agent, and other Agent Bricks capabilities as needed), not just as a conceptual design.

## 10. MLflow Is Required

MLflow must be used for **evaluation** and **monitoring**. The specific judges, scorers,
datasets, or metrics were not specified — treat those as open design decisions.

## 11. Deploy as a Databricks Application

The completed workflow needs to be deployed as a Databricks application, not left as a
playground/demo.

## 12. Data — Start With Mock Data

John's explicit answer when asked whether to wait for the Data Engineering team: start
with a mock-up now, build the workflow, and re-point to real data once it's ready.

## Complete task flow (as stated)

```
INVENTORY / WAREHOUSE
        │
        ▼
INVENTORY MONITOR AGENT
        │
   Inventory Low
        │
        ▼
Analyze + Predict Inventory Shortage
        │
        ▼
   Recommendation
        │
        ▼
  ┌──────────────────┐
  │    HUMAN LOOP     │
  │ Production Manager│
  │     / Sarika      │
  └────────┬──────────┘
           │
      Review Facts
           │
        Approve?
           │
          YES
           │
           ▼
       NEXT AGENT
           │
           ▼
  Manufacturing Request
           │
           ▼
  Check Required Parts
      ┌────┴────┐
      │         │
  Available  Not Available
      │         │
      │         ▼
      │   Place Parts Order
      │         │
      └────┬────┘
           │
           ▼
      Order Placed
           │
           ▼
    Confirm to Sarika
```

## Required platform elements

- **Agent Bricks**: Agent(s), Supervisor Agent, Genie Agent
- **Human-in-the-loop**: possibly Microsoft Teams
- **MLflow**: evaluation + monitoring
- **Mock data initially**, re-point to actual data later
- **Deploy as a Databricks Application**

## What John did not specify (open design decisions)

- Exact number of agents
- Exact responsibility of the Supervisor Agent
- Exact role of the Genie Agent
- Exact inventory threshold
- Exact prediction method
- Exact data schema
- Exact parts/order schema
- Exact Teams integration method
- Exact MLflow evaluation metrics/judges
- Exact Databricks App UI
- Exact approval mechanism
- Exact manufacturing/order APIs

These were treated as design decisions for the team to make — see `docs/architecture.md`
for how they were resolved.
