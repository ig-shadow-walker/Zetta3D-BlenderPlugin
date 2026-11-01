"""Pictures of objects, rendered for the AI helpers (Auto Rig's body detection, Texture's reference).

Rendered with Workbench in a throwaway scene that links the objects, so the user's scene, camera and render
settings are never touched. Main thread only.
"""

import math
import os
import tempfile

import bpy
from mathutils import Vector


def _bounds(objects):
    corners = []
    for obj in objects:
        if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
            corners.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
    if not corners:
        return None, None
    low = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    high = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    return (low + high) / 2, max((high - low).length / 2, 0.01)


def shots(objects, yaws=(0, 90, 180, 270), pitch=15.0, fov=35.0, size=512, quality=82):
    """JPEG bytes of the objects seen from each yaw (degrees; 0 = from the front, -Y), framed to their bounds."""
    everything = set()
    for obj in objects:
        everything.add(obj)
        everything.update(obj.children_recursive)
    centre, radius = _bounds(everything)
    if centre is None:
        raise ValueError("There is nothing to look at — select a mesh.")

    scene = bpy.data.scenes.new("Zetta 3D capture")
    camera_data = bpy.data.cameras.new("Zetta 3D capture")
    camera = bpy.data.objects.new("Zetta 3D capture", camera_data)
    folder = tempfile.mkdtemp(prefix="zetta3d-capture-")
    results = []
    try:
        for obj in everything:
            scene.collection.objects.link(obj)
        scene.collection.objects.link(camera)
        scene.camera = camera
        camera_data.angle = math.radians(fov)
        camera_data.clip_end = radius * 100
        render = scene.render
        render.engine = "BLENDER_WORKBENCH"
        render.resolution_x = render.resolution_y = size
        render.resolution_percentage = 100
        render.film_transparent = False
        render.image_settings.file_format = "JPEG"
        render.image_settings.quality = quality
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "TEXTURE"
        distance = radius / math.sin(math.radians(fov) / 2) * 1.1
        p = math.radians(pitch)
        for index, yaw in enumerate(yaws):
            y = math.radians(yaw)
            offset = Vector((math.sin(y) * math.cos(p), -math.cos(y) * math.cos(p), math.sin(p))) * distance
            camera.location = centre + offset
            camera.rotation_euler = (centre - camera.location).to_track_quat("-Z", "Y").to_euler()
            render.filepath = os.path.join(folder, f"shot-{index}.jpg")
            bpy.ops.render.render(write_still=True, scene=scene.name)
            with open(render.filepath, "rb") as handle:
                results.append(handle.read())
    finally:
        bpy.data.objects.remove(camera)
        bpy.data.cameras.remove(camera_data)
        bpy.data.scenes.remove(scene)
    return results
