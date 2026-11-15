"""App events for the agent: a job or a pipeline run the agent started has finished (BlenderAgentProtocol.md,
the `event` field — the same `job_finished` and `pipeline_run_finished` as Unity's).

The Zetta tools emit them when the work lands; the runtime listens, and sends each to the server once the
conversation is idle, so the model can carry on ("it finished and was imported to collection …").
"""

_listeners = []


def listen(callback):
    """callback(kind, payload) — on the main thread."""
    if callback not in _listeners:
        _listeners.append(callback)


def forget(callback):
    if callback in _listeners:
        _listeners.remove(callback)


def emit(kind, payload):
    """kind "job_finished": {generationId, status, importedPath}; "pipeline_run_finished": {runId, status,
    importedPaths}. Call on the main thread."""
    for callback in list(_listeners):
        callback(kind, dict(payload))
