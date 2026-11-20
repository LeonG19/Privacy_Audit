"""Variation pipeline: swap ONE CI parameter of an existing sample for a same-domain value, then have an LLM
rewrite the original story so it expresses the new parameter set.

    knowledge.py  per-domain pools of roles / attributes / transmission principles (from the project's knowledge files)
    seeds.py      load original samples (text + extracted CI parameters + domain) and select a subset
    plan.py       pick the replacement values (deterministic given --seed); no LLM involved
    generate.py   prompts, LLM calls (vllm | openai | anthropic), output files, resume
"""
