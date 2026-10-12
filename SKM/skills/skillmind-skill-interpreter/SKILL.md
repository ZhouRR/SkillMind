---
name: skillmind-skill-interpreter
description: Identify caller inputs and resources for executing a frozen Skill directly.
version: 7.0.0
---
# Skillmind Skill Interpreter

Read the numbered frozen sources and return one `skillmind.skill-candidate/v3` matching the supplied
schema. Do not execute the Skill or rewrite its procedures, rules, outputs or recovery into a plan.
The execution Agent receives the complete frozen source and dependencies.

## Inputs

Use a concise title and description in the source language, or null to retain source metadata.
Declare only values the caller must choose. Document selection and resource binding supply paths,
document IDs, library IDs, bucket and connection metadata. Internal/generated IDs and runtime
settings are not caller inputs. Keep exact field names, types, enums and defaults where supported;
use an empty object contract when no caller input is needed. Reference `input_source_ref`.

## Resources

Identify connections the caller needs to select from the supplied resource kinds and Providers.
Group tables or endpoints sharing a connection into one resource. Keep distinct connection purposes
separate. Use `selection: documents` only for documents that define the task's input scope;
use `selection: resource` for a connection or project document library. This does not grant or
restrict read/write permission. Project document reference reading is already supplied; an optional
knowledge library in that project does not need another required connection.

Declare a resource required only when the task cannot proceed without it. Optional uses remain
optional. Provider hints and selection guidance help choose the correct resource. Do not generate
capabilities, tool lists, operations or approval policies. The platform supplies registered local
Tools, conversion and Schema validation, and derives external operations from selected resources.
Actual permissions and the Run's automatic approval settings govern access and effects.

## Gaps and repair

Report an error only for a genuine missing essential input or unsupported resource type.
An unbound connection is resolved at task launch, not an invented Skill defect. Do not turn an
optional tool or evidence limitation into a universal start condition. Never copy credentials.
On validation feedback, repair the identified declaration and return the complete candidate.
See [field rules](references/output-contract.md) for source references and field ownership.
