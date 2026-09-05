"""生成 FrontendModule の版 lifecycle、静的検査と依存白名单を提供する (計画 §24)。"""

from projectmind.modules.build_plan import (
    PUBLIC_REGISTRY_HOSTS,
    ModuleBuildPlan,
    ModuleBuildRefusedError,
    plan_module_build,
)
from projectmind.modules.domain import (
    ALLOWED_MODULE_TRANSITIONS,
    MODULE_API_VERSION,
    MODULE_CONTENT_SECURITY_POLICY,
    ModuleVersionError,
    ModuleVersionRecord,
    ModuleVersionStatus,
    plan_module_transition,
    require_publishable,
)
from projectmind.modules.static_analysis import (
    ALLOWED_DEPENDENCIES,
    FORBIDDEN_PACKAGE_SCRIPTS,
    ModuleFinding,
    ModuleSourceFile,
    ModuleStaticReport,
    analyze_module_sources,
    validate_dependencies,
)

__all__ = [
    "ALLOWED_DEPENDENCIES",
    "ALLOWED_MODULE_TRANSITIONS",
    "FORBIDDEN_PACKAGE_SCRIPTS",
    "MODULE_API_VERSION",
    "MODULE_CONTENT_SECURITY_POLICY",
    "PUBLIC_REGISTRY_HOSTS",
    "ModuleBuildPlan",
    "ModuleBuildRefusedError",
    "ModuleFinding",
    "ModuleSourceFile",
    "ModuleStaticReport",
    "ModuleVersionError",
    "ModuleVersionRecord",
    "ModuleVersionStatus",
    "analyze_module_sources",
    "plan_module_build",
    "plan_module_transition",
    "require_publishable",
    "validate_dependencies",
]
