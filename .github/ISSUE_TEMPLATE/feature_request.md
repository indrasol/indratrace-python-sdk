---
name: Feature request
about: Suggest an idea or improvement for the SDK
title: "[feature] "
labels: enhancement
assignees: ''
---

## The problem

<!-- What are you trying to do that the SDK makes hard or impossible today? -->

## The idea

<!-- What you'd like to see. A rough API sketch helps if you have one. -->

```python
# optional: how you imagine calling it
```

## Alternatives you've considered

<!-- Workarounds you're using now, or other approaches you thought about. -->

## Scope check

The SDK is intentionally a **thin OpenTelemetry wrapper**: it produces telemetry
and stays out of policy (no redaction/sampling/routing — that's the Collector's
job) and does no cost math. If your idea touches those areas, note it here so we
can figure out whether it belongs in the SDK or the platform.

## Anything else

<!-- Links, prior art, context. -->
