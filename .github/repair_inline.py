"""検証で見つかった局所修正を、原 patch の同じ byte にだけ適用する。"""
from pathlib import Path

p=Path('SKM/backend/src/skillmind/agent/engine.py')
t=p.read_text()
needle='from collections.abc import '
assert t.count(needle)==1
t=t.replace(needle,needle+'Awaitable, ',1)
p.write_text(t)
p=Path('SKM/backend/src/skillmind/runs/repository_effects.py')
t=p.read_text()
start=t.index('    async def finalize_effect_execution(')
end=t.index('    async def _finish_effect_continuation(',start)
part=t[start:end]
for line in ('            outcome = "APPLIED"\n','            event_type = AgentEventType.EFFECT_APPLIED\n','            outcome = failure.status.value\n','            event_type = AgentEventType.EFFECT_FAILED\n'):
    assert part.count(line)==1,line
    part=part.replace(line,'',1)
t=t[:start]+part+t[end:]
p.write_text(t)
