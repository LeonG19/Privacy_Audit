# TEST FIXTURES - INVENTED TEXT, NOT BENCHMARK DATA

Everything generated for tests (see `conftest.py`) is invented and every scenario starts with `[TEST FIXTURE]`.
No real benchmark item, rating, or label is copied into the test fixtures. Tests marked `real_data` use the
genuinely downloaded files under `data/raw/` and are skipped when those are absent.
