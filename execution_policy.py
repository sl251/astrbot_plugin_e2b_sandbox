"""Request-scoped execution guidance; the actual tools still enforce permissions."""

from copy import copy


LOCAL_EXECUTION_TOOLS = (
    "astrbot_execute_shell",
    "astrbot_execute_python",
    "astrbot_shell_session",
)
E2B_EXECUTION_TOOL = "e2b_sandbox_run_python_code"

LOCAL_RESTRICTED_PROMPT = """
[Execution permissions for this request]
The current user cannot execute host-local Shell, Python, or shell sessions.
Earlier general statements about access to the host execution environment do not
grant that permission. E2B cloud access is governed separately by the plugin's
user policy. Use only tools actually available in this request; do not invent a
tool or a sub-agent handoff. Do not infer that cloud execution is forbidden just
because a host-local tool requires an AstrBot administrator.
"""

E2B_DEFAULT_PROMPT = """
[Default code execution environment: E2B cloud sandbox]
The current user has passed the E2B plugin's user access policy. E2B execution
does not require AstrBot administrator status.
For general code execution, calculations, plotting, and processing user-provided
attachments, directly prefer e2b_sandbox_run_python_code. Do not first try host
execution or wait for the user to explicitly ask for a cloud sandbox. This tool
automatically creates or reuses the session sandbox; a separate create call is
normally unnecessary. Shell commands needed for these cloud tasks can be run
through Python subprocess inside E2B, subject to the same execution timeout.
E2B has its own files, processes, packages, network, and operating system. Host
paths and host Skills are not automatically present in E2B. Never present E2B
inspection results as the state of the AstrBot host or another target server.
For an explicit host/server task, use the authorized tools for that target or
explain the missing access; do not silently redirect the task to E2B. If the
target of an environment inspection is unclear, clarify which machine is meant.
An E2B service or interpreter failure is not evidence of missing AstrBot admin
permissions. Do not fall back to host execution to bypass a cloud failure.
Deliver generated files with the available E2B file tools; a sandbox path alone
is not a delivered attachment.
"""


def _has_active_tool(tool_set, name):
    getter = getattr(tool_set, "get_tool", None)
    tool = getter(name) if callable(getter) else None
    return tool is not None and bool(getattr(tool, "active", True))


def _local_execution_restricted(context, event, logger):
    # Never substitute global defaults for a missing conversation identity.
    origin = getattr(event, "unified_msg_origin", None)
    if not origin or not callable(getattr(context, "get_config", None)):
        return False
    try:
        settings = context.get_config(umo=origin).get("provider_settings", {})
        return (
            settings.get("computer_use_runtime") == "local"
            and bool(settings.get("computer_use_require_admin", True))
            and not event.is_admin()
        )
    except Exception as exc:
        # Older/third-party hosts retain their own tool permission checks.
        logger.warning(f"[E2B] Cannot determine host execution policy: {type(exc).__name__}")
        return False


def apply_execution_policy(context, event, request, config, logger):
    """Apply to this request only and report whether direct E2B execution exists."""
    tool_set = getattr(request, "func_tool", None)
    if _local_execution_restricted(context, event, logger):
        if config.get("hide_restricted_local_tools", True) is not False:
            if any(_has_active_tool(tool_set, name) for name in LOCAL_EXECUTION_TOOLS):
                try:
                    # Copy the container and its list, not plugin instances/locks.
                    filtered = copy(tool_set)
                    filtered.tools = list(tool_set.tools)
                    for name in LOCAL_EXECUTION_TOOLS:
                        filtered.remove_tool(name)
                    request.func_tool = tool_set = filtered
                except (AttributeError, TypeError) as exc:
                    logger.warning(f"[E2B] Cannot filter this host's tool set: {type(exc).__name__}")
        if LOCAL_RESTRICTED_PROMPT not in request.system_prompt:
            request.system_prompt += "\n" + LOCAL_RESTRICTED_PROMPT

    available = _has_active_tool(tool_set, E2B_EXECUTION_TOOL)
    if available and config.get("prefer_e2b_for_code", True) is not False:
        if E2B_DEFAULT_PROMPT not in request.system_prompt:
            request.system_prompt += "\n" + E2B_DEFAULT_PROMPT
    return available
