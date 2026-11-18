# SetuHaul — 15-minute customer demo script

Audience: SetuHaul operations leadership (Jaipur DC), possibly an IT lead. Goal: show that the assistant handles
real driver exceptions safely, never double-books, and hands humans the cases that need them.
Artifact: `brief_simulation.html` (self-contained, opens offline in any browser).

## 0. Setup (do this 10 minutes before)

```bash
cd SetuHaul_Data_Package
python3 --version                      # 3.10+ works; built on 3.15.2, SQLite 3.54.1, no pip installs
python3 -m unittest discover -s tests -q   # all green = engine, guards, race test, offline scenario run
```

Pick the report to show:

| Situation | Command | What you get |
|---|---|---|
| Show the committed live run (recommended) | `git show HEAD:brief_scenarios.json > brief_scenarios.json && python3 brief_scenarios.py --render` | HTML from the live Nemotron run: 92.6/100 |
| Fresh live run (needs `NVIDIA_API_KEY` in `.env`, ~10 min, ~27 calls at ~22 s) | `python3 brief_scenarios.py` (add `--repeats 3` for consistency, ~30 min) | New JSON + HTML |
| No network / API down | `python3 brief_scenarios.py --offline` | Same scenarios, deterministic stand-in agent (100% by construction — say so) |
| HTML only from saved JSON | `python3 brief_scenarios.py --render` | Re-renders, no API calls |

Open it: `open brief_simulation.html` (macOS) — or drag it into a browser. Check the header reads
`Model: nvidia/nemotron-3-ultra-550b-a55b · mode: live · 8 distinct scenarios · 27 agent calls`.
Have `docs/client_readout.md` open in a second tab.

> If a rerun with the new triage prompt has landed, its numbers will differ from those below. Quote what the
> header shows, and only claim the improvement once you have seen it.

---

## 1. Opening — the problem (1 min)

"Your Jaipur DC handles 190–240 receiving appointments a day. On a normal day 15–25 drivers message about
delays; in a bad half-hour it's 20–35, and five coordinators absorb all of it by phone and WhatsApp, with no
GPS. We built an assistant that answers drivers, re-slots trucks and escalates the hard cases — and we built it
so that it **cannot** double-book a dock or confirm something the warehouse hasn't accepted."

Key sentence to repeat: **"The AI reads and suggests; the scheduler decides."**

## 2. Top of the report — headline numbers (1.5 min)

Point at the KPI tiles:

- **Workflow confidence 92.6/100** — "a rubric: half agent decision quality, 30% safety, 20% hand-off
  quality. Not a probability."
- **Agent decision accuracy 85%** (23 of 27 live decisions as expected).
- **Conflicting allocations 0** — "no slot held or booked twice, no truck with two current bookings."
- **Stale options caught 1** — "an option that went away before the driver said yes was caught, not booked."
- **Escalation completeness 100%** — 3 hand-offs, every required field filled.
- **Guard interventions 13** — "13 times the safety layer corrected the AI — mostly it claimed something was
  *verified* when only the driver had said so. Every one was caught before it touched the schedule."
- **Priority violations 0**, **Agent errors 0**.

Scroll to **Coverage against the brief**: every ladder level, run item and decision has a green chip. "Each
chip opens the scenario that proves it."

Scroll to **Measures**: avg 5.4 turns to outcome, 62% resolved with no human takeover, 2 clarification turns,
standard-dock utilisation 70% in the 11:00–14:00 crunch, waiting 83.3 → 78.3 min in S7, mean agent latency
21.8 s.

## 3. Walk-through (9 min, ~1 min each; S4 and S5 get 2)

For each: click the tab under **Scenario simulation**, then use **▶ play** (or ▶ to step). The right side is
the live **Jaipur dock board** (D1–D6, 08:00–22:00); changed cells flash. Expand **agent I/O** on any
TRIAGE/DISPATCH line to show the exact input, raw model output, and the answer after guards.

### S1 — Options only (THR011, SHP1017)
- Driver: "What slots are possible after 1 PM if I reach around 12:45?"
- ETA rule: "around" → MEDIUM confidence, +15 min buffer.
- Dispatch proposes; the bot sends a 3-option comparison (D1/D2/D3 13:00).
- **Point at the last ENGINE line:** holds before = 0, after = 0, APT1017 still CONFIRMED. "Asking is not booking."

### S2 — Missing facts (THR003, THR010)
- "I am late by one hour." → LOW confidence, **no slot search** until the driver answers one question.
- Driver: "Reaching gate 11:00" → the pending 11:00 is now unsafe (planning ETA 11:15) → held D2 12:00 → REQUESTED.
- THR010: DRV004 has two loads and names neither → bot asks which one → SHP1020 → full HELD → REQUESTED →
  CONFIRMED (D2 19:00, ref WH-JAI-9120). Show the message-to-rows trace line (MSG016 → THR010 → DRV004 → SHP1020 → APT1020).
- Honest note: one triage answer here asked the warehouse if D2 was free — that's one of the 4 misses (see §5).

### S3 — Rules narrow the choices (SHP1010, SHP1016, SHP1015, RULE005)
- Reefer SHP1010 → only D5. Heavy SHP1016 (31 t) → only D6, first at 12:30.
- Driver: "D4 looks empty, put me there." **Read the refusal aloud**: "I can't book D4: your load is 31,000 kg and
  D4 takes up to 25,000 kg…" — "plain language, with the rule, and the next best option."
- SHP1015 reefer at 18:30 → no feasible dock. RULE005: no new start at/after 21:00 without approval.

### S4 — Five trucks chase the same slots (the money slide, 2 min)
- SHP1014 (CRITICAL), SHP1006 (HIGH), SHP1012, SHP1013 (NORMAL), SHP1009 (CRITICAL, confirmed).
- Expand the **Pre-allocation audit** data: APT1014A is 21,000 kg on a 20,000 kg dock — "your current booking
  was already infeasible."
- Read the **ordering policy**: priority → most-constrained first → LOW-confidence not held → earliest report.
- Result: SHP1014 → D2 12:00 (+13:00 for its 75-min unload), SHP1006 → D1 13:00, SHP1013 waits for clarification,
  SHP1012 can't meet its 13:30 leave-by → **escalated** with two recommended options.
- **Concurrency proof line:** "3 threads, separate DB connections, same slot, same instant → 1 winner, 1 live hold."

### S5 — The world changes mid-conversation (2 min)
- DEVT001: D3 breaks down under SHP1005 (in the yard since 09:05). Earliest safe dock means a 175-min wait →
  escalated; coordinator approves the held D2 12:00.
- THR009 is a duplicate of THR001 → DUPLICATE, no second hold, no second alert.
- SHP1006's 10-min hold expires; SHP1013 takes that slot; SHP1006's late "YES" → **STALE**, apology +
  re-offer D3 13:00 → retried YES is a DUPLICATE (idempotent) → confirmed WH-JAI-9106.
- Replayed APT1008 cancellation is a no-op.

### S6 — No feasible slot (SHP1015, THR005, OM004)
- Reefer at 18:30; D5 is in maintenance 18:00–22:00; RULE005 bars ≥21:00.
- "The original escalation email OM004 **failed**, and there's no Jaipur night contact." → hand-off goes to the
  shift manager **by phone**, with 3 options (approve 22:00 start, hold overnight with reefer running, divert to
  Gurugram).
- Show the driver message: tells them what happens next and to keep the reefer running.

### S7 — Early, late and in-progress (SHP1002/1003/1004)
- Recalculate only on real triggers (gate check-in, facility WIP update, verified dock event, confirmed ETA change).
- Expand the data: average gate-in → dock-in wait **83.3 → 78.3 min**; "and the yard supervisor still approves
  before anyone moves."

### S8 — The human side (THR007, DRV012, DRV013, THR012)
- "I'm at the gate" with a gate check-in → accepted. Same claim without a check-in → asks the gate → not there →
  polite reply, **no penalty**. "D2 is broken" → facility says it's fine → nothing changes. Cancelled load →
  "don't travel."

## 4. Close (1 min)

"What this proves: the assistant handles every situation in your brief, a bad AI answer can't corrupt the
schedule, and your coordinators only see the cases that need a human — with everything they need on one card.
What we need from you is in the readout: the real hold time you'll accept, a night contact for Jaipur, and
fixing the four bookings that are already infeasible."

## 5. Likely customer questions

| Question | Answer |
|---|---|
| What if the AI is wrong? | It only suggests. The scheduler checks every rule again before anything is held or confirmed. 13 AI corrections in this run, 0 reached the schedule. |
| What were the 4 misses? | All the same: the AI asked the warehouse "is the dock free?" instead of letting the scheduler answer. We changed the instruction; the rerun is pending. |
| Can two drivers get the same slot? | No. Shown live: 3 simultaneous attempts, 1 winner. A database rule backs it up. |
| What if a driver lies? | We don't call it lying. We plan on their ETA, but anything that affects other trucks (at the gate, dock broken) is checked with the gate or facility first. No automatic penalties. |
| How long is a slot held? | 10 minutes for a driver to reply YES; then it's released. Coordinator holds 30 min. Both configurable. |
| How fast is it? | ~22 s per AI decision on the hosted model today. Routine replies can be templated; a smaller model for triage would cut this. |
| Will coordinators be flooded in a spike? | Duplicates are suppressed, repeats merged, low-impact items go to a digest; the simulated queue never exceeded 2 waiting items with 5 coordinators. |
| Does it need GPS? | No. It works on driver ETAs, gate check-ins and facility updates, and it's explicit when something is unverified. |
| Where does our data go? | Only IDs, times, weights and dock codes go to the model — no names or phone numbers. The model is open-weight and can be hosted in India. |
| What does it replace? | Nothing on day one: we propose a 2-week shadow pilot where coordinators see its suggestions alongside their normal work. |
| Gurugram? | The data supports both sites; the scheduler is set up for Jaipur first. |

## 6. Fallbacks

- **No network / API key issue:** `python3 brief_scenarios.py --offline` then open the HTML. Say: "This is the
  same workflow with a scripted stand-in for the AI; the live numbers are in the readout." Note `--offline`
  overwrites `brief_scenarios.json`/`.html`; restore the live run afterwards with the `git show` line below.
- **JSON was overwritten by a partial rerun:** `git show HEAD:brief_scenarios.json > brief_scenarios.json`
  then `python3 brief_scenarios.py --render`.
- **Browser blocked:** the readout (`docs/client_readout.md`) has all the numbers; tests (`python3 -m unittest
  discover -s tests -v`) show the race and stale-option tests passing in the terminal.
- **Want to show the database itself:** `sqlite3 setuhaul_freight_operations.db "PRAGMA integrity_check;"` → `ok`.

## Timing card

| Min | Section |
|---|---|
| 0–1 | Problem |
| 1–2.5 | Headline KPIs, coverage, measures |
| 2.5–4.5 | S1, S2 |
| 4.5–5.5 | S3 |
| 5.5–7.5 | S4 |
| 7.5–9.5 | S5 |
| 9.5–11.5 | S6, S7, S8 |
| 11.5–12.5 | Close and asks |
| 12.5–15 | Questions |
