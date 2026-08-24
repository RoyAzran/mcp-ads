# Contributing

Thanks for looking under the hood. Ground rules that keep this repo shippable:

## Before a PR

```bash
python -m py_compile $(git ls-files '*.py')
python tests/test_no_private_imports.py
python scripts/check_tools_execute.py
python scripts/check_tool_collisions.py
```

CI runs these plus the rest of `scripts/check_*.py`. Those scripts are not
style checks — each one pins a failure that actually happened in production
(the docstrings tell the story). If your change trips one, read the script
before working around it.

## What makes a good change here

- **New actions**: follow the existing shape in the platform module — build the
  real upstream URL, call through the shared request path, return the raw JSON.
  Write tools must create entities paused and be named so `permissions.py`
  recognises them as writes (`*_create_*`, `*_update_*`, ...).
- **New platforms**: one module in `tools/`, credentials resolved through
  `credentials.provider()` — never read tokens from env inside a tool body.
- **Error messages**: say what to do next. "Not connected — add X to your
  .env" beats a stack trace.

## What we will decline

- Anything that weakens the boundary gate (`tests/test_no_private_imports.py`).
- Storing or logging credential values.
- Scraping endpoints beyond the (default-off) transparency family without a
  clearly documented opt-in.

## We cannot grant platform access

Google Ads developer tokens, Meta App Review, LinkedIn Marketing Developer
Platform — those approvals belong to the platforms. Issues asking for help
obtaining them will be pointed at the README's honesty table.
