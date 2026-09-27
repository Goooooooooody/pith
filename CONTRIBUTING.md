# Contributing

Thanks for helping! pith is deliberately small: one package, standard library only, Python 3.8+.

- **Tests:** `python -m unittest discover -s tests` must pass. Add a test for every behaviour change.
- **Benchmark:** changes to parsing, grouping or rendering should not lower recall:
  `python bench/fetch.py && python bench/recall.py bench/cases.json`. Include before/after numbers
  in your PR. New log formats are very welcome - add a small anonymised fixture to the tests.
- **No dependencies.** If you need one, open an issue first.
- **Secrets:** never commit real logs that aren't already public. `bench/logs/` is git-ignored.
- **Style:** match the surrounding code; keep functions small and comments about *why*.

Bug reports are most useful with the command you ran, pith's output, and (if you can share it) the
input - `pith` saves every input under `~/.cache/pith/inputs`.
