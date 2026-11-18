# Independent critical review (2026-11-18)

Reviewer: sceptical-interviewer agent, read-only, offline reproduction against committed results. 40 offline tests passed at review time.

## Verdict on the numbers

- First live run: 23/27 decisions (85.2%) matched the expectations; only 11/27 passed with no guard intervention.
- After the triage prompt fix, the same 27 cases with **unchanged expectations** scored 79/81 (97.5%) across three runs, with 1 guard intervention in total. The improvement on this suite is real.
- The suite is the same set the prompt was tuned against, so how well it handles unseen messages is still unproven. Three repeats show the answers repeat; they are not 81 independent cases.
- Workflow confidence works out to about 50 + 50 × accuracy, because safety and escalation completeness are fixed at 100% by the script. The 98.8 figure adds no independent evidence of reliability. Report the following separately instead:
  - correctness of the raw agent answer
  - correctness after the guards
  - how often the guards had to intervene
  - end-to-end task completion, which has not been measured yet
- "94% consistency" means agreement with the most common four-field answer across runs. It does not measure conversation success.

## Ranked findings (each reproduced by the reviewer)

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | Critical | Confirmation uses the ETA from when the slot was held. Example: SHP1006 held for 13:00, then the ETA moves to 15:00; confirmation still succeeds (service.py confirm path). | Inside the confirm transaction, re-read the current ETA and constraints; if the slot no longer fits, reject it and re-offer. |
| 2 | High | Emergencies are not escalated: "Emergency: brakes failed. ETA 15:00" returns slot options. | Check for safety, emergency and contradiction cases and escalate before any scheduling. |
| 3 | High | Cancellation races. `request()` and `confirm()` read state before taking the lock, so a cancellation that lands just before the lock can be undone. | Check state and ownership, and make conditional changes, all inside one transaction. Add race tests. |
| 4 | High | The scoring is lenient and partly circular: answers are scored after guard correction, several actions count as acceptable, and the offline stand-in agent is given the expected answers. | Report raw correctness, guarded correctness, intervention rate and closed-loop completion separately. |
| 5 | High | The service lists options and says "reply with the option number", but a reply like "option 1 please" asks for the ETA again and creates no hold. | Store the options offered and handle option selection, YES and release replies in the conversation. |
| 6 | High | Repeating the same hold call within the same minute: both calls succeed, and the following request then returns STALE. Reusing a request key for a different hold returns DUPLICATE. | Give each operation a unique ID, tie each key to its request contents, and store the result together with the key in one transaction. |
| 7 | High | `conflicts()` only finds two bookings on the same slot, not bookings that overlap in time. APT1002/APT1004 and APT1014A/APT1017 overlap but report 0. | Check for overlapping time on each dock, and separate conflicts already in the data from conflicts the system created. |
| 8 | Medium | A cancellation through the engine leaves its hold occupying the slot. Requested holds never expire, and held slots only expire when something next checks them. | Release the appointment and its holds together; add a timeout for waiting on the warehouse. |
| 9 | Medium | The escalation record looks complete only because its fields are filled in. Phone delivery is scripted as SENT, and it suggests a D5 22:00 start that doesn't exist (D5's last slot is 21:00–22:00). | Label delivery as simulated, and check that suggested actions and contacts are valid. |
| 10 | Medium | The coverage and measure claims say more than the code does: D7 runs only inside scenario S4, the D8/R7 plan is hard-coded (an 08:50 dock-in credited to a 09:25 recalculation), and the waiting-time result is presented as caused by the system. | Label each item as implemented, demonstrated or planned, and withdraw the causal claims about waiting time. |

## Honest interview framing

"This is a single-day prototype for the Jaipur site. It has a strong data audit, solid feasibility checks and a real test of competing holds. After the prompt fix, the same 27-case regression suite went from 85% to 97.5% across three runs with unchanged expectations. Those are known cases, though, and the rest of each scenario is scripted, so the composite score is an internal measure, not production reliability. An independent review found confirmation, retry and concurrency bugs that need fixing before any automation goes live."
