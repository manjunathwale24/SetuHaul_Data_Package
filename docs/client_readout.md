# SetuHaul driver-exception assistant — client readout

**For:** SetuHaul Logistics operations leadership, Jaipur DC · **Data:** snapshot of 4 Aug 2026 (Asia/Kolkata)
· **Model:** NVIDIA Nemotron (`nemotron-3-ultra-550b-a55b`), live run of 27 AI decisions

## What we built

A chat assistant for inbound drivers that handles delay reports end to end. It reads the message, asks one
short question when something is missing, finds dock slots that really fit the truck, holds the chosen slot for
10 minutes, asks the warehouse to confirm, and books it only after re-checking every rule. When no safe answer
exists, it hands the case to a coordinator with everything needed on one card.

The design principle is **the AI reads and suggests; the scheduler decides.** The AI never changes a booking
directly. A rules engine checks dock type, reefer, weight, unload length, dock breakdowns, operating hours and
every other truck before anything is held or confirmed. A safety layer corrects the AI when it overreaches.

## Results (8 scenarios covering every situation in the brief)

| Measure | Result |
|---|---|
| Double-bookings / trucks with two live bookings | **0** |
| AI decisions as expected | **23 of 27 (85%)** |
| AI over-reach caught by the safety layer before touching the schedule | **13 of 13** |
| Options that went stale before the driver said yes, caught and re-offered | 1 of 1 |
| Escalations with a complete hand-off record | 3 of 3 (100%) |
| Cases resolved without a coordinator | 5 of 8 scenarios (62%) |
| Priority-policy violations when 5 delayed trucks competed | 0 |
| Simultaneous booking test (3 trucks, same slot, same instant) | 1 winner, 0 duplicates |
| Average yard wait after resequencing on a gate check-in | 83.3 → 78.3 min |
| Questions asked to drivers for missing facts | 2 (only where needed) |
| Overall rubric score ("workflow confidence") | **92.6 / 100** |

The score is a weighted rubric (50% AI decisions, 30% schedule safety, 20% hand-off quality), not a probability.
An earlier 15-minute live simulation with role-played drivers scored 80.7 with 100% safety; we then built
scenario-by-scenario tests because random drivers kept repeating the same situations.

## What it proves

- **Asking is not booking.** A driver asking for options gets a comparison; nothing is reserved until they choose.
- **Missing facts are asked for, not guessed.** "Late by one hour" triggers a question, not a slot search.
- **Hard rules hold.** A 31-tonne truck asking for an empty standard dock is refused politely, with the rule and
  the next safe option.
- **Competition is fair and explainable.** Priority first, then the most constrained truck, then whoever reported
  first; low-confidence ETAs don't block firm ones.
- **Changes mid-conversation are safe.** Breakdowns, expired holds, duplicate messages, retried "YES"es and
  replayed cancellations never create a second booking.
- **Drivers are treated fairly.** Claims are checked against gate and facility records, never penalised
  automatically.

## Data issues found in your snapshot

1. **APT1014A (SHP1014, 21,000 kg) is pending on D1, which takes 20,000 kg.** APT1005 (SHP1005, 20,500 kg on
   D3, 20,000 kg) has the same problem. These bookings cannot be honoured as made.
2. **Unloads longer than their 60-minute slot spill into the next truck's slot:** APT1002 (70 min) into APT1004;
   APT1014A (75) into APT1017; APT1005 and APT1009 (75) into the following slot. The booking system doesn't
   reserve the extra time.
3. **OM004 (the reefer exception for SHP1015) failed** — it went to a night-shift address, and Jaipur has no
   night-shift contact on file (the only one is a vacant Gurugram role with no email). OM002 (SHP1013
   confirmation request) is still queued and was never sent.
4. **Dock events and slots disagree:** D3's 09:00 slot stays open although the breakdown starts at 09:15, and the
   availability view trusts vague driver ETAs. We corrected both in the assistant, not in your data.

Each issue has a one-line SQL check in our knowledge base so your team can re-run it.

## Risks

- **AI routing:** all 4 misses were the AI asking the warehouse "is the dock free?" — work the scheduler already
  does. We changed its instructions; a confirming rerun is pending.
- **Speed:** ~22 seconds per AI decision on the hosted model today. Fine for chat, too slow for a spike unless
  routine replies are templated or a faster model handles triage.
- **ETA quality:** with no GPS, plans rest on driver ETAs. Our +15-minute buffer for uncertain ETAs is a starting
  assumption, not yet calibrated on your history.
- **Scope:** tested on Jaipur, one day, simulated warehouse replies. Gurugram and real integrations are not yet
  covered.

## Decisions that need your people

- Any case with no feasible slot (e.g. reefer during D5 maintenance).
- Moving one truck's booking to make room for another, or approving a short overlap.
- Starts at or after 21:00 (RULE005 exception).
- Contradictory reports (driver vs gate vs facility), safety issues, regulated loads, emergencies.
- Any penalty or carrier action — the assistant never does this.

## Recommended next steps

1. Rerun the live test with the new instructions, 3 times, and share consistency.
2. **Two-week shadow pilot at Jaipur:** the assistant drafts replies and slot proposals; coordinators approve.
   Measure auto-resolution, coordinator minutes saved, dock-in waits, and any disagreement.
3. Move holds and confirmations onto your production database with real timers and an outbox for messages.
4. Calibrate the ETA buffer per carrier from past ETA vs gate-in data.
5. Extend to Gurugram.

## What we need from you

- **Fix the four infeasible or overrunning bookings** (APT1014A, APT1005, APT1002/APT1004, APT1009) or confirm how
  the floor actually handles them.
- **A named Jaipur night-shift contact** (phone and email) for after-hours escalations.
- **Hold policy sign-off:** is 10 minutes for driver reply and 30 minutes for coordinator decisions acceptable?
  What is the warehouse's confirmation SLA?
- **Priority policy sign-off:** CRITICAL > HIGH > NORMAL > LOW, then most-constrained, then first report.
- **6–8 weeks of ETA and gate-in history** to calibrate buffers.
- **Data-hosting preference** — the model is open-weight and can run in India if required.
