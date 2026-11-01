"""The 3D Pipelines routes. Worker threads only."""

import urllib.parse

from ..core import http


class PipelineError(http.ApiError):
    """An API error that names the steps it is about: problems = [(node_id or None, message)]."""

    def __init__(self, error, problems):
        super().__init__(error.status, error.message, error.code, error.body)
        self.problems = problems


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except http.ApiError as error:
        found = (error.body or {}).get("problems")
        if isinstance(found, list) and found:
            problems = [(p.get("nodeId"), p.get("message") or "") for p in found if isinstance(p, dict)]
            raise PipelineError(error, problems) from None
        raise


# Recipes


def list_pipelines():
    return _call(http.get, "/pipelines") or []


def get_pipeline(pipeline_id):
    return _call(http.get, f"/pipelines/{int(pipeline_id)}")


def create_pipeline(title="Untitled pipeline", graph=None):
    return _call(http.post, "/pipelines", {"title": title, "graph": graph or {"version": 1, "nodes": [], "edges": []}})


def update_pipeline(pipeline_id, title=None, graph=None):
    body = {}
    if title is not None:
        body["title"] = title
    if graph is not None:
        body["graph"] = graph
    return _call(http.patch, f"/pipelines/{int(pipeline_id)}", body)


def duplicate_pipeline(pipeline_id):
    return _call(http.post, f"/pipelines/{int(pipeline_id)}/duplicate")


def delete_pipeline(pipeline_id):
    _call(http.delete, f"/pipelines/{int(pipeline_id)}")


def draft(description, graph=None):
    """"Describe it": {title, summary, graph} — nothing is saved."""
    body = {"description": description}
    if graph is not None:
        body["graph"] = graph
    return _call(http.post, "/pipelines/draft", body, timeout=240)


# Runs


def list_runs(pipeline_id):
    return _call(http.get, f"/pipelines/{int(pipeline_id)}/runs") or []


def batch(pipeline_id, mode, seeds, graph):
    """Creates one draft run per seed (they start with start_run). Returns {runs: [...]}."""
    return _call(http.post, f"/pipelines/{int(pipeline_id)}/batch",
                 {"mode": mode, "seeds": seeds, "graph": graph, "draft": True}, timeout=120)


def get_run(run_id):
    return _call(http.get, f"/pipeline-runs/{int(run_id)}")


def delete_run(run_id):
    _call(http.delete, f"/pipeline-runs/{int(run_id)}")


def run_action(run_id, action, body=None):
    """start | pause | resume | cancel | run-waiting | mode."""
    return _call(http.post, f"/pipeline-runs/{int(run_id)}/{action}", body)


def patch_run_node(run_id, node_id, request=None, settings=None):
    body = {}
    if request is not None:
        body["request"] = request
    if settings is not None:
        body["settings"] = settings
    return _call(http.patch, f"/pipeline-runs/{int(run_id)}/nodes/{urllib.parse.quote(node_id, safe='')}", body)


def run_step(run_id, step_id):
    return _call(http.post, f"/pipeline-runs/{int(run_id)}/steps/{int(step_id)}/run")


def retry_step(run_id, step_id, request=None, settings=None):
    body = {}
    if request is not None:
        body["request"] = request
    if settings is not None:
        body["settings"] = settings
    return _call(http.post, f"/pipeline-runs/{int(run_id)}/steps/{int(step_id)}/retry", body)


def choose_parts(run_id, step_id, keys, extra_parts):
    return _call(http.post, f"/pipeline-runs/{int(run_id)}/steps/{int(step_id)}/parts",
                 {"keys": list(keys)[:16], "extraParts": list(extra_parts)[:16]})
