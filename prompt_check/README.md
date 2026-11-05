# Prompt check: focused prompt vs. Samantha's prompt (gpt-6.1-sol, 2026-11-04)

Question (professor): does a prompt that extracts only the CI parameters each dataset actually varies keep extraction
quality? Tested on 5 records per dataset that Samantha had already extracted with her prompt (`test_ids.txt`).

- **Old** = Samantha's prompt (`scripts/run_pilot_sam.py` on `samanthas-eval`), re-run unchanged with `gpt-6.1-sol` at
  temperature 0, because her outputs are not on GitHub: `samantha_prompt/gpt-6.1-sol.jsonl`.
- **New** = `prompts/<dataset>/extract_ci_focused.j2` through the repo pipeline (`--prompt-variant focused --blind-native-ci`):
  `focused/<dataset>/openai__gpt-6.1-sol/`.
- Side by side: `side_by_side.txt`. Both outputs were put through the same evidence check (quotes must be in the text): 0 warnings either way.

Checked by Claude (AI), not by a person: have a human confirm before treating it as the review the professor asked for.

## AirGapAgent-R (5 records)

The item states only a task (domain + scenario) and one profile attribute.

| item                                               | old                                      | new                                                               |
| -------------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------- |
| information_type                                   | 5/5 correct                              | 5/5 correct (keeps the attribute name verbatim)                   |
| sender, subject, recipient, transmission principle | null in all 5 (correct: not stated)      | null in all 5, reason "not specified by AirGapAgent-R"            |
| purpose                                            | 5/5 fine                                 | 5/5 fine                                                          |
| domain                                             | 4 clear + 2 "Unclear" (vacation, movies) | 4 clear + vacation "Other / Travel and leisure", movies "Unclear" |

No difference in quality. The new prompt is more explicit about why the four parameters are empty.

## PiSAs (5 records)

| record                                              | old                                                                                                                              | new                                                                                                                                                                      |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| meeting_allocation/24 (task names a private reason) | lists all 5 attendees as senders and subjects and all their private facts as the information type; transmission principle missed | Henry → Maya Chen, Henry's HR-complaint meetings, "visible to 1: Henry": exactly the three attributes PiSAs designed as the private reason (a9, a11, a13). All 5 correct |
| thesis_readiness/01                                 | lists 8 authors as sender, everyone as subject, ~8 facts as the information type: not one flow                                   | Mateo Dubois → Carlos Hassan about Jamie Costa, visibility correct; information type **partial**: mixes a private fact (a1, inappropriate) with a task fact (a2)         |
| inpatient_discharge/09                              | coherent, but describes the task request (system → coordinator), not a private-information flow                                  | Elena Nakamura → Sienna Chen about Jamie Johansson, visibility correct; information type **partial**: mixes clinical stability with the wife's death (a1, inappropriate) |
| severity_classification/12                          | coherent task-request flow (alert system → on-call)                                                                              | Kofi Dubois → Liam Kowalski, a restricted task finding, visibility correct; subject left null with a stated reason (no cast member is the subject)                       |
| uas_flight_readiness/21                             | coherent task-request flow                                                                                                       | Jonas Tanaka → Liam Meyer about Tariq Okafor, a restricted task note, visibility correct                                                                                 |

In 4 of 5 the task description names no single private fact. The new prompt then uses its fallback (the most
restricted artifact relevant to the task) and says so in `unknown_facts`, as instructed.

Totals over the 25 PiSAs CI items: **new** 23 correct + 2 partial. **Old**: the 3 task-request records are internally
correct (15) but describe boilerplate the benchmark does not vary; the 2 lumped records are not single CI flows
(about 9 partial + 1 missed).

## Tokens per record

The new prompt is about 1,000 input tokens longer (shared rules, full domain list, output schema) but its answers are
shorter: AirGapAgent-R about 40–180 fewer output tokens, PiSAs about 160–970 fewer.

## Verdict

Quality does not drop: it is the same on AirGapAgent-R and better on PiSAs, where the old prompt often does not
produce a single flow. Caveats: only 5 records per dataset, an AI check, and Samantha's outputs re-generated rather
than taken from her file. Possible tweak: tell the PiSAs prompt to use exactly one fact (one artifact line), which
fixes the two partial information types.
