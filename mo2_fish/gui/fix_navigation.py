"""Preflight-to-editor routing and transient hints; never performs validation."""
from dataclasses import dataclass
from pathlib import Path
import re

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPen, QTextCursor
from PySide6.QtWidgets import QStyledItemDelegate, QTreeWidgetItem

from gui.field_help import HELP, HelpButton, ROLE_HELP, help_text
from gui.video_teacher import ROLES


def fields(data, prefix=""):
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            yield from fields(value, name)
        else:
            yield name, value


def yaml_line(text, key):
    wanted = re.sub(r"\[\d+\]", "", key).split(".")
    stack = []
    for number, line in enumerate(text.splitlines()):
        match = re.match(r"^(\s*)([\w-]+)\s*:", line)
        if not match:
            continue
        indent, name = len(match[1]), match[2]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, name))
        if [part for _, part in stack] == wanted:
            return number
    return None


class RoleHintDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        role = self.parent().property("fixRole")
        if role and index.column() == 0 and index.data(Qt.ItemDataRole.UserRole) == role:
            painter.save()
            painter.setPen(QPen(QColor("#ff555f"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(option.rect.adjusted(1, 1, -1, -1))
            painter.restore()


class FieldHighlight(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.target_id = None
        self.widgets, self.watched = [], []

    def mark(self, key, widgets, role=None):
        self.clear()
        self.target_id = key
        for widget in widgets:
            self.widgets.append((widget, widget.styleSheet()))
            widget.setProperty("fixHighlighted", True)
            widget.setProperty("fixTargetId", key)
            widget.setStyleSheet(widget.styleSheet() + '\n*[fixHighlighted="true"] { border:2px solid #ff555f; }')
            if role and hasattr(widget, "viewport"):
                widget.setProperty("fixRole", role)
            for target in [widget, *widget.findChildren(QObject)]:
                target.installEventFilter(self)
                self.watched.append(target)
            widget.update()

    def clear(self):
        for target in self.watched:
            target.removeEventFilter(self)
        for widget, style in self.widgets:
            widget.setProperty("fixHighlighted", False)
            widget.setProperty("fixTargetId", None)
            widget.setProperty("fixRole", None)
            widget.setStyleSheet(style)
            widget.update()
            if hasattr(widget, "viewport"):
                widget.viewport().update()
        self.target_id, self.widgets, self.watched = None, [], []

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.clear()
        return False


@dataclass
class FixTarget:
    tab: object
    widget: object
    role: str | None = None
    sound: str | None = None
    yaml_key: str | None = None


class SetupNavigation(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.highlight = FieldHighlight(self)
        window.teacher.role_list.setItemDelegate(RoleHintDelegate(window.teacher.role_list))
        window.preflight.fix_requested.connect(self.fix_line)
        window.preflight.help_key = self.resolve_key
        window.field_list.itemClicked.connect(lambda item, column: self.focus_yaml(item.data(0, Qt.ItemDataRole.UserRole)))
        window.field_search.textChanged.connect(self.filter_fields)
        self.refresh_targets()

    def refresh_targets(self):
        w, teacher = self.window, self.window.teacher
        self.targets = {key: FixTarget(w.profile_tab, w.editor, yaml_key=key) for key, _ in fields(w.profile.data)}
        self.targets["audio_device_name"] = FixTarget(w.audio_tab, w.devices_combo)
        self.targets["templates"] = FixTarget(teacher, teacher.role_list)
        self.targets["audio.templates"] = FixTarget(w.audio_clips, w.audio_clips.tree)
        for role, (roi_key, template_key, _) in ROLES.items():
            target = FixTarget(teacher, teacher.role_list, role=role)
            # Shared ROIs belong to their universal role, not a template crop.
            if roi_key not in self.targets or not self.targets[roi_key].role:
                self.targets[roi_key] = target
            if template_key:
                self.targets[template_key] = target
                HELP.setdefault(template_key, ROLE_HELP[role])
            self.targets[f"teacher_boxes.{role}"] = target
        # These have explicit entries in the existing Profile YAML editor.
        for key in ("logout.sequence", "logout.success_template", "logout.success_roi", "resolution", "game_origin", "window_title_contains", "counts_per_degree"):
            self.targets[key] = FixTarget(w.profile_tab, w.editor, yaml_key=key)
        for role in ("bubble", "splash", "tension", "quest_complete", "catch"):
            self.targets[f"audio.templates.{role}"] = FixTarget(teacher, teacher.audio_role, sound=role)
        w.field_list.clear()
        for key, _ in fields(w.profile.data):
            item = QTreeWidgetItem(w.field_list, [key])
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setToolTip(0, help_text(key))
            w.field_list.setItemWidget(item, 1, HelpButton(key))
        self.filter_fields(w.field_search.text())

    def filter_fields(self, text):
        for index in range(self.window.field_list.topLevelItemCount()):
            item = self.window.field_list.topLevelItem(index)
            item.setHidden(text.casefold() not in item.text(0).casefold())

    def resolve_key(self, line):
        # Missing-file diagnostics contain paths rather than setting names.
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, (list, dict)):
                for child in (value.values() if isinstance(value, dict) else value):
                    yield from strings(child)
        for key, value in fields(self.window.profile.data):
            for path in strings(value):
                if Path(path).suffix.lower() in (".wav", ".png"):
                    absolute = str(self.window.profile.settings.asset(path))
                    if absolute.casefold() in line.casefold() or path.casefold() in line.casefold():
                        return key
        for key in sorted(self.targets, key=len, reverse=True):
            if re.search(r"(?<![\w.])" + re.escape(key) + r"(?![\w.])", line):
                return key
        for phrase, key in (("catch PNG", "templates.catch"), ("ticks mode", "compass.tick_templates"),
                            ("verify_every_n_catches", "progress.verify_every_n_catches"),
                            ("max_unverified_catches", "progress.max_unverified_catches")):
            if phrase in line:
                return key
        match = re.match(r"([a-z_][\w.]*)", line)
        return match[1] if match else "profile"

    def fix_line(self, line):
        self.navigate(self.resolve_key(line))

    def focus_yaml(self, key):
        if key:
            self.navigate(key, force_yaml=True)

    def navigate(self, key, force_yaml=False):
        w, teacher = self.window, self.window.teacher
        target = self.targets.get(key)
        if force_yaml or target is None:
            target = FixTarget(w.profile_tab, w.editor, yaml_key=key)
        self.highlight.clear()
        w.tabs.setCurrentWidget(target.tab)
        widgets = [target.widget]
        if target.role:
            item = teacher.role_items[target.role]
            teacher.role_list.setCurrentItem(item)
            teacher.role_list.scrollToItem(item)
            if target.role in teacher.document.rects():
                widgets.append(teacher.canvas)
        if target.sound:
            teacher.audio_splitter.set_audio_expanded(True)
            teacher.audio_role.setCurrentText(target.sound)
        if target.yaml_key:
            row = yaml_line(w.editor.toPlainText(), target.yaml_key)
            if row is not None:
                cursor = w.editor.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.Start)
                cursor.movePosition(QTextCursor.MoveOperation.Down, n=row)
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                w.editor.setTextCursor(cursor)
                w.editor.ensureCursorVisible()
        target.widget.setFocus(Qt.FocusReason.OtherFocusReason)
        self.highlight.mark(key, widgets, target.role)
        description = ROLE_HELP[target.role] if target.role else help_text(key)
        if target.yaml_key:
            description = f"Edit {key} in the Profile YAML, then Save and Validate. " + description
        w.show_message(description)
