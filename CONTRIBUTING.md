# Contributing

This is a personal tool. Outside contributions are welcome, but keep them small and focused.

## Setup

```bash
./install.sh
.venv/bin/pip install -e ".[test]"
```

`install.sh` builds `.venv`; the second command adds `pytest` and `pytest-asyncio` on top of it
so you can run the test suite from the same environment `droid` uses.

## Running the checks

```bash
tools/verify/verify.sh
```

This is the same script CI runs. It refuses to proceed if `pytest` and `pytest-asyncio` are not
installed in the interpreter it picks (`.venv/bin/python` by default, or `$DROID_PY`), and it runs
the full test suite.

## Fixtures

Tests run against anonymized captures of real `adb` output, not live devices. See
[`tests/README.md`](tests/README.md) for how a fixture is captured under `tests/fixtures/raw/`,
anonymized with `python -m tests.anonymize`, and independently re-checked before it can be
committed. If you add a new fixture, read that file first.

## Commit style

- One change per commit. A commit that mixes an unrelated cleanup with a feature is harder to
  review and harder to revert.
- Write the message in English and explain why the change is needed, not just what changed.
- Each commit should leave the tree green on its own (`tools/verify/verify.sh` passes), not only
  after later commits in the same branch.
- Do not add AI attribution lines (for example `Co-Authored-By` referencing an AI tool, or a
  "Generated with" footer) to commits or pull request descriptions submitted to this project.
