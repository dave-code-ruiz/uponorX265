# Fix branch summary (`fix/ha-retry-reload-storm`)

This branch contains stability-focused changes compared to `main`.

## What changed

- Migrated Uponor HTTP calls to async `aiohttp`
- Added explicit timeouts and bounded retries
- Prevented overlapping update runs with an async lock
- Added reload cooldown to avoid reload storms during outages
- Added cached thermostat discovery metadata for startup resilience
- Added entity availability handling for cleaner outage/recovery behavior
- Updated config flow to use async client path
- Restored missing `get_model` / `get_version` methods in state proxy

## Why

To reduce Home Assistant resource spikes when the controller is unavailable,
improve startup behavior with many thermostats, and make entities recover more reliably after temporary network/controller failures.
