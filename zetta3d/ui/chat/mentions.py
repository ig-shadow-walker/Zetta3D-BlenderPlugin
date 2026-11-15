"""What "@" can tag: the objects and data-blocks of the open file, matched against what follows the @.

A tag is a target the agent understands (agent/targets.py): an object's name, or "<kind>:<name>" for any other
data-block.
"""

import re

import bpy

# kind → (bpy.data collection, label shown in the list)
KINDS = (
    ("material", "materials", "Material"),
    ("collection", "collections", "Collection"),
    ("node_group", "node_groups", "Node group"),
    ("image", "images", "Image"),
    ("text", "texts", "Text"),
    ("action", "actions", "Action"),
    ("world", "worlds", "World"),
    ("camera", "cameras", "Camera data"),
    ("light", "lights", "Light data"),
)
LIMIT = 8
_TOKEN = re.compile(r"(?:(?<=\s)|^)@([^\s@]{1,48})")


def everything():
    """[(target, name, label)] — objects first, then the other data-blocks. Hidden ones (".name") are left out."""
    found = [(obj.name, obj.name, obj.type.replace("_", " ").capitalize()) for obj in bpy.data.objects]
    for kind, attr, label in KINDS:
        data = getattr(bpy.data, attr, None)
        if data is None:
            continue
        for block in data:
            if not block.name.startswith("."):
                found.append((f"{kind}:{block.name}", block.name, label))
    return found


def find(query, limit=LIMIT):
    """The best matches for what was typed after @: names that start with it first, then names that contain it,
    then names whose words start with it; objects before other data within each group."""
    q = query.strip().lower()
    if not q:
        return everything()[:limit]
    first, second, third = [], [], []
    for entry in everything():
        name = entry[1].lower()
        if name.startswith(q):
            first.append(entry)
        elif q in name:
            second.append(entry)
        elif any(word.startswith(q) for word in re.split(r"[\s._\-]+", name)):
            third.append(entry)
    return (first + second + third)[:limit]


def resolve_typed(text):
    """Targets for "@Name" words typed without picking from the list — exact names only (any case)."""
    targets = []
    if "@" not in text:
        return targets
    names = {}
    for target, name, _label in everything():
        names.setdefault(name.lower(), target)
    for match in _TOKEN.finditer(text):
        word = match.group(1).rstrip(".,;:!?)]}\"'")
        target = names.get(word.lower())
        if target and target not in targets:
            targets.append(target)
    return targets


def label(target):
    """What a tag chip says: the name, with the kind for data that is not an object."""
    kind, sep, name = target.partition(":")
    if sep and kind in {k for k, _a, _l in KINDS}:
        return name
    return target
