# Protocol documents

| File | Status |
| --- | --- |
| [agent-messaging.md](agent-messaging.md) | **Authoritative** messaging and joint-task protocol, v1 |
| [formats.md](formats.md) | **Authoritative** file grammar, format 1 |
| [upstream/epesi-agent-messaging.md](upstream/epesi-agent-messaging.md) | Historical source, verbatim; not authoritative |

Protocol changes are made in `agent-messaging.md` / `formats.md` and recorded in
[../decisions.md](../decisions.md) in the same change. The upstream file is never edited.

## Upstream provenance

`upstream/epesi-agent-messaging.md` is a verbatim copy of the protocol first used for
multi-agent work in the EpesiCRM project (see
[../case-studies/epesi-field-report.md](../case-studies/epesi-field-report.md)).

| Source | Source commit | SHA-256 |
| --- | --- | --- |
| `bim.epe.si` checkout, `AI-shared/agent-messaging.md` | `e800d3e` (2026-10-16) | `8095934E4BFD53E4CE1DFFB05770BB9FF9E8B2FE2ADA55470AA69F28EFBC9F93` |

A second copy in the `epesi` checkout differs only in lacking the clarification that Codex
`send_input` reaches only agents in the same Codex-managed hierarchy. Links inside the
upstream file refer to that project and do not resolve here.

Main generalisations in v1: configurable state root (default `.multi-agent-ai/`) instead of
`storage/framework/agents/`; project-profile wording instead of Composer/npm and Premium
module rules; `plan-revision` on acks; explicit id and timestamp rules; dated corrections to
acks; registry marked as planned.
