# Candidate field rules

- Emit one native JSON object, without Markdown or a JSON string envelope.
- `candidate_version` is `skillmind.skill-candidate/v2`.
- Fill every property required by the supplied native schema. Use null only where allowed;
  use empty arrays for absent declarations. Resource capabilities must be nonempty.
- Source references use the supplied source ID, optionally followed by a 1-based line:
  `s0` or `s0:12`. Never invent file IDs or line numbers. Input and resource declarations
  require a source reference. Diagnostics may use null when not source-specific.
- `operations` contains `{capability_version, operation}` pairs on that resource. Use the exact
  registered names. Reads have no write operations; distinct operations have separate entries.
- The platform owns task keys, source copies, hashes, identity, workflow, result envelope,
  report rendering and authorization. Do not generate these fields.
- This contract describes execution preparation. The frozen Skill remains the business
  instruction; platform resource bindings and actual Tool checks determine authority.
