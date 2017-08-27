#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from os import path
from math import sin, cos, pi

from bluesky import RunEngine
from bluesky.utils import install_qt_kicker
from bluesky.plans import sleep, checkpoint

from pydm import Display
from pydm.PyQt.QtCore import pyqtSlot, QCoreApplication, QPoint
from pydm.PyQt.QtGui import QDoubleValidator

from pswalker.config import homs_system
from pswalker.skywalker import lcls_RE

logging.basicConfig(level=logging.DEBUG,
                    format=('%(asctime)s '
                            '%(name)-12s '
                            '%(levelname)-8s '
                            '%(message)s'),
                    datefmt='%m-%d %H:%M:%S',
                    filename='./skywalker_debug.log',
                    filemode='a')
logger = logging.getLogger(__name__)
MAX_MIRRORS = 4

config = homs_system()


class SkywalkerGui(Display):
    """
    Display class to define all the logic for the skywalker alignment gui.
    Refers to widgets in the .ui file.
    """
    # System mapping of associated devices
    system = dict(
        m1h=dict(mirror=config['m1h'],
                 imager=config['hx2'],
                 slits=config['hx2_slits'],
                 rotation=90),
        m2h=dict(mirror=config['m2h'],
                 imager=config['dg3'],
                 slits=config['dg3_slits'],
                 rotation=90),
        mfx=dict(mirror=config['xrtm2'],
                 imager=config['mfxdg1'],
                 slits=config['mfxdg1_slits'],
                 rotation=90)
    )

    # Alignment mapping of which sets to use for each alignment
    alignments = {'HOMS': [['m1h', 'm2h']],
                  'MFX': [['mfx']],
                  'HOMS + MFX': [['m1h', 'm2h'], ['mfx']]}

    def __init__(self, parent=None, args=None):
        super().__init__(parent=parent, args=args)
        ui = self.ui

        # Load config into the combo box objects
        ui.image_title_combo.clear()
        ui.procedure_combo.clear()
        self.all_imager_names = [entry['imager'].name for
                                 entry in self.system.values()]
        for imager_name in self.all_imager_names:
            ui.image_title_combo.addItem(imager_name)
        for align in self.alignments.keys():
            ui.procedure_combo.addItem(align)

        # Pick out some initial parameters from system and alignment dicts
        first_alignment_name = list(self.alignments.keys())[0]
        first_system_key = list(self.alignments.values())[0][0][0]
        first_set = self.system[first_system_key]
        first_imager = first_set['imager']
        first_slit = first_set['slits']
        first_rotation = first_set.get('rotation', 0)

        # self.procedure and self.image_obj keep track of the gui state
        self.procedure = first_alignment_name
        self.image_obj = first_imager

        # Initialize slit readback
        self.slit_group = ObjWidgetGroup([ui.slit_x_width,
                                          ui.slit_y_width],
                                         ['xwidth.readback',
                                          'ywidth.readback'],
                                         first_slit,
                                         label=ui.readback_slits_title)

        # Initialize mirror control
        self.mirror_groups = []
        mirror_labels = self.get_widget_set('mirror_name')
        mirror_rbvs = self.get_widget_set('mirror_readback')
        mirror_vals = self.get_widget_set('mirror_setpos')
        mirror_circles = self.get_widget_set('mirror_circle')
        for label, rbv, val, circle, mirror in zip(mirror_labels,
                                                   mirror_rbvs,
                                                   mirror_vals,
                                                   mirror_circles,
                                                   self.mirrors_padded()):
            mirror_group = ObjWidgetGroup([rbv, val, circle],
                                          ['pitch.user_readback',
                                           'pitch.user_setpoint',
                                           'pitch.motor_done_move'],
                                          mirror, label=label)
            if mirror is None:
                mirror_group.hide()
            self.mirror_groups.append(mirror_group)

        # Initialize the goal entry fields
        self.goal_cache = {}
        self.goals_groups = []
        goal_labels = self.get_widget_set('goal_name')
        goal_edits = self.get_widget_set('goal_value')
        goal_slits = self.get_widget_set('slit_check')
        for label, edit, slit, img in zip(goal_labels, goal_edits,
                                          goal_slits, self.imagers_padded()):
            if img is None:
                name = None
            else:
                name = img.name
            validator = QDoubleValidator(0, 5000, 3)
            goal_group = ValueWidgetGroup(edit, label, checkbox=slit,
                                          name=name, cache=self.goal_cache,
                                          validator=validator)
            if img is None:
                goal_group.hide()
            self.goals_groups.append(goal_group)

        # Initialize image and centroids. Needs goals defined first.
        self.image_group = ImgObjWidget(ui.image, first_imager,
                                        ui.beam_x_value, ui.beam_y_value,
                                        ui.beam_x_delta, ui.beam_y_delta,
                                        ui.readback_imager_title,
                                        self, first_rotation)

        # Create the RunEngine that will be used in the alignments.
        # This gives us the ability to pause, etc.
        self.RE = lcls_RE()
        install_qt_kicker()

        # Some hax to keep the state string updated
        # There is probably a better way to do this
        # This might break on some package update
        self.RE.state  # Yes this matters
        old_set = RunEngine.state._memory[self.RE].set_
        def new_set(state):  # NOQA
            old_set(state)
            txt = " Status: " + state.capitalize()
            self.ui.status_label.setText(txt)
        RunEngine.state._memory[self.RE].set_ = new_set

        # Connect relevant signals and slots
        procedure_changed = ui.procedure_combo.activated[str]
        procedure_changed.connect(self.on_procedure_combo_changed)

        imager_changed = ui.image_title_combo.activated[str]
        imager_changed.connect(self.on_image_combo_changed)

        for goal_value in self.get_widget_set('goal_value'):
            goal_changed = goal_value.editingFinished
            goal_changed.connect(self.on_goal_changed)

        start_pressed = ui.start_button.clicked
        start_pressed.connect(self.on_start_button)

        pause_pressed = ui.pause_button.clicked
        pause_pressed.connect(self.on_pause_button)

        abort_pressed = ui.abort_button.clicked
        abort_pressed.connect(self.on_abort_button)

        slits_pressed = ui.slit_run_button.clicked
        slits_pressed.connect(self.on_slits_button)

        # Setup the on-screen logger
        self.setup_gui_logger()

    def setup_gui_logger(self):
        """
        Initializes the text stream at the bottom of the gui. This text stream
        is actually just the log messages from Python!
        """
        console = GuiHandler(self.ui.log_text)
        console.setLevel(logging.INFO)
        formatter = logging.Formatter(fmt='%(asctime)s %(message)s',
                                      datefmt='%m-%d %H:%M:%S')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)
        logger.info("Skywalker GUI initialized.")

    @pyqtSlot(str)
    def on_image_combo_changed(self, imager_name):
        """
        Slot for the combo box above the image feed. This swaps out the imager,
        centroid, and slit readbacks.

        Parameters
        ----------
        imager_name: str
            name of the imager to activate
        """
        logger.info('Selecting imager %s', imager_name)
        for k, v in self.system.items():
            if imager_name == v['imager'].name:
                image_obj = v['imager']
                slits_obj = v.get('slits')
                rotation = v.get('rotation', 0)
        self.image_obj = image_obj
        self.image_group.change_obj(image_obj, rotation=rotation)
        if slits_obj is not None:
            self.slit_group.change_obj(slits_obj)

    @pyqtSlot(str)
    def on_procedure_combo_changed(self, procedure_name):
        """
        Slot for the main procedure combo box. This swaps out the mirror and
        goals sections to match the chosen procedure, and determines what
        happens when we press go.

        Parameters
        ----------
        procedure_name: str
            name of the procedure to activate
        """
        logger.info('Selecting procedure %s', procedure_name)
        self.procedure = procedure_name
        for obj, widgets in zip(self.mirrors_padded(), self.mirror_groups):
            if obj is None:
                widgets.hide()
            else:
                widgets.change_obj(obj)
                widgets.show()
        for obj, widgets in zip(self.imagers_padded(), self.goals_groups):
            widgets.save_value()
            widgets.clear()
        for obj, widgets in zip(self.imagers_padded(), self.goals_groups):
            if obj is None:
                widgets.hide()
            else:
                widgets.setup(name=obj.name)
                widgets.show()

    @pyqtSlot()
    def on_goal_changed(self):
        """
        Slot for when the user picks a new goal. Updates the goal delta so it
        reflects the new chosen value.
        """
        self.image_group.update_deltas()

    @pyqtSlot()
    def on_start_button(self):
        """
        Slot for the start button. This begins from an idle state or resumes
        from a paused state.
        """
        if self.RE.state == 'idle':
            logger.info("Starting %s procedure", self.procedure)
            try:
                # TODO Skywalker here
                def plan(n):
                    for i in range(n):
                        logger.info("Fake align pt %s", i + 1)
                        yield from checkpoint()
                        yield from sleep(2)
                    logger.info("Fake align done")
                self.RE(plan(10))
            except:
                logger.exception("Error in procedure.")
        elif self.RE.state == 'paused':
            logger.info("Resuming procedure.")
            try:
                self.RE.resume()
            except:
                logger.exception("Error in procedure.")

    @pyqtSlot()
    def on_pause_button(self):
        """
        Slot for the pause button. This brings us from the running state to the
        paused state.
        """
        if self.RE.state == 'running':
            logger.info("Pausing procedure.")
            try:
                self.RE.request_pause()
            except:
                logger.exception("Error on pause.")

    @pyqtSlot()
    def on_abort_button(self):
        """
        Slot for the abort button. This brings us from any state to the idle
        state.
        """
        if self.RE.state != 'idle':
            logger.info("Aborting procedure.")
            try:
                self.RE.abort()
            except:
                logger.exception("Error on abort.")

    @pyqtSlot()
    def on_slits_button(self):
        """
        Slot for the slits procedure. This checks the slit fiducialization.
        """
        pass

    def active_system(self):
        """
        List of system keys that are part of the active procedure.
        """
        active_system = []
        for part in self.alignments[self.procedure]:
            active_system.extend(part)
        return active_system

    def mirrors(self):
        """
        List of active mirror objects.
        """
        return [self.system[act]['mirror'] for act in self.active_system()]

    def imagers(self):
        """
        List of active imager objects.
        """
        return [self.system[act]['imager'] for act in self.active_system()]

    def slits(self):
        """
        List of active slits objects.
        """
        return [self.system[act].get('slits') for act in self.active_system()]

    def goals(self):
        """
        List of goals in the user entry boxes, or None for empty or invalid
        goals.
        """
        return [goal.value for goal in self.goals_groups]

    def goal(self):
        """
        The goal associated with the visible imager, or None if the visible
        imager is not part of the active procedure.
        """
        index = self.procedure_index()
        if index is None:
            return None
        else:
            return self.goals()[index]

    def procedure_index(self):
        """
        Goal index of the active imager, or None if the visible imager is not
        part of the active procedure.
        """
        try:
            return self.imagers_padded().index(self.image_obj)
        except ValueError:
            return None

    def none_pad(self, obj_list):
        """
        Helper function to extend a list with 'None' objects until it's the
        length of MAX_MIRRORS.
        """
        padded = []
        padded.extend(obj_list)
        while len(padded) < MAX_MIRRORS:
            padded.append(None)
        return padded

    def mirrors_padded(self):
        return self.none_pad(self.mirrors())

    def imagers_padded(self):
        return self.none_pad(self.imagers())

    def slits_padded(self):
        return self.none_pad(self.slits())

    def get_widget_set(self, name, num=MAX_MIRRORS):
        """
        Widgets that come in sets of count MAX_MIRRORS are named carefully so
        we can use this macro to grab related widgets.

        Parameters
        ----------
        name: str
            Base name of widget set e.g. 'name'

        num: int, optional
            Number of widgets to return

        Returns
        -------
        widget_set: list
            List of widgets e.g. 'name_1', 'name_2', 'name_3'...
        """
        widgets = []
        for n in range(1, num + 1):
            widget = getattr(self.ui, name + "_" + str(n))
            widgets.append(widget)
        return widgets

    def ui_filename(self):
        return 'skywalker_gui.ui'

    def ui_filepath(self):
        return path.join(path.dirname(path.realpath(__file__)),
                         self.ui_filename())

intelclass = SkywalkerGui # NOQA


class GuiHandler(logging.Handler):
    """
    Logging handler that logs to a scrolling text widget.
    """
    terminator = '\n'

    def __init__(self, text_widget, level=logging.NOTSET):
        super().__init__(level=level)
        self.text_widget = text_widget

    def emit(self, record):
        try:
            msg = self.format(record)
            cursor = self.text_widget.cursorForPosition(QPoint(0, 0))
            cursor.insertText(msg + self.terminator)
        except Exception:
            self.handleError(record)


class BaseWidgetGroup:
    """
    A group of widgets that are part of a set with a single label.
    """
    def __init__(self, widgets, label=None, name=None, **kwargs):
        """
        Parameters
        ----------
        widgets: list
            list of widgets in the group

        label: QLabel, optional
            A special widget that acts as the label for the group

        name: str, optional
            The label text
        """
        self.widgets = widgets
        self.label = label
        self.setup(name=name, **kwargs)

    def setup(self, name=None, **kwargs):
        """
        Do basic widget setup. For Base, this is just changing the label text.
        """
        if None not in (self.label, name):
            self.label.setText(name)

    def hide(self):
        """
        Hide all widgets in group.
        """
        for widget in self.widgets:
            widget.hide()
        if self.label is not None:
            self.label.hide()

    def show(self):
        """
        Show all widgets in group.
        """
        for widget in self.widgets:
            widget.show()
        if self.label is not None:
            self.label.show()


class ValueWidgetGroup(BaseWidgetGroup):
    """
    A group of widgets that have a user-editable value field.
    """
    def __init__(self, line_edit, label, checkbox=None, name=None, cache=None,
                 validator=None):
        """
        Parameters
        ----------
        line_edit: QLineEdit
            The user-editable value field.

        checkbox: QCheckbox, optional
            Optional checkbox widget associated with the value.

        cache: dict, optional
            For widgets that need to save/share values

        validator: QDoubleValidator, optional
            Make sure the text is a double
        """
        widgets = [line_edit]
        if checkbox is not None:
            widgets.append(checkbox)
        self.line_edit = line_edit
        self.checkbox = checkbox
        if cache is None:
            self.cache = {}
        else:
            self.cache = cache
        if validator is None:
            self.force_type = None
        else:
            if isinstance(validator, QDoubleValidator):
                self.force_type = float
            else:
                raise NotImplementedError
            self.line_edit.setValidator(validator)
        super().__init__(widgets, label=label, name=name)

    def setup(self, name=None, **kwargs):
        """
        Put name in the checkbox too
        """
        super().setup(name=name, **kwargs)
        if None not in (self.checkbox, name):
            self.checkbox.setText(name)
        if self.checkbox is not None:
            self.checkbox.setChecked(False)
        self.load_value(name)

    def save_value(self):
        """
        Stash current value in self.cache
        """
        old_name = self.label.text()
        old_value = self.value
        if None not in (old_name, old_value):
            self.cache[old_name] = old_value

    def load_value(self, name):
        """
        Grab current value from self.cache
        """
        cache_value = self.cache.get(name)
        if cache_value is not None:
            self.value = cache_value

    def clear(self):
        """
        Reset the value
        """
        self.line_edit.clear()

    @property
    def value(self):
        raw = self.line_edit.text()
        if not raw:
            return None
        if self.force_type is None:
            return raw
        else:
            try:
                return self.force_type(raw)
            except:
                return None

    @value.setter
    def value(self, val):
        txt = str(val)
        self.line_edit.setText(txt)


class PydmWidgetGroup(BaseWidgetGroup):
    """
    A group of pydm widgets under a single label that may be set up and reset
    as a group.
    """
    protocol = 'ca://'

    def __init__(self, widgets, pvnames, label=None, name=None, **kwargs):
        """
        Parameters
        ----------
        pvnames: list
            pvs to assign to the widgets
        """
        super().__init__(widgets, label=label, name=name,
                         pvnames=pvnames, **kwargs)

    def setup(self, *, pvnames, name=None, **kwargs):
        """
        In addition to base setup, assign pv names.
        """
        super().setup(name=name, **kwargs)
        if pvnames is None:
            pvnames = [None] * len(self.widgets)
        for widget, pvname in zip(self.widgets, pvnames):
            if pvname is None:
                chan = ''
            else:
                chan = self.protocol + pvname
            try:
                widget.setChannel(chan)
            except:
                widget.channel = chan

    def change_pvs(self, pvnames, name=None, **kwargs):
        """
        Swap active pv names and manage connections
        """
        self.clear_connections()
        self.setup(pvnames=pvnames, name=name, **kwargs)
        self.create_connections()

    def clear_connections(self):
        """
        Tell pydm to drop own pv connections.
        """
        QApp = QCoreApplication.instance()
        for widget in self.widgets:
            QApp.close_widget_connections(widget)
            widget._channels = None

    def create_connections(self):
        """
        Tell pydm to establish own pv connections.
        """
        QApp = QCoreApplication.instance()
        for widget in self.widgets:
            QApp.establish_widget_connections(widget)


class ObjWidgetGroup(PydmWidgetGroup):
    """
    A group of pydm widgets that get their channels from an object that can be
    stripped out and replaced to change context, provided the class is the
    same.
    """
    def __init__(self, widgets, attrs, obj, label=None, **kwargs):
        """
        Parameters
        ----------
        attrs: list
            list of attribute strings to pull from obj e.g. 'centroid.x'

        obj: object
            Any object that holds ophyd EpicsSignal objects that have pvname
            fields that we can use to send pvname info to pydm
        """
        self.attrs = attrs
        self.obj = obj
        if obj is None:
            name = None
        else:
            name = obj.name
        pvnames = self.get_pvnames(obj)
        super().__init__(widgets, pvnames, label=label, name=name,
                         **kwargs)

    def change_obj(self, obj, **kwargs):
        """
        Swap the active object and fix connections

        Parameters
        ----------
        obj: object
            The new object
        """
        self.obj = obj
        pvnames = self.get_pvnames(obj)
        self.change_pvs(pvnames, name=obj.name, **kwargs)

    def get_pvnames(self, obj):
        """
        Given an object, return the pvnames based on self.attrs
        """
        if obj is None:
            return None
        pvnames = []
        for attr in self.attrs:
            sig = self.nested_getattr(obj, attr)
            pvnames.append(sig.pvname)
        return pvnames

    def nested_getattr(self, obj, attr):
        """
        Do a getattr more than one level deep, splitting on '.'
        """
        steps = attr.split('.')
        for step in steps:
            obj = getattr(obj, step)
        return obj


class ImgObjWidget(ObjWidgetGroup):
    """
    Macros to set up the image widget channels from opyhd areadetector obj.
    This also includes all of the centroid stuff.
    """
    def __init__(self, img_widget, img_obj, cent_x_widget, cent_y_widget,
                 delta_x_widget, delta_y_widget, label, goals_source,
                 rotation=0):
        self.cent_x_widget = cent_x_widget
        self.cent_y_widget = cent_y_widget
        self.delta_x_widget = delta_x_widget
        self.delta_y_widget = delta_y_widget
        self.goals_source = goals_source
        attrs = ['detector.image2.width',
                 'detector.image2.array_data']
        super().__init__([img_widget], attrs, img_obj, label=label,
                         rotation=rotation)

    def setup(self, *, pvnames, rotation=0, **kwargs):
        self.rotation = rotation
        img_widget = self.widgets[0]
        width_pv = pvnames[0]
        image_pv = pvnames[1]
        img_widget.getImageItem().setRotation(rotation)
        img_widget.resetImageChannel()
        img_widget.resetWidthChannel()
        img_widget.setWidthChannel(self.protocol + width_pv)
        img_widget.setImageChannel(self.protocol + image_pv)
        centroid = self.obj.detector.stats2.centroid
        self.beam_x_stats = centroid.x
        self.beam_y_stats = centroid.y
        self.beam_x_stats.subscribe(self.update_centroid)
        self.update_centroid()

    def update_centroid(self, *args, **kwargs):
        centroid_x = self.beam_x_stats.value
        centroid_y = self.beam_y_stats.value
        rotation = -self.rotation
        xpos, ypos = self.rotate(centroid_x, centroid_y, rotation)
        if xpos < 0:
            xpos += self.size_x
        if ypos < 0:
            ypos += self.size_y
        self.xpos = xpos
        self.ypos = ypos
        self.cent_x_widget.setText(str(xpos))
        self.cent_y_widget.setText(str(ypos))
        self.update_deltas()

    def update_deltas(self):
        goal = self.goals_source.goal()
        if goal is None:
            self.delta_x_widget.clear()
        else:
            self.delta_x_widget.setText(str(self.xpos - goal))
        self.delta_y_widget.clear()

    @property
    def size(self):
        rot_x, rot_y = self.rotate(self.raw_size_x, self.raw_size_y,
                                   self.rotation)
        return (int(round(abs(rot_x))), int(round(abs(rot_y))))

    @property
    def size_x(self):
        return self.size[0]

    @property
    def size_y(self):
        return self.size[1]

    @property
    def raw_size_x(self):
        return self.obj.detector.cam.array_size.array_size_x.value

    @property
    def raw_size_y(self):
        return self.obj.detector.cam.array_size.array_size_y.value

    def to_rad(self, deg):
        return deg*pi/180

    def sind(self, deg):
        return sin(self.to_rad(deg))

    def cosd(self, deg):
        return cos(self.to_rad(deg))

    def rotate(self, x, y, deg):
        x2 = x * self.cosd(deg) - y * self.sind(deg)
        y2 = x * self.sind(deg) + y * self.cosd(deg)
        return (x2, y2)
