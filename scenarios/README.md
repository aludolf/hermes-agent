# Test Scenarios (021 Hermes Intelligence Layer)

This directory holds bot-to-bot test scenarios that Hermes runs against target
bots via the harness runner.

**One scenario per YAML file.** Filename must match the `id` field.

See the canonical schema at
[specs/021-hermes-intelligence-layer/contracts/scenario-yaml-contract.md]
for the full contract, validation rules, and runtime semantics.

## Quick reference

```yaml
id: rauru_smoke                              # required, unique
name: "Rauru onboarding + credits smoke test"
category: smoke                              # smoke | regression | edge
target_bot: Rauru_HD_bot
target_chat_id: "-1003992792803"
timeout_per_step_ms: 15000
steps:
  - step: 1
    sent: "/start@Rauru_HD_bot"
    expected_pattern: "Welcome to Rauru"
    match_type: substring                    # substring (default) | regex | exact
    gate: true
  - step: 2
    sent: "English"
    expected_pattern: "date of birth"
```

## Adding a new scenario

1. Drop a new `.yaml` file in this directory.
2. Rebuild + redeploy the Hermes image (scenarios are baked in at build time,
   re-loaded at gateway startup).
3. Verify with `/scenarios` in Telegram.

Invalid scenarios are skipped at load time without blocking others; check the
gateway logs for parse/validation errors.
