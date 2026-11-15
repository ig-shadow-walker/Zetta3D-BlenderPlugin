"""Values: the agent's JSON values in and out of Blender properties (BlenderAgentProtocol.md › values).

In: numbers, booleans, strings, enum identifiers, vectors [x, y, z], colours "#rrggbb" / "#rrggbbaa" (sRGB,
made linear for a linear colour property), data-block references {"id": "<target>"} (None clears one).

Every write goes through the property's own RNA definition — type, array length, hard range, enum items — so
a wrong value is refused with what the property takes, instead of failing deep inside Blender or doing
something else.

Out: `show(value)` writes a property's value the way the model writes it back.
"""

import math
import re

import bpy

from . import targets
from .tools import ToolError

_INDEXED = re.compile(r"^(?P<base>.*?)\[(?P<index>-?\d+)\]$")


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def colour(text, linear=True, size=4):
    """A "#rrggbb(aa)" colour as floats, made linear unless the property stores sRGB."""
    if not isinstance(text, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", text.strip()):
        raise ToolError(f'{text!r} is not a colour. Write "#rrggbb" or "#rrggbbaa".')
    raw = text.strip().lstrip("#")
    values = [int(raw[i:i + 2], 16) / 255 for i in range(0, len(raw), 2)]
    rgb = [srgb_to_linear(c) if linear else c for c in values[:3]]
    alpha = values[3] if len(values) == 4 else 1.0
    return (rgb + [alpha])[:size] if size >= 3 else rgb[:size]


def _enum_items(prop, owner=None):
    try:
        return [item.identifier for item in prop.enum_items]
    except (AttributeError, TypeError):
        return []


def convert(owner, identifier, value):
    """The Python value to assign to owner.<identifier>, checked against its RNA definition."""
    rna = owner.bl_rna.properties.get(identifier)
    if rna is None:
        raise ToolError(_unknown(owner, identifier))
    if rna.is_readonly and rna.type not in {"POINTER", "COLLECTION"}:
        raise ToolError(f"{identifier} is read-only.")
    kind = rna.type
    if kind == "POINTER":
        if value is None:
            return None
        if isinstance(value, dict) and "id" in value:
            block = targets.datablock(value["id"])
        elif isinstance(value, str):
            block = targets.datablock(value)
        else:
            raise ToolError(f'{identifier} takes a reference: {{"id": "<target>"}} or null.')
        # Blender checks the reference's type on assignment; assign() reports a mismatch.
        return block
    if kind == "COLLECTION":
        raise ToolError(f"{identifier} is a list. Change its items by path, e.g. {identifier}[0].<property>.")
    length = getattr(rna, "array_length", 0) or 0
    if length:
        subtype = getattr(rna, "subtype", "")
        if isinstance(value, str) and subtype in {"COLOR", "COLOR_GAMMA"}:
            return colour(value, linear=subtype == "COLOR", size=length)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = [value] * length
        if not isinstance(value, (list, tuple)) or len(value) != length:
            raise ToolError(f"{identifier} takes {length} values, like [{', '.join(['0'] * length)}].")
        return [_scalar(rna, identifier, item) for item in value]
    return _scalar(rna, identifier, value)


def _scalar(rna, identifier, value):
    kind = rna.type
    if kind == "BOOLEAN":
        if not isinstance(value, bool):
            raise ToolError(f"{identifier} is true or false.")
        return value
    if kind in {"INT", "FLOAT"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"{identifier} is a number.")
        if kind == "FLOAT" and getattr(rna, "subtype", "") == "ANGLE" and getattr(rna, "unit", "") == "ROTATION":
            pass  # angles go in as radians through set(); the tools that take degrees convert first
        low, high = rna.hard_min, rna.hard_max
        if value < low or value > high:
            raise ToolError(f"{identifier} must be between {_num(low)} and {_num(high)}; {value} is out of range.")
        return int(value) if kind == "INT" else float(value)
    if kind == "STRING":
        if not isinstance(value, str):
            raise ToolError(f"{identifier} is text.")
        return value
    if kind == "ENUM":
        items = _enum_items(rna)
        if getattr(rna, "is_enum_flag", False):
            values = value if isinstance(value, (list, set, tuple)) else [value]
            bad = [v for v in values if v not in items] if items else []
            if bad:
                raise ToolError(f"{identifier} takes {', '.join(items)}; not {', '.join(map(str, bad))}.")
            return set(values)
        if not isinstance(value, str):
            raise ToolError(f"{identifier} takes one of: {', '.join(items[:40])}.")
        if items and value not in items:
            close = [i for i in items if i.lower() == value.lower()]
            if close:
                return close[0]
            # A DYNAMIC ENUM (render.engine, a node's data types) lists only its fixed items in RNA, so a value
            # missing here may still be valid: Blender checks it on assignment, and its error names the real
            # choices (assign() reports it).
            return value
        return value
    raise ToolError(f"{identifier} cannot be set this way ({kind}).")


def _num(value):
    if abs(value) > 1e30:
        return "∞" if value > 0 else "-∞"
    return f"{value:g}"


def _unknown(owner, identifier):
    names = [p.identifier for p in owner.bl_rna.properties if p.identifier != "rna_type"]
    import difflib

    close = difflib.get_close_matches(identifier, names, n=5, cutoff=0.5)
    kind = owner.bl_rna.identifier
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"{kind} has no property {identifier}.{hint} (inspect or api_lookup lists what it has.)"


def _split_path(path):
    """(container path, property, index):
    'modifiers["Bevel"].width' → ('modifiers["Bevel"]', "width", None); "location[0]" → ("", "location", 0);
    '["custom"]' or 'data["custom"]' → a custom property ('', '["custom"]') / ('data', '["custom"]').

    A trailing ["…"] is a custom property when nothing, a "." or a "]" comes before it; after a name it is an
    item of that collection (modifiers["Bevel"]), which is not a value to set."""
    path = path.strip()
    if path.endswith('"]') or path.endswith("']"):
        start = max(path.rfind('["'), path.rfind("['"))
        if start == 0 or (start > 0 and path[start - 1] in ".]"):
            return path[:start].rstrip("."), path[start:], None
    head, _, tail = path.rpartition(".")
    match = _INDEXED.match(tail)
    if match and not tail.startswith("["):
        return head, match.group("base"), int(match.group("index"))
    return head, tail, None


def assign(owner, path, value):
    """Sets one property by data path relative to owner. Returns the value as Blender now has it."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("A property path is like \"location\" or \"modifiers[\\\"Bevel\\\"].width\".")
    head, attr, index = _split_path(path)
    try:
        container = owner.path_resolve(head, False) if head else owner
    except ValueError:
        raise ToolError(f"{targets.name_of(owner) if hasattr(owner, 'name') else owner} has nothing at {head}. "
                        "Check the path with inspect.") from None
    if attr.startswith("["):  # a custom property: ["name"]
        key = attr[2:-2]
        container[key] = value
        return container[key]
    if not hasattr(container, "bl_rna"):
        raise ToolError(f"{head} is a value, not something with properties.")
    if index is not None:
        rna = container.bl_rna.properties.get(attr)
        if rna is None:
            raise ToolError(_unknown(container, attr))
        current = getattr(container, attr)
        try:
            current[index] = _scalar(rna, f"{attr}[{index}]", value)
        except IndexError:
            raise ToolError(f"{attr} has {len(current)} values; there is no [{index}].") from None
        return getattr(container, attr)[index]
    converted = convert(container, attr, value)
    try:
        setattr(container, attr, converted)
    except (TypeError, ValueError, AttributeError) as error:
        raise ToolError(f"{attr}: {error}".replace("bpy_struct: item.attr = val: ", "")) from None
    return getattr(container, attr)


def show(value):
    """A property's value as the model writes it."""
    if isinstance(value, bpy.types.ID):
        return f'{{"id": "{targets.name_of(value)}"}}'
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(map(str, value))) + "}"
    try:
        items = list(value)
    except TypeError:
        return str(value)
    if len(items) > 16:
        return f"[{', '.join(show(v) for v in items[:16])}, … {len(items)} values]"
    return "[" + ", ".join(show(v) for v in items) + "]"


def degrees(values):
    """Euler degrees from the agent → radians for Blender."""
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ToolError("rotation is [x, y, z] in degrees.")
    return [math.radians(float(v)) for v in values]


def vector(values, name="value", size=3):
    if not isinstance(values, (list, tuple)) or len(values) != size or not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in values
    ):
        raise ToolError(f"{name} is {size} numbers, like [{', '.join(['0'] * size)}].")
    return [float(v) for v in values]


# Public names for the tool modules (the leading-underscore ones stay for older callers).
split_path = _split_path
unknown_property = _unknown
