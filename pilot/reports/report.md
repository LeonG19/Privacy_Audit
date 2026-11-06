# Pilot comparison report

Annotated records per dataset: confaide_tier2 0, privacylens 0, mpci_bench 0, airgapagent_r 0, pisas 0

## CI parameter accuracy (all datasets)

Strict accuracy (95% Wilson interval); partial verdicts count as wrong.

| system | sender | subject | recipient | information_type | transmission_principle | overall |
|---|---|---|---|---|---|---|
| vllm:llama-3.3-70b-instruct | – | – | – | – | – | – |

## Missing information

Invention = a value where the reference says the text states none. Miss = null where the text states one.

| system | sender inv / miss | subject inv / miss | recipient inv / miss | information_type inv / miss | transmission_principle inv / miss | records with unsupported evidence |
|---|---|---|---|---|---|---|
| vllm:llama-3.3-70b-instruct | – / – | – / – | – / – | – / – | – / – | 23/100 |

## By dataset

CI = overall strict accuracy over the five parameters; domain = strict (with accepted alternatives).

| dataset | vllm:llama-3.3-70b-instruct CI | vllm:llama-3.3-70b-instruct domain |
|---|---|---|
| airgapagent_r | – | – (–) |
| confaide_tier2 | – | – (–) |
| mpci_bench | – | – (–) |
| pisas | – | – (–) |
| privacylens | – | – (–) |

## Domain classification (all datasets)

| system | strict | with alternatives | n |
|---|---|---|---|
| vllm:llama-3.3-70b-instruct | – | – | 0 |

## Output validity

| system | dataset | valid | valid first try | clean (no warnings) | failures |
|---|---|---|---|---|---|
| vllm:llama-3.3-70b-instruct | airgapagent_r | 20/20 | 20 | 17 | – |
| vllm:llama-3.3-70b-instruct | confaide_tier2 | 20/20 | 20 | 16 | – |
| vllm:llama-3.3-70b-instruct | mpci_bench | 20/20 | 20 | 14 | – |
| vllm:llama-3.3-70b-instruct | pisas | 20/20 | 20 | 5 | – |
| vllm:llama-3.3-70b-instruct | privacylens | 20/20 | 20 | 10 | – |

## Time, tokens and cost

| system | dataset | prompt tok | output tok | mean latency s | busy s | rec/min | out tok/s | API $ | GPU-h | GPU util | peak mem GiB |
|---|---|---|---|---|---|---|---|---|---|---|---|
| vllm:llama-3.3-70b-instruct | airgapagent_r | 53,122 | 6,674 | 13.2 | 264 | 4.5 | 25 | – | 0.15 | 98% | 87.2 |
| vllm:llama-3.3-70b-instruct | confaide_tier2 | 52,763 | 7,976 | 15.8 | 317 | 3.8 | 25 | – | 0.18 | 98% | 87.2 |
| vllm:llama-3.3-70b-instruct | mpci_bench | 58,826 | 9,887 | 20.0 | 401 | 3.0 | 25 | – | 0.22 | 97% | 87.2 |
| vllm:llama-3.3-70b-instruct | pisas | 76,950 | 10,912 | 22.1 | 443 | 2.7 | 25 | – | – | –% | – |
| vllm:llama-3.3-70b-instruct | privacylens | 58,336 | 9,085 | 18.6 | 373 | 3.2 | 24 | – | 0.21 | 98% | 87.2 |

## Estimated cost of the full datasets

Per-subset means from the pilot × population, same concurrency. Small samples: treat as order-of-magnitude.

| system | records | prompt tok | output tok | wall h | API $ | GPU-h | GPU $ |
|---|---|---|---|---|---|---|---|
| vllm:llama-3.3-70b-instruct | 12,256 | 35,180,131 | 5,336,263 | 59.8 | – | – | – |
