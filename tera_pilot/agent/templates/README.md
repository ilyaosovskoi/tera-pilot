# v2 templates directory

This directory contains prompt templates used by the v2 agent runtime
(`tera_pilot.agent.*`). Files here are read at runtime by the corresponding
modules; do not rename without updating the loaders.

## Files

- `intra_compaction_system.txt` — system prompt for intra-turn compaction.
- `intra_compaction_user.txt` — user prompt template for intra-turn compaction.
- `inter_compaction_system.txt` — system prompt for inter-turn chunked compaction.
- `code_compaction_system.txt` — system prompt for full-replace code compaction.
- `subagent_explore.md` — system prompt for the `explore` built-in sub-agent.
- `subagent_plan.md` — system prompt for the `plan` built-in sub-agent.
- `subagent_general_purpose.md` — system prompt for the `general-purpose` sub-agent.
- `interjection_frame.txt` — frame template for drained interjections.

These templates mirror the Grok Build `templates/` directory layout
(`crates/common/xai-grok-compaction/src/templates/` and
`crates/codegen/xai-grok-agent/templates/`).
