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

- The Python adapter contains only archive creation, Books manifest creation and native invocation; unused upstream CLI, canary and device-selection flows were removed in rc.5. Operation directories and recovery journals are managed by the repository wrapper.
- Native calls require an exact device/model/version/build binding supplied by the wrapper through a child-process environment. There is no model/build allowlist and no experimental mode; unbound calls are rejected. `targetTested` is false, not a compatibility claim.
- The wrapper discovers the Belarus overlay through active SIM references; only direct Belarus overlay paths and validated country plists are accepted. No general-purpose write CLI is exposed.
- The adapter has no direct CLI entrypoint; repository wrapper provides authorization, exclusive locking, and durable operation logging.

Local three/four-asset catalog flows and process-interruption recovery were subsequently tested on the research phone; see ../../VALIDATION.md. Automatic discovery is not a compatibility guarantee. Native Books preservation covers the six tracked synchronization artifacts, not a complete device backup.

- The paused worker defaults to returning the original on stdin EOF/timeout. Four-asset mode chooses either original or staged candidate, never both; placement is independently read back by the Python wrapper.
