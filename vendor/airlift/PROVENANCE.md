# AirLift source provenance

Upstream: https://github.com/0xjohnnydev/airlift

Reviewed source commit recorded by the original experiment:
`c75b3ea50041c5531d6823674c098f71c94cec52`.

The two Objective-C sources matched the recorded upstream SHA-256 values before local changes:

- `airtraffic_host.m`: `2d1423ee37aa77b5434c59fb11a8de69915e8d321b33eea2b0285bcffcbafd34`
- `device_helper.m`: `b45c9f46a9c76ea60acf82064dd6aab4fd63b045b3c0ae3a1dd74b41cae70c9c`

MIT license and copyright notice are retained in LICENSE. No proprietary bytecode is included.

Local changes:

- Device discovery callbacks are disabled under a mutex before global references are released; JSON output is flushed. This addresses an observed exit-time callback crash.
- AirTraffic supports an explicit host pause after the export, retaining one sync session through return.

- AirTraffic helper accepts one asset pair for returning an exported directory (previous argument-count check required two). The initial separate-sync return failed validation and was replaced with a single-session pause/continue flow.

- Python work directories persist on disk on success and failure (carried forward from the experiment).
- Native target gate rejects all models/builds except iPhone18,2 / 27.2 / 24B5084k.
- Python target is restricted to `/var/mobile/Library/Caches` for a generated canary only.
- Direct Python entrypoint is disabled; repository wrapper provides authorization, exclusive locking, and durable operation logging.

Local three/four-asset catalog flows and process-interruption recovery were subsequently tested on the research phone; see ../../VALIDATION.md. The build allowlist is a research scope, not a compatibility guarantee. Native Books preservation covers the six tracked synchronization artifacts, not a complete device backup.

- The paused worker defaults to returning the original on stdin EOF/timeout. Four-asset mode chooses either original or staged candidate, never both; placement is independently read back by the Python wrapper.
