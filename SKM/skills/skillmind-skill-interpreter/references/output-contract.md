# Output contract rules

These rules specify candidate shape; interpretation decisions are defined in SKILL.md.

## Platform-owned fields

- `runtime_manifest_draft.capability_blueprint` is the primary product. Do not emit its `identity`
  or `compatibility`: Skillmind binds them to the Manifest and frozen request.
- Bind Manifest `source_hash` to the request source and `interpreter_version` to the supplied
  system Skill identity. Do not emit `source_documents`; the platform attaches them separately.
- Skillmind compiles task contracts into executable Schema and checksums. Do not emit JSON
  Schema, `$ref`, executable expressions or compiler checksums.
- Use the supplied versioned response contract. RuntimeManifest candidates remain subject to
  deterministic Schema, reference, permission, Tool and publish validation.

Blueprint `source_traces[].target` is a JSON Pointer relative to the Blueprint itself
(for example `/tasks/0` or `/guidance/required_rules/0`) and must resolve to an existing value.
Put Manifest Tool/workflow evidence in `report.source_traces` (for example `/tools/0`),
not Blueprint traces.

## Task contracts

Every task has a bounded `input_contract` for caller-supplied parameters and
`contract_source_trace` entries for material fields. Do not duplicate bound document contents,
selected paths, internal IDs, processing state or report payloads as form fields. No caller
parameters means an empty object contract.

Add `output_contract` only for stable machine-consumable business fields explicitly defined
by the source. Open reports, Markdown and artifact delivery use the platform's generic
OutcomeEnvelope. Missing output contracts, ViewSpec or fixtures do not lower compatibility.

Add enum, pattern, length or numeric bounds only when the source gives their exact constraints;
catalog Provider names are not business enum evidence. Use conservative types and diagnostics
for unresolved fields rather than inventing restrictions.

Every TaskContractDraft has a maximum nesting depth of 5. Count the root as depth 1; each field
and each array `items` node adds 1, including scalar leaves. Count independently for
`input_contract`, `output_contract`, `parameter_contract` and `result_contract`; Manifest and
Blueprint wrappers do not count. For example: root object (1), array field (2), object items
(3), array field (4), scalar items (5).

A required deep JSON file can remain an exact Artifact deliverable with its source-backed
rules in guidance. Do not truncate, rename or serialize its structure to fit an optional task
contract. If a required caller input cannot be represented faithfully within the depth limit,
record the limitation as an `assisted` diagnostic.

## Workflow shape

Create exactly one workflow per immediate task. Its stages are one `tool` step for each declared
RuntimeManifest Tool in array order, followed by one `agent`, one `validator` and one `artifact`
step. This execution scaffold does not replace the source's business order or conditions.

Set `execution_preferences.recommended_profile` only when the source justifies it; otherwise
omit it and use the platform default. Explanatory confidence, assumptions, questions and unmapped
references are optional. There is no separate `data_sources` list: the Blueprint requirements
are the resources reviewed and bound for the Run.
