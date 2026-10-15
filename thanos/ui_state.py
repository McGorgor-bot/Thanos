"""Helpers for keeping UI navigation state isolated between document modes."""


def resolve_active_module(context, previous_context, active_module, module_count):
    """Return a valid active module and current context.

    PDF and CAD modes expose different module lists. Reusing the same numeric
    index after switching modes can activate an unrelated module (for example,
    PDF preview index 3 becomes Excel→ATH index 3 in CAD mode). Reset to the
    first module whenever the mode changes, and guard against stale indices.
    """
    if previous_context != context:
        return 0, context
    if not isinstance(active_module, int) or isinstance(active_module, bool):
        return 0, context
    if module_count <= 0 or active_module not in range(module_count):
        return 0, context
    return active_module, context
