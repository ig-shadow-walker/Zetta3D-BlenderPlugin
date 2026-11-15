"""Colours and sizes for the agent's chat, taken from the user's Blender theme and interface scale, so the
drawn chat sits in the sidebar like Blender's own widgets — dark or light, at any resolution."""

import bpy


def _rgba(value, alpha=1.0):
    try:
        values = list(value)
    except TypeError:
        return (0.5, 0.5, 0.5, alpha)
    if len(values) == 3:
        values.append(alpha)
    return tuple(float(v) for v in values[:4])


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3)) + (a[3] + (b[3] - a[3]) * t,)


def fade(color, alpha):
    return color[:3] + (alpha,)


def luminance(color):
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


class Palette:
    """The colours one frame is drawn with."""

    def __init__(self):
        try:
            theme = bpy.context.preferences.themes[0]
            ui = theme.user_interface
            space = theme.view_3d.space
            bg = _rgba(space.button)
            text = _rgba(space.button_text)
            field = _rgba(ui.wcol_text.inner)
            field_text = _rgba(ui.wcol_text.text)
            button = _rgba(ui.wcol_regular.inner)
            button_text = _rgba(ui.wcol_regular.text)
            accent = _rgba(ui.wcol_regular.inner_sel)
            accent_text = _rgba(ui.wcol_regular.text_sel)
            menu = _rgba(ui.wcol_menu_back.inner)
        except (AttributeError, IndexError, TypeError):
            bg, text = (0.19, 0.19, 0.19, 1.0), (0.9, 0.9, 0.9, 1.0)
            field, field_text = (0.11, 0.11, 0.11, 1.0), (0.9, 0.9, 0.9, 1.0)
            button, button_text = (0.33, 0.33, 0.33, 1.0), (0.9, 0.9, 0.9, 1.0)
            accent, accent_text = (0.28, 0.45, 0.70, 1.0), (1.0, 1.0, 1.0, 1.0)
            menu = (0.09, 0.09, 0.09, 1.0)
        self.bg = fade(bg, 1.0)  # the sidebar may be see-through over the viewport; the chat never is
        self.dark = luminance(self.bg) < 0.5
        self.text = fade(text, 1.0)
        self.muted = mix(self.text, self.bg, 0.42)
        self.faint = mix(self.text, self.bg, 0.75)
        self.card = mix(self.bg, self.text, 0.06)
        self.card_line = mix(self.bg, self.text, 0.14)
        self.field = fade(field, 1.0)
        self.field_text = fade(field_text, 1.0)
        self.field_line = mix(self.field, self.text, 0.18)
        self.button = fade(button, 1.0)
        self.button_hover = mix(self.button, self.text, 0.12)
        self.button_text = fade(button_text, 1.0)
        self.accent = fade(accent, 1.0)
        self.accent_hover = mix(self.accent, (1, 1, 1, 1), 0.12)
        self.accent_text = fade(accent_text, 1.0)
        self.user = mix(self.bg, self.accent, 0.22)
        self.selection = fade(self.accent, 0.45)
        self.hover = fade(self.text, 0.07)
        self.menu = fade(menu, 1.0)
        self.danger = (0.86, 0.33, 0.33, 1.0)
        self.danger_bg = mix(self.bg, self.danger, 0.18)
        self.success = (0.42, 0.78, 0.47, 1.0) if self.dark else (0.15, 0.55, 0.25, 1.0)
        self.warning = (0.93, 0.70, 0.30, 1.0) if self.dark else (0.62, 0.42, 0.05, 1.0)
        self.code = mix(self.bg, (0, 0, 0, 1), 0.25) if self.dark else mix(self.bg, (0, 0, 0, 1), 0.06)


class Sizes:
    """Lengths in region pixels at the user's interface scale."""

    def __init__(self):
        try:
            system = bpy.context.preferences.system
            self.scale = float(system.ui_scale) or 1.0
            self.pixel = max(1, int(system.pixel_size))
        except AttributeError:
            self.scale, self.pixel = 1.0, 1
        s = self.scale
        self.font = round(11.0 * s)
        self.small = round(10.0 * s)
        self.title = round(12.0 * s)
        self.line = round(self.font * 1.5)
        self.small_line = round(self.small * 1.45)
        self.pad = round(8 * s)
        self.gap = round(6 * s)
        self.button = round(22 * s)
        self.radius = round(5 * s)
        self.widget_unit = int(round(18 * s)) + 2 * self.pixel
        self.scrollbar = max(2, round(3 * s))

    def __call__(self, value):
        return round(value * self.scale)
