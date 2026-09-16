---
name: skillmind-skill-interpreter
description: Identify caller inputs and resource requirements for executing a frozen Skill directly.
version: 6.0.0
---
# Skillmind Skill Interpreter

Read the numbered frozen sources. Return one `skillmind.skill-candidate/v2` matching the supplied
schema; do not execute the Skill. The platform creates one task and supplies the complete original
Skill and references to the execution Agent. Do not rewrite business procedures, rules, output
schemas, success criteria, approval points or recovery instructions into a second execution plan.

## Inputs

Use a concise task title and description in the source language, or null to retain source metadata.
Declare only values the caller must choose. Document selection and resource binding already supply
paths, document IDs, library IDs, bucket and connection metadata; do not ask for these again.
Keep exact caller field names, types, enums and defaults where the input contract supports them.
Use an empty object contract when no caller input is needed. Internal IDs and generated values
are runtime context, not inputs. Reference the source section with `input_source_ref`.

## Resources

Select registered capabilities and supported Providers from the supplied catalog. Group targets
sharing a connection and authorization purpose into one resource; multiple tables do not imply
multiple connections. Keep document inputs and artifact destinations separate. Selection guidance
should only help choose the correct resource. Exact table names, columns, paths, conditions and
processing order remain in the original source, which the Agent receives in full.

For external writes, declare the resource capability and each required operation from
`write_operations`. The resource must be required and write-access, even for conditional writes;
its availability does not approve or force a write. Read-only resources have no operations.
Platform approvals and the Run's automatic approval option govern actual proposals at runtime.
Source commands and bundled scripts do not grant execution permission.

`platform_tools` contains only needed task-local capabilities: workspace read/search/write,
JSON Schema validation, interaction or subagent dispatch. External Tools and `change.propose/v1`
are derived from resource declarations. Use `workspace.write/v2` for published output files.
Select required document conversion explicitly; metadata is not document content.

## Gaps and repair

Report an error only for a real unsupported requirement or missing essential caller decision.
A connection not yet bound is resolved by platform readiness, not an invented source defect.
Do not invent a resource, permission or unsupported workaround. Optional requirements remain
optional unless an external write needs its target bound. Never copy credentials into the candidate.
On validation feedback, correct the identified declaration errors and return the complete candidate.
See [field rules](references/output-contract.md) for source references and field ownership.
