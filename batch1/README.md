# Final set: AirGapAgent-R and PiSAs (Enrique's 100 of the 250)

The final set is 250 records, 50 per dataset. Samantha covers ConfAIde, PrivacyLens and MPCI-Bench (150); this folder is
the other 100: 50 AirGapAgent-R + 50 PiSAs.

## Prompt decision

| dataset       | prompt                                                      | why                                                                                                                                                                            |
| ------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| AirGapAgent-R | focused (`prompts/airgapagent_r/extract_ci_focused.j2`, v1) | Same quality as the original prompt on the 5-record check; it states why sender, subject, recipient and transmission principle are empty instead of leaving them blank.        |
| PiSAs         | focused (`prompts/pisas/extract_ci_focused.j2`, v1)         | Better than the original prompt on the 5-record check (23 correct + 2 partial of 25 values vs about 15 / 9 / 1): it gives one flow instead of listing everyone and every fact. |

Details of that check: `prompt_check/README.md`. Neither dataset gives all five CI parameters, so both needed the
model; datasets that do give all five (PrivacyLens, MPCI-Bench) are not run twice.

## Sample

`enrique_sample.json`: seed 20261104, drawn proportionally to subset size, excluding every record already used in the
Llama pilot (`pilot/pilot_sample.json`) and in Samantha's pilot (`data/processed/pilot/pilot_samples.jsonl`).

- AirGapAgent-R: 50 from `full` (4,130 available).
- PiSAs: 35 `expanded` + 15 `original` (236 available).

## Extraction

`runs/<dataset>/openai__gpt-6.1-sol/`: `gpt-6.1-sol`, temperature 0, `--prompt-variant focused --blind-native-ci` (the model
extracts every parameter itself; the benchmark's own values stay in `native_ci` for comparison). Open `extractions.csv`
to read them.

|                                   | AirGapAgent-R                    | PiSAs                                            |
| --------------------------------- | -------------------------------- | ------------------------------------------------ |
| valid / failed                    | 50 / 0                           | 50 / 0                                           |
| validator warnings                | 0                                | 0                                                |
| parameters filled                 | information type only, by design | all 5 (subject empty in 4, with a reason)        |
| agrees with the benchmark's value | information type 50/50           | recipient 50/50, subject 30/35 (expanded subset) |
| tokens in / out                   | 173k / 20k                       | 232k / 41k                                       |
| time per record                   | about 5 s                        | about 10 s                                       |

## Known limits (worth a look in the review)

- PiSAs task descriptions rarely name the private fact (5 of 50). In the other 45 the prompt falls back to the most
  restricted artifact relevant to the task and says so in `unknown_facts`; in 28 of 50 the chosen fact is one the
  benchmark annotates as inappropriate to share.
- Some PiSAs information types hold two facts. This mostly follows the data: PiSAs often puts a task fact and a
  private fact in the same artifact or sentence (e.g. "Quinn available hours: 4 / Quinn is distracted by an
  unresolved conflict with Nova"), so the prompt was not changed for it.
- The 5 subject disagreements in PiSAs are cases where the benchmark's subject is the case's person but the extracted
  fact is about their event or a facility (3), or the model left subject empty (2).
