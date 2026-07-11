---
name: Bug report
about: Something isn't working the way you expect
title: "[bug] "
labels: bug
assignees: ''
---

## What happened

<!-- A clear description of the bug. -->

## What you expected

<!-- What you thought would happen instead. -->

## Steps to reproduce

<!-- Ideally a minimal snippet. The smaller, the faster we can help. -->

```python
from indratrace import init_observability
# ...
```

## Environment

- **IndraTrace SDK version:** <!-- python -c "import indratrace; print(indratrace.__version__)" -->
- **Python version:** <!-- python --version -->
- **OS:**
- **Relevant extras installed:** <!-- e.g. fastapi, anthropic, claude-agent-sdk -->

## `debug=True` output

Re-run with diagnostics on and paste the console output — this is usually the
fastest way for us to see what went wrong:

```python
init_observability(product="...", debug=True)   # or set INDRATRACE_DEBUG=1
```

```
<!-- paste the banner + any export-failure / skipped lines here -->
```

## Anything else

<!-- Logs, screenshots, or context that might help. -->
