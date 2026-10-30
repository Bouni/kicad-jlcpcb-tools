# Running tests

Install Docker and [`act`](https://nektosact.com/installation/), then start Docker.

```sh
python scripts/run_tests.py
```

Runs all seven lanes in `.github/workflows/tests.yml`: ordinary Python, native
wx controls, isolated input, and KiCad 7–10. Architecture follows the Docker
daemon, using AMD64 compatibility lanes where required.

Use `--architecture amd64` to force AMD64 throughout. Logs and artifacts are
saved in the printed results directory. Run with `--help` for output and cache
options.

Tests use tracked files with local edits; stage new files to include them.

See [native impedance checks](linux-impedance-testing.md) for the KiCad 10 smoke
workflow and isolated performance profiles.
