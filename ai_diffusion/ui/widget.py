from __future__ import annotations
from typing import Callable, Iterable, List, Optional

from PyQt5.QtWidgets import (
    QAction,
    QSlider,
    QPushButton,
    QWidget,
    QPlainTextEdit,
    QGroupBox,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QListWidget,
    QListView,
    QListWidgetItem,
    QMenu,
    QSpinBox,
    QDoubleSpinBox,
    QStackedWidget,
    QToolButton,
    QComboBox,
    QHBoxLayout,
    QVBoxLayout,
)
from PyQt5.QtGui import QFontMetrics, QGuiApplication, QKeyEvent, QMouseEvent
from PyQt5.QtCore import Qt, QSize, QUuid, pyqtSignal
from krita import Krita, DockWidget
import krita

from .. import Control, ControlMode, Style, Styles, Bounds, client
from . import actions, EventSuppression, SettingsDialog, theme
from .model import Model, ModelRegistry, Job, JobKind, JobQueue, State, Workspace
from .connection import Connection, ConnectionState
from ..util import ensure


class QueueWidget(QToolButton):
    _style = """
        QToolButton {{ border: none; border-radius: 6px; background-color: {color}; color: white; }}
        QToolButton::menu-indicator {{ width: 0px; }}"""

    def __init__(self, parent):
        super().__init__(parent)

        queue_menu = QMenu(self)
        queue_menu.addAction(self._create_action("Cancel active", actions.cancel_active))
        queue_menu.addAction(self._create_action("Cancel queued", actions.cancel_queued))
        queue_menu.addAction(self._create_action("Cancel all", actions.cancel_all))
        self.setMenu(queue_menu)

        self.setStyleSheet(self._style.format(color=theme.background_inactive))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setArrowType(Qt.ArrowType.NoArrow)

    def update(self, jobs: JobQueue):
        count = jobs.count(State.queued)
        if jobs.any_executing():
            self.setStyleSheet(self._style.format(color=theme.background_active))
            if count > 0:
                self.setToolTip(f"Generating image. {count} jobs queued - click to cancel.")
            else:
                self.setToolTip(f"Generating image. Click to cancel.")
        else:
            self.setStyleSheet(self._style.format(color=theme.background_inactive))
            self.setToolTip("Idle.")
        self.setText(f"+{count} ")

    def _create_action(self, name: str, func: Callable[[], None]):
        action = QAction(name, self)
        action.triggered.connect(func)
        return action


class ControlWidget(QWidget):
    changed = pyqtSignal()

    _model: Model
    _control: Control

    def __init__(self, parent=None):
        super().__init__(parent)
        model = Model.active()
        assert model
        self._model = model
        self._control = Control(ControlMode.image, self._model.document.active_layer)  # type: ignore (CTRLLAYER)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

        self.mode_select = QComboBox(self)
        self.mode_select.setStyleSheet(
            "QComboBox { border:none; background-color:transparent; padding: 1px 12px 1px 2px;}"
        )
        for mode in (m for m in ControlMode if m is not ControlMode.inpaint):
            icon = theme.icon(f"control-{mode.name}")
            self.mode_select.addItem(icon, mode.text, mode.value)
        self.mode_select.currentIndexChanged.connect(self._notify)
        self.mode_select.currentIndexChanged.connect(self._update_installed_packages)

        self.layer_select = QComboBox(self)
        self.layer_select.currentIndexChanged.connect(self._notify)
        self.layer_select.setMinimumContentsLength(20)
        self.layer_select.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLength
        )

        self.generate_button = QToolButton(self)
        self.generate_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.generate_button.setIcon(theme.icon("control-generate"))
        self.generate_button.setToolTip("Generate control layer from current image")
        self.generate_button.clicked.connect(self.generate)

        self.add_pose_button = QToolButton(self)
        self.add_pose_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.add_pose_button.setIcon(theme.icon("add-pose"))
        self.add_pose_button.setToolTip("Add new character pose to selected layer")
        self.add_pose_button.setVisible(False)
        self.add_pose_button.clicked.connect(self._add_pose_character)

        self.strength_spin = QSpinBox(self)
        self.strength_spin.setRange(0, 100)
        self.strength_spin.setValue(100)
        self.strength_spin.setSuffix("%")
        self.strength_spin.setSingleStep(10)
        self.strength_spin.valueChanged.connect(self._notify)

        self.error_text = QLabel(self)
        self.error_text.setText("ControlNet not installed")
        self.error_text.setStyleSheet(f"color: {theme.red};")
        self.error_text.setVisible(False)

        self.remove_button = QToolButton(self)
        self.remove_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.remove_button.setIcon(theme.icon("remove"))
        self.remove_button.setToolTip("Remove control layer")
        button_height = self.remove_button.iconSize().height()
        self.remove_button.setIconSize(QSize(int(button_height * 1.25), button_height))
        self.remove_button.setAutoRaise(True)
        self.remove_button.clicked.connect(self.remove)

        layout.addWidget(self.mode_select)
        layout.addWidget(self.layer_select, 1)
        layout.addWidget(self.generate_button)
        layout.addWidget(self.add_pose_button)
        layout.addWidget(self.strength_spin)
        layout.addWidget(self.error_text, 1)
        layout.addWidget(self.remove_button)

        self.value = Control(ControlMode.scribble, self._model.document.active_layer, 1)  # type: ignore (CTRLLAYER)

        # non-exhaustive list of actions that create/remove layers
        Krita.instance().action("add_new_paint_layer").triggered.connect(self.update_layers)
        Krita.instance().action("duplicatelayer").triggered.connect(self.update_layers)
        Krita.instance().action("remove_layer").triggered.connect(self.update_layers)

    _suppress_changes = EventSuppression()

    def _notify(self):
        if not self._suppress_changes:
            self._control.mode = ControlMode(self.mode_select.currentData())
            id = self.layer_select.currentData()
            self._control.image = self._model.document.find_layer(id)  # type: ignore (CTRLLAYER)
            self._control.strength = self.strength_spin.value() / 100
            self.changed.emit()

    def update_and_select_layer(self, id: QUuid):
        layers = reversed(self._model.document.image_layers)
        self.layer_select.clear()
        index = -1
        for layer in layers:
            self.layer_select.addItem(layer.name(), layer.uniqueId())
            if layer.uniqueId() == id:
                index = self.layer_select.count() - 1
        if index == -1 and self.value in self._model.control:
            self.remove()
        else:
            self.layer_select.setCurrentIndex(index)

    def update_layers(self):
        with self._suppress_changes:
            self.update_and_select_layer(self.layer_select.currentData())

    def generate(self):
        self._model.generate_control_layer(self.value)
        self.generate_button.setEnabled(False)
        self.layer_select.setEnabled(False)

    def remove(self):
        self._model.remove_control_layer(self.value)

    def _add_pose_character(self):
        self._model.document.add_pose_character(self.value.image)  # type: ignore (CTRLLAYER)

    @property
    def value(self):
        return self._control

    @value.setter
    def value(self, control: Control):
        self._control = control
        with self._suppress_changes:
            self.update_and_select_layer(control.image.uniqueId())  # type: ignore (CTRLLAYER)
            self.mode_select.setCurrentIndex(self.mode_select.findData(control.mode.value))
            self.strength_spin.setValue(int(control.strength * 100))
            if self._check_is_installed():
                active_job = self._model.jobs.find(control)
                has_active_job = active_job and active_job.state is not State.finished
                self.generate_button.setEnabled(not has_active_job)
                self.layer_select.setEnabled(not has_active_job)

    def _check_is_installed(self):
        connection = Connection.instance()
        is_installed = True
        mode = ControlMode(self.mode_select.currentData())
        if connection.state is ConnectionState.connected:
            sdver = client.resolve_sd_version(self._model.style, connection.client)
            if mode is ControlMode.image:
                if not sdver.has_ip_adapter:
                    self.error_text.setToolTip(f"Control mode is not supported for {sdver.value}")
                    is_installed = False
            elif connection.client.control_model[mode][sdver] is None:
                filenames = mode.filenames(sdver)
                self.error_text.setToolTip(
                    f"The server is missing {filenames}"
                    if filenames
                    else f"Control mode is not supported for {sdver.value}"
                )
                is_installed = False
        self.error_text.setVisible(False)  # Avoid layout resize
        self.layer_select.setVisible(is_installed)
        self.generate_button.setVisible(is_installed and mode is not ControlMode.image)
        self.add_pose_button.setVisible(is_installed and mode is ControlMode.pose)
        self.add_pose_button.setEnabled(self._is_vector_layer())
        self.strength_spin.setVisible(is_installed)
        self.strength_spin.setEnabled(self._is_first_image_mode())
        self.error_text.setVisible(not is_installed)
        return is_installed

    def _update_installed_packages(self):
        _ = self._check_is_installed()

    def _is_first_image_mode(self):
        return self._control.mode is not ControlMode.image or self._control == next(
            (c for c in self._model.control if c.mode is ControlMode.image), None
        )

    def _is_vector_layer(self):
        return isinstance(self.value.image, krita.Node) and self.value.image.type() == "vectorlayer"


class ControlListWidget(QWidget):
    _controls: List[ControlWidget]

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self._layout)

        self._controls = []

    def add(self):
        model = Model.active()
        assert model
        model.control.append(Control(ControlMode.image, model.document.active_layer))  # type: ignore (CTRLLAYER)
        self.value = model.control

    @property
    def value(self):
        # Filter out controls whose layer has been deleted
        result, removed = [], []
        for control in self._controls:
            c = control.value
            removed.append(control) if c.image is None else result.append(c)
        for control in removed:
            self._remove_widget(control)
        return result

    @value.setter
    def value(self, controls: List[Control]):
        with self._suppress_changes:
            while len(self._controls) > 0:
                self._remove_widget(self._controls[0])
            for control in controls:
                control_widget = self._add_widget()
                control_widget.value = control

    _suppress_changes = EventSuppression()

    def _notify(self):
        if not self._suppress_changes:
            self.changed.emit()

    def _add_widget(self):
        control = ControlWidget(self)
        control.changed.connect(self._notify)
        self._controls.append(control)
        self._layout.addWidget(control)
        return control

    def _remove_widget(self, control: ControlWidget):
        self._controls.remove(control)
        control.deleteLater()


class HistoryWidget(QListWidget):
    _last_prompt: Optional[str] = None
    _last_bounds: Optional[Bounds] = None

    def __init__(self, parent):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setResizeMode(QListView.Adjust)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFlow(QListView.LeftToRight)
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(96, 96))
        self.itemClicked.connect(self.handle_preview_click)

    def add(self, job: Job):
        if self._last_prompt != job.prompt or self._last_bounds != job.bounds:
            self._last_prompt = job.prompt
            self._last_bounds = job.bounds
            prompt = job.prompt if job.prompt != "" else "<no prompt>"

            header = QListWidgetItem(f"{job.timestamp:%H:%M} - {prompt}")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setData(Qt.ItemDataRole.UserRole, job.id)
            header.setData(Qt.ItemDataRole.ToolTipRole, job.prompt)
            header.setSizeHint(QSize(800, self.fontMetrics().lineSpacing() + 4))
            header.setTextAlignment(Qt.AlignmentFlag.AlignLeft)
            self.addItem(header)

        for i, img in enumerate(job.results):
            item = QListWidgetItem(img.to_icon(), None)  # type: ignore (text can be None)
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, i)
            item.setData(
                Qt.ItemDataRole.ToolTipRole,
                f"{job.prompt}\nClick to toggle preview, double-click to apply.",
            )
            self.addItem(item)

        scrollbar = self.verticalScrollBar()
        if scrollbar.isVisible() and scrollbar.value() >= scrollbar.maximum() - 4:
            self.scrollToBottom()

    def is_finished(self, job: Job):
        return job.kind is JobKind.diffusion and job.state is State.finished

    def prune(self, jobs: JobQueue):
        first_id = next((job.id for job in jobs if self.is_finished(job)), None)
        while self.count() > 0 and self.item(0).data(Qt.ItemDataRole.UserRole) != first_id:
            self.takeItem(0)

    def rebuild(self, jobs: Iterable[Job]):
        self.clear()
        for job in filter(self.is_finished, jobs):
            self.add(job)

    def item_info(self, item: QListWidgetItem):
        return item.data(Qt.ItemDataRole.UserRole), item.data(Qt.ItemDataRole.UserRole + 1)

    def handle_preview_click(self, item: QListWidgetItem):
        if item.text() != "" and item.text() != "<no prompt>":
            prompt = item.data(Qt.ItemDataRole.ToolTipRole)
            QGuiApplication.clipboard().setText(prompt)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        # make single click deselect current item (usually requires Ctrl+click)
        mods = e.modifiers()
        mods |= Qt.KeyboardModifier.ControlModifier
        e = QMouseEvent(
            e.type(),
            e.localPos(),
            e.windowPos(),
            e.screenPos(),
            e.button(),
            e.buttons(),
            mods,
            e.source(),
        )
        return super().mousePressEvent(e)


class StyleSelectWidget(QComboBox):
    _value: Style

    changed = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self._value = Styles.list()[0]

        self.addItems([style.name for style in Styles.list()])
        self.currentIndexChanged.connect(self.change_style)
        Styles.list().changed.connect(self.update_styles)
        Styles.list().name_changed.connect(self.update_styles)

    def update_styles(self):
        self.blockSignals(True)
        self.clear()
        self.addItems([style.name for style in Styles.list()])
        if self._value in Styles.list():
            self.setCurrentText(self._value.name)
        else:
            self._value = Styles.list()[0]
            self.setCurrentIndex(0)
            self.changed.emit()
        self.blockSignals(False)

    def change_style(self):
        style = Styles.list()[self.currentIndex()]
        if style != self._value:
            self._value = style
            self.changed.emit()

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, style: Style):
        if style != self._value:
            self._value = style
            self.setCurrentText(style.name)


class TextPromptWidget(QPlainTextEdit):
    activated = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setPlaceholderText("Describe the content you want to see, or leave empty.")
        fm = QFontMetrics(self.document().defaultFont())
        self.setFixedHeight(fm.lineSpacing() * 2 + 4)

    def keyPressEvent(self, event: QKeyEvent):
        if (
            event.key() == Qt.Key.Key_Return
            and event.modifiers() == Qt.KeyboardModifier.ShiftModifier
        ):
            self.activated.emit()
        else:
            super().keyPressEvent(event)


class WorkspaceSelectWidget(QToolButton):
    _icons = {
        Workspace.generation: theme.icon("workspace-generation"),
        Workspace.upscaling: theme.icon("workspace-upscaling"),
    }

    _value = Workspace.generation

    def __init__(self, parent):
        super().__init__(parent)

        menu = QMenu(self)
        menu.addAction(self._create_action("Generate", Workspace.generation))
        menu.addAction(self._create_action("Upscale", Workspace.upscaling))

        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setMenu(menu)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setArrowType(Qt.ArrowType.NoArrow)
        self.setAutoRaise(True)
        self.setToolTip("Switch between image generation and upscaling")
        self.value = Workspace.generation

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, workspace: Workspace):
        self._value = workspace
        self.setIcon(self._icons[workspace])

    def _create_action(self, name: str, workspace: Workspace):
        def set_workspace():
            model = ensure(Model.active())
            model.workspace = workspace
            model.changed.emit()

        action = QAction(name, self)
        action.setIcon(self._icons[workspace])
        action.setIconVisibleInMenu(True)
        action.triggered.connect(set_workspace)
        return action


class GenerationWidget(QWidget):
    _model: Optional[Model] = None

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 2, 0)
        self.setLayout(layout)

        self.workspace_select = WorkspaceSelectWidget(self)

        self.style_select = StyleSelectWidget(self)
        self.style_select.changed.connect(self.change_style)

        self.settings_button = QToolButton(self)
        self.settings_button.setIcon(theme.icon("settings"))
        self.settings_button.setAutoRaise(True)
        self.settings_button.clicked.connect(self.show_settings)

        style_layout = QHBoxLayout()
        style_layout.addWidget(self.workspace_select)
        style_layout.addWidget(self.style_select)
        style_layout.addWidget(self.settings_button)
        layout.addLayout(style_layout)

        self.prompt_textbox = TextPromptWidget(self)
        self.prompt_textbox.textChanged.connect(self.change_prompt)
        self.prompt_textbox.activated.connect(self.generate)
        layout.addWidget(self.prompt_textbox)

        self.control_list = ControlListWidget(self)
        self.control_list.changed.connect(self.change_control)
        layout.addWidget(self.control_list)

        self.strength_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.strength_slider.setMinimum(0)
        self.strength_slider.setMaximum(100)
        self.strength_slider.setSingleStep(5)
        self.strength_slider.valueChanged.connect(self.change_strength)

        self.strength_input = QSpinBox(self)
        self.strength_input.setMinimum(0)
        self.strength_input.setMaximum(100)
        self.strength_input.setSingleStep(5)
        self.strength_input.setPrefix("Strength: ")
        self.strength_input.setSuffix("%")
        self.strength_input.valueChanged.connect(self.change_strength)

        self.add_control_button = QToolButton(self)
        self.add_control_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.add_control_button.setIcon(theme.icon("control-add"))
        self.add_control_button.setToolTip("Add control layer")
        self.add_control_button.setAutoRaise(True)
        icon_height = self.add_control_button.iconSize().height()
        self.add_control_button.setIconSize(QSize(int(icon_height * 1.25), icon_height))
        self.add_control_button.clicked.connect(self.control_list.add)

        strength_layout = QHBoxLayout()
        strength_layout.addWidget(self.strength_slider)
        strength_layout.addWidget(self.strength_input)
        strength_layout.addWidget(self.add_control_button)
        layout.addLayout(strength_layout)

        self.generate_button = QPushButton("Generate", self)
        self.generate_button.setMinimumHeight(int(self.generate_button.sizeHint().height() * 1.2))
        self.generate_button.clicked.connect(self.generate)

        self.queue_button = QueueWidget(self)

        actions_layout = QHBoxLayout()
        actions_layout.addWidget(self.generate_button)
        actions_layout.addWidget(self.queue_button)
        layout.addLayout(actions_layout)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        layout.addWidget(self.progress_bar)

        self.error_text = QLabel(self)
        self.error_text.setStyleSheet("font-weight: bold; color: red;")
        self.error_text.setWordWrap(True)
        self.error_text.setVisible(False)
        layout.addWidget(self.error_text)

        self.history = HistoryWidget(self)
        self.history.itemSelectionChanged.connect(self.select_preview)
        self.history.itemDoubleClicked.connect(self.apply_result)
        layout.addWidget(self.history)

        self.apply_button = QPushButton(theme.icon("apply"), "Apply", self)
        self.apply_button.clicked.connect(self.apply_selected_result)
        layout.addWidget(self.apply_button)

    @property
    def model(self):
        assert self._model is not None
        return self._model

    @model.setter
    def model(self, model: Model):
        if self._model != model:
            self.history.rebuild(model.history)
            self._model = model

    def update(self):
        model = self.model
        self.workspace_select.value = model.workspace
        self.style_select.value = model.style
        self.prompt_textbox.setPlainText(model.prompt)
        self.control_list.value = model.control
        self.strength_input.setValue(int(model.strength * 100))
        self.error_text.setText(model.error)
        self.error_text.setVisible(model.error != "")
        self.apply_button.setEnabled(model.can_apply_result)
        self.update_progress()

    def update_progress(self):
        self.progress_bar.setValue(int(self.model.progress * 100))
        self.queue_button.update(self.model.jobs)

    def show_results(self, job: Job):
        if job.kind is JobKind.diffusion:
            self.history.prune(self.model.jobs)
            self.history.add(job)

    def generate(self):
        self.model.generate()
        self.update()

    def change_style(self):
        self.model.style = self.style_select.value

    def change_prompt(self):
        self.model.prompt = self.prompt_textbox.toPlainText()

    def change_strength(self, value: int):
        self.model.strength = value / 100
        if self.strength_input.value() != value:
            self.strength_input.setValue(value)
        if self.strength_slider.value() != value:
            self.strength_slider.setValue(value)

    def change_control(self):
        self.model.control = self.control_list.value

    def show_settings(self):
        SettingsDialog.instance().show(self.model.style)

    def show_preview(self, item: QListWidgetItem):
        job_id, index = self.history.item_info(item)
        self.model.show_preview(job_id, index)

    def select_preview(self):
        items = self.history.selectedItems()
        if len(items) > 0:
            self.show_preview(items[0])
        else:
            self.model.hide_preview()

    def apply_selected_result(self):
        self.model.apply_current_result()

    def apply_result(self, item: QListWidgetItem):
        self.show_preview(item)
        self.apply_selected_result()


class UpscaleWidget(QWidget):
    model_: Optional[Model] = None

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 4, 0)
        self.setLayout(layout)

        self.workspace_select = WorkspaceSelectWidget(self)

        self.model_select = QComboBox(self)
        self.model_select.currentIndexChanged.connect(self.change_model)

        model_layout = QHBoxLayout()
        model_layout.addWidget(self.workspace_select)
        model_layout.addWidget(self.model_select)
        layout.addLayout(model_layout)

        self.factor_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.factor_slider.setMinimum(100)
        self.factor_slider.setMaximum(400)
        self.factor_slider.setSingleStep(10)
        self.factor_slider.setPageStep(50)
        self.factor_slider.valueChanged.connect(self.change_factor)

        self.factor_input = QDoubleSpinBox(self)
        self.factor_input.setMinimum(1.0)
        self.factor_input.setMaximum(4.0)
        self.factor_input.setSingleStep(0.5)
        self.factor_input.setPrefix("Scale: ")
        self.factor_input.setSuffix("x")
        self.factor_input.setDecimals(1)
        self.factor_input.valueChanged.connect(self.change_factor)

        factor_layout = QHBoxLayout()
        factor_layout.addWidget(self.factor_slider)
        factor_layout.addWidget(self.factor_input)
        layout.addLayout(factor_layout)

        self.target_label = QLabel("Target size:", self)
        layout.addWidget(self.target_label, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addSpacing(6)

        self.refinement_checkbox = QGroupBox("Refine upscaled image", self)
        self.refinement_checkbox.setCheckable(True)
        self.refinement_checkbox.toggled.connect(self.change_refinement)

        self.style_select = StyleSelectWidget(self)
        self.style_select.changed.connect(self.change_style)

        self.strength_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.strength_slider.setMinimum(20)
        self.strength_slider.setMaximum(50)
        self.strength_slider.setPageStep(5)
        self.strength_slider.valueChanged.connect(self.change_strength)

        self.strength_input = QSpinBox(self)
        self.strength_input.setMinimum(0)
        self.strength_input.setMaximum(100)
        self.strength_input.setSingleStep(5)
        self.strength_input.setPrefix("Strength: ")
        self.strength_input.setSuffix("%")
        self.strength_input.valueChanged.connect(self.change_strength)

        strength_layout = QHBoxLayout()
        strength_layout.addWidget(self.strength_slider)
        strength_layout.addWidget(self.strength_input)

        group_layout = QVBoxLayout(self.refinement_checkbox)
        group_layout.addWidget(self.style_select)
        group_layout.addLayout(strength_layout)
        self.refinement_checkbox.setLayout(group_layout)
        layout.addWidget(self.refinement_checkbox)

        self.upscale_button = QPushButton("Upscale", self)
        self.upscale_button.setMinimumHeight(int(self.upscale_button.sizeHint().height() * 1.2))
        self.upscale_button.clicked.connect(self.upscale)

        self.queue_button = QueueWidget(self)

        actions_layout = QHBoxLayout()
        actions_layout.addWidget(self.upscale_button)
        actions_layout.addWidget(self.queue_button)
        layout.addLayout(actions_layout)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        layout.addWidget(self.progress_bar)

        self.error_text = QLabel(self)
        self.error_text.setStyleSheet("font-weight: bold; color: red;")
        self.error_text.setWordWrap(True)
        self.error_text.setVisible(False)
        layout.addWidget(self.error_text)

        layout.addStretch()

    @property
    def model(self):
        assert self._model is not None
        return self._model

    @model.setter
    def model(self, model: Model):
        self._model = model

    def update(self):
        client = Connection.instance().client
        params = self.model.upscale
        self.workspace_select.value = self.model.workspace
        self.model_select.clear()
        self.model_select.addItems(client.upscalers)
        self.model_select.setCurrentText(params.upscaler)
        self.factor_slider.setValue(int(params.factor * 100))
        self.factor_input.setValue(params.factor)
        self.target_label.setText(
            f"Target size: {params.target_extent.width} x {params.target_extent.height}"
        )
        self.refinement_checkbox.setChecked(params.use_diffusion)
        self.style_select.value = self.model.style
        self.strength_slider.setValue(int(params.strength * 100))
        self.strength_input.setValue(int(params.strength * 100))
        self.error_text.setText(self.model.error)
        self.error_text.setVisible(self.model.error != "")
        self.update_progress()

    def update_progress(self):
        self.progress_bar.setValue(int(self.model.progress * 100))
        self.queue_button.update(self.model.jobs)

    def upscale(self):
        self.model.upscale_image()

    def change_model(self):
        self.model.upscale.upscaler = self.model_select.currentText()

    def change_factor(self, value: int | float):
        params = self.model.upscale
        value = value / 100 if isinstance(value, int) else value
        params.factor = value
        if self.factor_input.value() != value:
            self.factor_input.setValue(value)
        if self.factor_slider.value() != value:
            self.factor_slider.setValue(int(value * 100))
        self.target_label.setText(
            f"Target size: {params.target_extent.width} x {params.target_extent.height}"
        )

    def change_refinement(self):
        self.model.upscale.use_diffusion = self.refinement_checkbox.isChecked()
        self.update()

    def change_style(self):
        self.model.style = self.style_select.value

    def change_strength(self, value: int):
        self.model.upscale.strength = value / 100
        if self.strength_input.value() != value:
            self.strength_input.setValue(value)
        if self.strength_slider.value() != value:
            self.strength_slider.setValue(value)


class WelcomeWidget(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        self.setLayout(layout)

        header_layout = QHBoxLayout()
        header_logo = QLabel(self)
        header_logo.setPixmap(theme.logo().scaled(64, 64))
        header_logo.setMaximumSize(64, 64)
        header_text = QLabel("AI Image\nGeneration", self)
        header_text.setStyleSheet("font-size: 12pt")
        header_layout.addWidget(header_logo)
        header_layout.addWidget(header_text)
        layout.addLayout(header_layout)
        layout.addSpacing(12)

        self._connect_status = QLabel("Not connected to server.", self)
        layout.addWidget(self._connect_status)
        layout.addSpacing(6)

        self._connect_error = QLabel(self)
        self._connect_error.setVisible(False)
        self._connect_error.setWordWrap(True)
        self._connect_error.setStyleSheet(f"color: {theme.yellow};")
        layout.addWidget(self._connect_error)

        self._settings_button = QPushButton(theme.icon("settings"), "Configure", self)
        self._settings_button.setMinimumHeight(32)
        self._settings_button.clicked.connect(self.show_settings)
        layout.addWidget(self._settings_button)

        layout.addStretch()

        Connection.instance().changed.connect(self.update)

    def update(self):
        connection = Connection.instance()
        if connection.state in [ConnectionState.disconnected, ConnectionState.error]:
            self._connect_status.setText("Not connected to server.")
        if connection.state is ConnectionState.error:
            self._connect_error.setText(
                "Connection attempt failed! Click below to configure and reconnect."
            )
            self._connect_error.setVisible(True)
        if connection.state is ConnectionState.connecting:
            self._connect_status.setText(f"Connecting to server...")
        if connection.state is ConnectionState.connected:
            self._connect_status.setText(
                f"Connected to server at {connection.client.url}.\n\nCreate"
                " a new document or open an existing image to start!"
            )
            self._connect_error.setVisible(False)

    def show_settings(self):
        Krita.instance().action("ai_diffusion_settings").trigger()


class ImageDiffusionWidget(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Image Generation")
        self._welcome = WelcomeWidget()
        self._generation = GenerationWidget()
        self._upscaling = UpscaleWidget()
        self._frame = QStackedWidget(self)
        self._frame.addWidget(self._welcome)
        self._frame.addWidget(self._generation)
        self._frame.addWidget(self._upscaling)
        self.setWidget(self._frame)

        Connection.instance().changed.connect(self.update)
        ModelRegistry.instance().created.connect(self.register_model)

    def canvasChanged(self, canvas):
        self.update()

    def register_model(self, model):
        model.changed.connect(self.update)
        model.job_finished.connect(self._generation.show_results)
        model.progress_changed.connect(self.update_progress)

    def update(self):
        model = Model.active()
        connection = Connection.instance()
        if model is None or connection.state in [
            ConnectionState.disconnected,
            ConnectionState.connecting,
            ConnectionState.error,
        ]:
            self._frame.setCurrentWidget(self._welcome)
        elif model.workspace is Workspace.generation:
            self._generation.model = model
            self._generation.update()
            self._frame.setCurrentWidget(self._generation)
        elif model.workspace is Workspace.upscaling:
            self._upscaling.model = model
            self._upscaling.update()
            self._frame.setCurrentWidget(self._upscaling)

    def update_progress(self):
        model = ensure(Model.active())
        if model.workspace is Workspace.generation:
            self._generation.update_progress()
        elif model.workspace is Workspace.upscaling:
            self._upscaling.update_progress()
