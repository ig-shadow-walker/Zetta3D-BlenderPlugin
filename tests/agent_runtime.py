"""The agent's conversation loop, against a scripted server: no network, real Blender.

Each test scripts the events the server would stream for each request, then checks what Blender did and
what it sent back — the turn lifecycle of BlenderAgentProtocol.md.
"""

import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(__file__))
from agent_common import fresh, module, run_checks  # noqa: E402

runtime = module("agent.runtime")
client = module("agent.client")
tools = module("agent.tools")
changes = module("agent.changes")
events = module("agent.events")
tasks = module("core.tasks")
auth = module("auth")

# ─── A scripted server ───────────────────────────────────────────────────────────────────────────

sent = []  # every request body, in order
script = []  # per request: the events to stream back


def fake_chat(body, on_event, provider_key=None, token=None):
    sent.append(body)
    for event in script.pop(0) if script else [{"type": "done", "tokens": 1, "awaiting": []}]:
        on_event(event)


def sync_run(work, done=None, fail=None, token=None, name=None):
    try:
        result = work(token) if token is not None else work()
    except Exception as error:  # noqa: BLE001
        if fail:
            fail(error)
        return
    if done:
        done(result)


client.chat = fake_chat
client.stop = lambda session_id: None
tasks.run = sync_run
tasks.post = lambda callback, *args: callback(*args)
auth.refresh = lambda: None
auth.current = auth.SIGNED_IN
auth.user = {"id": 1, "email": "demo@example.com", "paymentPlan": "pro",
             "credits": {"threeD": {"monthly": 100, "purchased": 0, "bonusMonthly": 0, "bonus": 0}}}

ran = []


@tools.tool("test_add_cube", undoable=True)
def _add_cube(args, ctx):
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.active_object.name = args.get("name", "Box")
    ran.append(("add", args.get("name")))
    return tools.Result(f"Added {bpy.context.active_object.name}.", label="Added a cube")


@tools.tool("test_delete", risky=True, undoable=True)
def _delete(args, ctx):
    ran.append(("delete", args.get("name")))
    bpy.data.objects.remove(bpy.data.objects[args["name"]])
    return "Deleted."


def reset():
    fresh()
    sent.clear()
    script.clear()
    ran.clear()
    runtime.new_chat()
    runtime.conversation["halted"] = False
    props = bpy.context.window_manager.zetta_agent
    props.approval = "confirm_destructive"
    props.mode = "agent"


def client_tool(tool_id, tool_name, effect="write", **args):
    return {"type": "client_tool", "toolUseId": tool_id, "name": tool_name, "input": args, "effect": effect}


# ─── Checks ──────────────────────────────────────────────────────────────────────────────────────


def test_a_turn_runs_blenders_tools_and_resumes():
    reset()
    script.extend([
        [{"type": "session", "sessionId": 42}, {"type": "route", "route": {"label": "Capable"}},
         {"type": "text", "text": "Adding two boxes.", "start": True},
         {"type": "tool", "name": "test_add_cube", "toolUseId": "t1", "side": "client"},
         client_tool("t1", "test_add_cube", name="Crate"),
         {"type": "tool", "name": "test_add_cube", "toolUseId": "t2", "side": "client"},
         client_tool("t2", "test_add_cube", name="Barrel"),
         {"type": "done", "tokens": 10, "awaiting": ["t1", "t2"]}],
        [{"type": "session", "sessionId": 42}, {"type": "text", "text": "Done — two boxes.", "start": True},
         {"type": "done", "tokens": 5, "awaiting": []}],
    ])
    assert runtime.send("add a crate and a barrel") is None
    assert ran == [("add", "Crate"), ("add", "Barrel")], ran
    assert {"Crate", "Barrel"} <= set(bpy.data.objects.keys())
    first, second = sent
    assert first["message"] == "add a crate and a barrel"
    assert first["editor"]["blenderVersion"].startswith(bpy.app.version_string.split(" ")[0])
    assert "sessionId" not in first and second["sessionId"] == 42
    results = second["toolResults"]
    assert [r["toolUseId"] for r in results] == ["t1", "t2"]
    assert results[0]["content"] == "Added Crate." and not results[0]["isError"]
    assert runtime.phase() == runtime.IDLE
    texts = [i["text"] for i in runtime.items() if i["role"] == "assistant"]
    assert texts == ["Adding two boxes.", "Done — two boxes."], texts
    steps = [i for i in runtime.items() if i["role"] == "step"]
    assert [s["status"] for s in steps] == ["done", "done"] and steps[0]["label"] == "Added a cube"
    assert changes.current()["steps"] == 2  # both on the changes card


def test_the_project_id_lives_in_the_file():
    reset()
    first = runtime.project_id()
    assert len(first) == 32 and bpy.data.scenes[0]["zetta_project_id"] == first
    assert runtime.project_id() == first


def test_a_destructive_call_waits_for_the_card():
    reset()
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.active_object.name = "Old"
    script.extend([
        [{"type": "session", "sessionId": 7}, client_tool("d1", "test_delete", "destructive", name="Old"),
         {"type": "done", "tokens": 1, "awaiting": ["d1"]}],
        [{"type": "done", "tokens": 1, "awaiting": []}],
    ])
    runtime.send("delete the old cube")
    assert runtime.phase() == runtime.WAITING and runtime.card["kind"] == "approve"
    assert ran == [] and "Old" in bpy.data.objects  # nothing ran before the OK
    # An unsaved file cannot be copied: the card says so instead of offering it ticked.
    assert runtime.card["payload"]["copy"] and not runtime.card["payload"]["copy_ticked"]
    runtime.answer_card({"allow": True, "copy": False})
    assert ran == [("delete", "Old")] and "Old" not in bpy.data.objects
    assert sent[-1]["toolResults"][0]["content"] == "Deleted."


def test_a_declined_call_says_so():
    reset()
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.active_object.name = "Keep"
    script.extend([
        [{"type": "session", "sessionId": 8}, client_tool("d2", "test_delete", "destructive", name="Keep"),
         {"type": "done", "tokens": 1, "awaiting": ["d2"]}],
        [{"type": "done", "tokens": 1, "awaiting": []}],
    ])
    runtime.send("delete it")
    runtime.answer_card({"allow": False})
    assert "Keep" in bpy.data.objects and ran == []
    result = sent[-1]["toolResults"][0]
    assert result["isError"] and result["content"] == "The user declined."


def test_full_autonomy_runs_destructive_calls_and_keeps_a_copy():
    reset()
    folder = bpy.app.tempdir or "/tmp"
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, "autonomy-test.blend"))
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.active_object.name = "Gone"
    bpy.context.window_manager.zetta_agent.approval = "auto"
    script.extend([
        [{"type": "session", "sessionId": 9}, client_tool("d3", "test_delete", "destructive", name="Gone"),
         {"type": "done", "tokens": 1, "awaiting": ["d3"]}],
        [{"type": "done", "tokens": 1, "awaiting": []}],
    ])
    runtime.send("clear it out")
    assert runtime.card is None and ran == [("delete", "Gone")]
    turn = changes.current()
    assert turn["copy"] and os.path.isfile(turn["copy"]), turn  # the risky call kept a copy first
    assert "A copy of the file was kept" in sent[-1]["toolResults"][0]["content"]


def test_the_server_review_insists_even_in_full_autonomy():
    reset()
    bpy.context.window_manager.zetta_agent.approval = "auto"
    call = client_tool("w1", "test_add_cube", name="Flagged")
    call["review"] = {"confirm": True, "reasons": ["Touches objects the user did not mention"]}
    script.extend([[{"type": "session", "sessionId": 10}, call, {"type": "done", "tokens": 1, "awaiting": ["w1"]}]])
    runtime.send("tidy up")
    assert runtime.card is not None and runtime.card["payload"]["reasons"]
    runtime.stop()


def test_ask_user_waits_and_answers_by_id():
    reset()
    script.extend([
        [{"type": "session", "sessionId": 11},
         client_tool("q1", "ask_user", "read", title="Pick the bottle",
                     questions=[{"id": "bottle", "question": "Which bottle?", "kind": "object"},
                                {"id": "finish", "question": "Glass or plastic?", "kind": "choice",
                                 "options": [{"label": "Glass"}, {"label": "Plastic"}]}]),
         {"type": "done", "tokens": 1, "awaiting": ["q1"]}],
        [{"type": "done", "tokens": 1, "awaiting": []}],
    ])
    runtime.send("put the logo on the bottle")
    assert runtime.card["kind"] == "ask_user"
    runtime.answer_card({"bottle": ["Bottle_B"], "finish": "Glass"})
    text = sent[-1]["toolResults"][0]["content"]
    assert 'bottle — "Which bottle?": Bottle_B — target it as "Bottle_B"' in text, text
    assert 'chose "Glass"' in text


def test_stop_ends_everything():
    reset()
    script.extend([[{"type": "session", "sessionId": 12},
                    client_tool("s1", "test_delete", "destructive", name="X"),
                    {"type": "done", "tokens": 1, "awaiting": ["s1"]}]])
    runtime.send("delete x")
    assert runtime.card is not None
    runtime.stop()
    assert runtime.card is None and runtime.phase() == runtime.IDLE and runtime.conversation["halted"]
    count = len(sent)
    events.emit("job_finished", {"generationId": 5, "status": "success", "importedPath": 'collection "Zetta 3D/5 X"'})
    assert len(sent) == count  # nothing resumes after Stop until a new message


def test_a_finished_job_wakes_the_conversation():
    reset()
    script.extend([[{"type": "session", "sessionId": 13}, {"type": "done", "tokens": 1, "awaiting": []}],
                   [{"type": "done", "tokens": 1, "awaiting": []}]])
    runtime.send("make a helmet")
    events.emit("job_finished", {"generationId": 501, "status": "success",
                                 "importedPath": 'collection "Zetta 3D/501 Helmet"'})
    event = sent[-1]["event"]
    assert event["kind"] == "job_finished" and event["generationId"] == 501, sent[-1]
    assert "message" not in sent[-1]


def test_mentions_are_addresses():
    reset()
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.active_object.name = "Chair"
    bpy.data.materials.new("Oak")
    script.append([{"type": "session", "sessionId": 14}, {"type": "done", "tokens": 1, "awaiting": []}])
    runtime.send("make this wood", mentions=["Chair", "material:Oak"])
    message = sent[-1]["message"]
    assert '- object "Chair" (MESH, collection "Collection") — target it as "Chair"' in message or \
           '- object "Chair" (MESH' in message, message
    assert '- material "Oak" — target it as "material:Oak"' in message, message


def test_errors_show_with_their_code():
    reset()
    script.append([{"type": "session", "sessionId": 15},
                   {"type": "error", "message": "Your OpenAI key was refused.", "code": "provider_key_invalid"}])
    runtime.send("hello")
    error = runtime.items()[-1]
    assert error["role"] == "error" and error["code"] == "provider_key_invalid"
    assert runtime.phase() == runtime.IDLE


run_checks(globals())
