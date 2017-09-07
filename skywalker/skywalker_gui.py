#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from os import path
from functools import partial
from threading import RLock

import simplejson as json

from bluesky import RunEngine
from bluesky.utils import install_qt_kicker
from bluesky.plans import run_wrapper, stage_wrapper

from pydm import Display
from pydm.PyQt.QtCore import (pyqtSlot, pyqtSignal,
                              QCoreApplication,
                              QPoint, QObject, QEvent)
from pydm.PyQt.QtGui import QDoubleValidator

from pcdsdevices import sim
from pswalker.examples import patch_pims

from pswalker.config import homs_system
from pswalker.plan_stubs import slit_scan_fiducialize
from pswalker.skywalker import lcls_RE, skywalker

logger = logging.getLogger(__name__)
MAX_MIRRORS = 4


def sim_system():
    s = sim.source.Undulator('test_undulator')
    m1 = sim.mirror.OffsetMirror('test_m1h', 'test_m1h_xy',
                                 z=90.510, alpha=0.0014)
    m2 = sim.mirror.OffsetMirror('test_m2h', 'test_m2h_xy',
                                 x=0.0317324, z=101.843, alpha=0.0014)
    xrtm2 = sim.mirror.OffsetMirror('test_xrtm2', 'test_xrtm2_xy',
                                    x=0.0317324, z=200, alpha=0.0014)
    y1 = sim.pim.PIM('test_p3h', x=0.0317324, z=103.660,
                     zero_outside_yag=True)
    y2 = sim.pim.PIM('test_dg3', x=0.0317324, z=375.000,
                     zero_outside_yag=True)
    mecy1 = sim.pim.PIM('test_mecy1', x=0.0317324, z=350,
                        zero_outside_yag=True)
    mfxdg1 = mecy1
    patch_pims([y1, y2], mirrors=[m1, m2], source=s)
    patch_pims([mecy1], mirrors=[xrtm2], source=s)

    config = dict(
        m1h=m1,
        hx2=y1,
        hx2_slits=None,
        m2h=m2,
        dg3=y2,
        dg3_slits=None,
        xrtm2=xrtm2,
        mfxdg1=mfxdg1,
        mfxdg1_slits=None,
    )
    return config


# System mapping of associated devices
def get_system(config, rotation):
    system = dict(
        m1h=dict(mirror=config['m1h'],
                 imager=config['hx2'],
                 slits=config['hx2_slits'],
                 rotation=rotation),
        m2h=dict(mirror=config['m2h'],
                 imager=config['dg3'],
                 slits=config['dg3_slits'],
                 rotation=rotation),
        mfx=dict(mirror=config['xrtm2'],
                 imager=config['mfxdg1'],
                 slits=config['mfxdg1_slits'],
                 rotation=rotation),
        )
    return system


class SkywalkerGui(Display):
    """
    Display class to define all the logic for the skywalker alignment gui.
    Refers to widgets in the .ui file.
    """
    # Alignment mapping of which sets to use for each alignment
    alignments = {'HOMS': [['m1h', 'm2h']],
                  'MFX': [['mfx']],
                  'HOMS + MFX': [['m1h', 'm2h'], ['mfx']]}

    def __init__(self, parent=None, args=None):
        super().__init__(parent=parent, args=args)
        ui = self.ui

        # Configure debug file after all the qt logs
        logging.basicConfig(level=logging.DEBUG,
                            format=('%(asctime)s '
                                    '%(name)-12s '
                                    '%(levelname)-8s '
                                    '%(message)s'),
                            datefmt='%m-%d %H:%M:%S',
                            filename='./skywalker_debug.log',
                            filemode='a')

        # Set self.sim, self.system, self.nominal_config
        self.parse_args(args)

        # Convenient remappings of the system
        self.imager_info = {}
        for info in self.system.values():
            self.imager_info[info['imager'].name] = info

        # Load things
        self.config_cache = {}
        self.cache_config()

        # Load system and alignments into the combo box objects
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
        self.goals_groups = []
        goal_labels = self.get_widget_set('goal_name')
        goal_edits = self.get_widget_set('goal_value')
        slit_checks = self.get_widget_set('slit_check')
        for label, edit, check, img, slit in zip(goal_labels, goal_edits,
                                                 slit_checks,
                                                 self.imagers_padded(),
                                                 self.slits_padded()):
            if img is None:
                name = None
            else:
                name = img.name
            validator = QDoubleValidator(0, 5000, 3)
            goal_group = ValueWidgetGroup(edit, label, checkbox=check,
                                          name=name, cache=self.config_cache,
                                          validator=validator)
            if img is None:
                goal_group.hide()
            elif slit is None:
                goal_group.checkbox.setEnabled(False)
            self.goals_groups.append(goal_group)

        # Initialize image and centroids. Needs goals defined first.
        self.image_group = ImgObjWidget(ui.image, first_imager,
                                        ui.beam_x_value, ui.beam_y_value,
                                        ui.beam_x_delta, ui.beam_y_delta,
                                        ui.readback_imager_title,
                                        self, first_rotation)
        ui.image.setColorMapToPreset('jet')

        # Create the RunEngine that will be used in the alignments.
        # This gives us the ability to pause, etc.
        if self.sim:
            self.RE = RunEngine({})
        else:
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
        procedure_changed = ui.procedure_combo.currentIndexChanged[str]
        procedure_changed.connect(self.on_procedure_combo_changed)

        imager_changed = ui.image_title_combo.currentIndexChanged[str]
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

        save_mirrors_pressed = ui.save_mirrors_button.clicked
        save_mirrors_pressed.connect(self.on_save_mirrors_button)

        save_goals_pressed = ui.save_goals_button.clicked
        save_goals_pressed.connect(self.on_save_goals_button)

        # Set up automatic camera switching
        self.auto_switch_cam = False
        self.cam_lock = RLock()
        for comp_set in self.system.values():
            imager = comp_set['imager']
            imager.subscribe(self.pick_cam, run=False)

        # Store some info about our screen size.
        QApp = QCoreApplication.instance()
        desktop = QApp.desktop()
        geometry = desktop.screenGeometry()
        self.screen_size = (geometry.width(), geometry.height())
        window_qsize = self.window().size()
        self.preferred_size = (window_qsize.width(), window_qsize.height())

        # Setup the post-init hook
        post_init = PostInit(self)
        self.installEventFilter(post_init)
        post_init.post_init.connect(self.on_post_init)

        # Setup the on-screen logger
        console = self.setup_gui_logger()

        # Stop the run if we get closed
        close_dict = dict(RE=self.RE, console=console)
        self.destroyed.connect(partial(SkywalkerGui.on_close, close_dict))

        # Put out the initialization message.
        init_base = 'Skywalker GUI initialized in '
        if self.sim:
            init_str = init_base + 'sim mode.'
        else:
            init_str = init_base + 'live mode.'
        logger.info(init_str)

    def parse_args(self, args):
        logger.debug('Parsing args: %s', args)
        i = 0
        is_live = False
        has_cfg = False
        while i < len(args):
            this_arg = args[i]
            try:
                next_arg = args[i+1]
            except IndexError:
                next_arg = None
            if this_arg == '--live':
                is_live = True
                self.sim = False
                self.system = get_system(homs_system(), 90)
                i += 1
            elif this_arg == '--cfg':
                has_cfg = True
                self.nominal_config = next_arg
                i += 2
                logger.debug('Using config file %s', next_arg)
        if not is_live:
            self.sim = True
            self.system = get_system(sim_system(), 0)
        if not has_cfg:
            self.nominal_config = None

    @pyqtSlot()
    def on_post_init(self):
        x = min(self.preferred_size[0], self.screen_size[0])
        y = min(self.preferred_size[1], self.screen_size[1])
        self.window().resize(x, y)

    # Close handler needs to be a static class method because it is run after
    # the object instance is already completely gone
    @staticmethod
    def on_close(close_dict):
        RE = close_dict['RE']
        console = close_dict['console']
        console.close()
        if RE.state != 'idle':
            RE.abort()

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
        return console

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
        try:
            logger.info('Selecting imager %s', imager_name)
            info = self.imager_info[imager_name]
            image_obj = info['imager']
            slits_obj = info.get('slits')
            rotation = info.get('rotation', 0)
            self.image_obj = image_obj
            self.image_group.change_obj(image_obj, rotation=rotation)
            if slits_obj is not None:
                self.slit_group.change_obj(slits_obj)
        except:
            logger.exception()

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
        try:
            logger.info('Selecting procedure %s', procedure_name)
            self.procedure = procedure_name
            for obj, widgets in zip(self.mirrors_padded(), self.mirror_groups):
                if obj is None:
                    widgets.hide()
                    widgets.change_obj(None)
                else:
                    widgets.change_obj(obj)
                    widgets.show()
            for obj, widgets in zip(self.imagers_padded(), self.goals_groups):
                widgets.save_value()
                widgets.clear()
            for obj, slit, widgets in zip(self.imagers_padded(),
                                          self.slits_padded(),
                                          self.goals_groups):
                if obj is None:
                    widgets.hide()
                else:
                    widgets.setup(name=obj.name)
                    if slit is None:
                        widgets.checkbox.setEnabled(False)
                    else:
                        widgets.checkbox.setEnabled(True)
                    widgets.show()
        except:
            logger.exception()

    @pyqtSlot()
    def on_goal_changed(self):
        """
        Slot for when the user picks a new goal. Updates the goal delta so it
        reflects the new chosen value.
        """
        try:
            self.image_group.update_deltas()
        except:
            logger.exception()

    @pyqtSlot()
    def on_start_button(self):
        """
        Slot for the start button. This begins from an idle state or resumes
        from a paused state.
        """
        try:
            if self.RE.state == 'idle':
                # Check for valid goals
                active_size = len(self.active_system())
                raw_goals = []
                for i, goal in enumerate(self.goals()):
                    if i >= active_size:
                        break
                    elif goal is None:
                        msg = 'Please fill all goal fields before alignment.'
                        logger.info(msg)
                        return
                    raw_goals.append(goal)

                logger.info("Starting %s procedure with goals %s",
                            self.procedure, raw_goals)
                self.auto_switch_cam = True
                alignment = self.alignments[self.procedure]
                for key_set in alignment:
                    yags = [self.system[key]['imager'] for key in key_set]
                    mots = [self.system[key]['mirror'] for key in key_set]
                    rots = [self.system[key].get('rotation')
                            for key in key_set]

                    # Make sure nominal positions are correct
                    for mot in mots:
                        try:
                            mot.nominal_position = self.config_cache[mot.name]
                        except KeyError:
                            pass

                    mot_rbv = 'pitch'
                    # We need to select det_rbv and interpret goals based on
                    # the camera rotation, converting things to the unrotated
                    # coordinates.
                    det_rbv = []
                    goals = []
                    for rot, yag, goal in zip(rots, yags, raw_goals):
                        rot_info = ad_stats_x_axis_rot(yag, rot)
                        det_rbv.append(rot_info['key'])
                        modifier = rot_info['mod_x']
                        if modifier is not None:
                            goal = modifier - goal
                        goals.append(goal)
                    first_steps = 6
                    tolerances = 5
                    average = 100
                    timeout = 600
                    tol_scaling = 8
                    # Temporary fix: undo skywalker's goal mangling.
                    # TODO remove goal mangling from skywalker.
                    goals = [480 - g for g in goals]
                    plan = skywalker(yags, mots, det_rbv, mot_rbv, goals,
                                     first_steps=first_steps,
                                     tolerances=tolerances,
                                     averages=average, timeout=timeout,
                                     sim=self.sim, use_filters=not self.sim,
                                     tol_scaling=tol_scaling)
                    self.RE(plan)
            elif self.RE.state == 'paused':
                logger.info("Resuming procedure.")
                self.auto_switch_cam = True
                self.RE.resume()
        except:
            logger.exception()
        finally:
            self.auto_switch_cam = False

    @pyqtSlot()
    def on_pause_button(self):
        """
        Slot for the pause button. This brings us from the running state to the
        paused state.
        """
        self.auto_switch_cam = False
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
        self.auto_switch_cam = False
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
        try:
            logger.info('Starting slit check process.')
            image_to_check = []
            slits_to_check = []

            # First, check the slit checkboxes.
            for img_obj, slit_obj, goal_group in zip(self.imagers_padded(),
                                                     self.slits_padded(),
                                                     self.goals_groups):
                if slit_obj is not None and goal_group.is_checked:
                    image_to_check.append(img_obj)
                    slits_to_check.append(slit_obj)
            if not slits_to_check:
                logger.info('No valid slits selected!')
                return
            logger.info('Checking the following slits: %s',
                        [slit.name for slit in slits_to_check])

            self.auto_switch_cam = True

            def plan(img, slit, rot, output_obj):
                rot_info = ad_stats_x_axis_rot(img, rot)
                det_rbv = rot_info['key']
                fidu = slit_scan_fiducialize(slit, img, centroid=det_rbv)
                output = yield from fidu
                modifier = rot_info['mod_x']
                if modifier is not None:
                    output = modifier - output
                output_obj[img.name] = output

            results = {}
            for img, slit in zip(image_to_check, slits_to_check):
                rotation = self.imager_info[img.name]['rotation']
                this_plan = plan(img, slit, rotation, results)
                wrapped = run_wrapper(this_plan)
                wrapped = stage_wrapper(wrapped, [img, slit])
                self.RE(wrapped)

            logger.info('Slit scan found the following goals: %s', results)
            if self.ui.slit_fill_check.isChecked():
                logger.info('Filling goal fields automatically.')
                for img, fld in zip(self.imagers_padded(), self.goals_groups):
                    if img is not None:
                        try:
                            fld.value = results[img.name]
                        except KeyError:
                            pass
        except:
            logger.exception()
        finally:
            self.auto_switch_cam = False

    @pyqtSlot()
    def on_save_mirrors_button(self):
        try:
            logger.info('Saving mirror positions.')
            self.save_active_mirrors()
            self.cache_config()
        except:
            logger.exception()

    @pyqtSlot()
    def on_save_goals_button(self):
        try:
            logger.info('Saving goals.')
            self.save_active_goals()
            self.cache_config()
        except:
            logger.exception()

    def pick_cam(self, *args, **kwargs):
        """
        Callback to switch the active imager as the procedures progress.
        """
        if self.auto_switch_cam:
            with self.cam_lock:
                chosen_imager = None
                for img in self.imagers():
                    if img.position == "Unknown":
                        return
                    elif img.position == "IN":
                        chosen_imager = img
                        break
                combo = self.ui.image_title_combo
                if chosen_imager is not None:
                    name = chosen_imager.name
                    if name != combo.currentText():
                        # TODO why does this segfault
                        # logger.info('Automatically switching cam to %s',name)
                        index = self.all_imager_names.index(name)
                        combo.setCurrentIndex(index)

    def read_config(self):
        if self.nominal_config is not None:
            try:
                with open(self.nominal_config, 'r') as f:
                    d = json.load(f)
            except:
                logger.exception('File %s not found!', self.nominal_config)
                return None
            return d
        return None

    def save_config(self, d):
        if self.nominal_config is not None:
            with open(self.nominal_config, 'w') as f:
                json.dump(d, f)

    def cache_config(self):
        d = self.read_config()
        if d is not None:
            self.config_cache.update(d)

    def save_goal(self, goal_group):
        if goal_group.value is None:
            logger.info('No value to save for this goal.')
            return
        d = self.read_config() or {}
        d[goal_group.text()] = goal_group.value
        self.save_config(d)

    def save_active_goals(self):
        text = []
        values = []
        for i, goal_group in enumerate(self.goals_groups):
            if i >= len(self.active_system()):
                break
            val = goal_group.value
            if val is not None:
                values.append(val)
                text.append(goal_group.text())
        d = self.read_config() or {}
        for t, v in zip(text, values):
            d[t] = v
        self.save_config(d)

    def save_mirror(self, mirror_group):
        d = self.read_config() or {}
        mirror = mirror_group.obj
        d[mirror.name] = mirror.position
        self.save_config(d)

    def save_active_mirrors(self):
        d = self.read_config() or {}
        for mirror in self.mirrors():
            d[mirror.name] = mirror.position
        self.save_config(d)

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
        if self.text_widget is not None:
            try:
                msg = self.format(record)
                cursor = self.text_widget.cursorForPosition(QPoint(0, 0))
                cursor.insertText(msg + self.terminator)
            except Exception:
                self.handleError(record)

    def close(self):
        self.text_widget = None


class PostInit(QObject):
    """
    Catch the visibility event for one last sequence of functions after pydm is
    fully initialized, which is later than we can do things inside __init__.
    """
    post_init = pyqtSignal()
    do_it = True

    def eventFilter(self, obj, event):
        if self.do_it and event.type() == QEvent.WindowActivate:
            self.do_it = False
            self.post_init.emit()
            return True
        return False


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

    def text(self):
        if self.label is None:
            return None
        else:
            return self.label.text()


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

    @property
    def is_checked(self):
        if self.checkbox is None:
            return False
        else:
            return self.checkbox.isChecked()


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
        if obj is None:
            name = None
        else:
            name = obj.name
        self.change_pvs(pvnames, name=name, **kwargs)

    def get_pvnames(self, obj):
        """
        Given an object, return the pvnames based on self.attrs
        """
        if obj is None:
            return None
        pvnames = []
        for attr in self.attrs:
            sig = self.nested_getattr(obj, attr)
            try:
                pvnames.append(sig.pvname)
            except AttributeError:
                pvnames.append(None)
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
        self.xpos = 0
        self.ypos = 0
        attrs = ['detector.image2.width',
                 'detector.image2.array_data']
        super().__init__([img_widget], attrs, img_obj, label=label,
                         rotation=rotation)

    def setup(self, *, pvnames, name=None, rotation=0, **kwargs):
        BaseWidgetGroup.setup(self, name=name)
        try:
            self.cent_x.clear_sub(self.update_centroid)
            self.cent_y.clear_sub(self.update_centroid)
        except (AttributeError, ValueError):
            pass
        self.rotation = rotation
        rot_info = ad_stats_x_axis_rot(self.obj, rotation)
        self.size_x = rot_info['x_size'].value
        self.size_y = rot_info['y_size'].value
        self.cent_x = rot_info['x_cent']
        self.cent_y = rot_info['y_cent']
        self.mod_x = rot_info['mod_x']
        self.mod_y = rot_info['mod_y']
        img_widget = self.widgets[0]
        width_pv = pvnames[0]
        image_pv = pvnames[1]
        image_item = img_widget.getImageItem()
        image_item.setTransformOriginPoint(self.raw_size_x//2,
                                           self.raw_size_y//2)
        image_item.setRotation(rotation)
        view = img_widget.getView()
        view.setRange(xRange=(0, self.raw_size_x),
                      yRange=(0, self.raw_size_y),
                      padding=0.0)
        view.setLimits(xMin=0, xMax=self.raw_size_x,
                       yMin=0, yMax=self.raw_size_y)
        img_widget.resetImageChannel()
        img_widget.resetWidthChannel()
        img_widget.setMinimumWidth(self.size_x)
        img_widget.setMinimumHeight(self.size_y)
        if width_pv is None:
            width_channel = ''
        else:
            width_channel = self.protocol + width_pv
        if image_pv is None:
            image_channel = ''
        else:
            image_channel = self.protocol + image_pv
        img_widget.setWidthChannel(width_channel)
        img_widget.setImageChannel(image_channel)
        self.cent_x.subscribe(self.update_centroid)
        self.cent_y.subscribe(self.update_centroid)

    def update_centroid(self, *args, **kwargs):
        xpos = self.cent_x.value
        ypos = self.cent_y.value
        if self.mod_x is not None and xpos not in (0, None):
            xpos = self.mod_x - xpos
        if self.mod_y is not None and ypos not in (0, None):
            ypos = self.mod_y - ypos
        if xpos is not None:
            self.cent_x_widget.setText("{:.1f}".format(xpos))
            self.xpos = xpos
        if ypos is not None:
            self.cent_y_widget.setText("{:.1f}".format(ypos))
            self.ypos = ypos
        self.update_deltas()

    def update_deltas(self, *args, **kwargs):
        goal = self.goals_source.goal()
        if goal is None:
            self.delta_x_widget.clear()
        else:
            self.delta_x_widget.setText("{:.1f}".format(self.xpos - goal))
        self.delta_y_widget.clear()

    @property
    def size(self):
        return (self.size_x, self.size_y)

    @property
    def raw_size_x(self):
        return self.obj.detector.cam.array_size.array_size_x.value

    @property
    def raw_size_y(self):
        return self.obj.detector.cam.array_size.array_size_y.value


def ad_stats_x_axis_rot(imager, rotation):
    """
    Helper function to pick the correct key and modify a value for a rotated
    areadetector camera with a stats plugin, where you care about the x axis of
    the centroid.

    Returns
    -------
    output: dict
        ['key']: 'detector_stats2_centroid_x' or 'detector_stats2_centroid_y'
        ['mod_x']: int or None. If int, you get a true value by doing int-value
        ['mod_y']: int or None. If int, you get a true value by doing int-value
        ['x_cent']: Signal associated with the x centroid
        ['y_cent']: Signal associated with the y centroid
        ['x_size']: Signal associated with the x size
        ['y_size']: Signal associated with the y size
    """
    det_key_base = 'detector_stats2_centroid_'
    sizes = imager.detector.cam.array_size
    centroid = imager.detector.stats2.centroid
    rotation = rotation % 360
    if rotation % 180 == 0:
        det_key = det_key_base + 'x'
        x_size = sizes.array_size_x
        y_size = sizes.array_size_y
        x_cent = centroid.x
        y_cent = centroid.y
    else:
        det_key = det_key_base + 'y'
        x_size = sizes.array_size_y
        y_size = sizes.array_size_x
        x_cent = centroid.y
        y_cent = centroid.x
    if rotation == 0:
        mod_x = None
        mod_y = None
    elif rotation == 90:
        mod_x = y_size.value
        mod_y = None
    elif rotation == 180:
        mod_x = x_size.value
        mod_y = y_size.value
    else:
        mod_x = None
        mod_y = x_size.value
    return dict(key=det_key, mod_x=mod_x, mod_y=mod_y, x_cent=x_cent,
                y_cent=y_cent, x_size=x_size, y_size=y_size)


def debug_log_pydm_connections():
    QApp = QCoreApplication.instance()
    plugins = QApp.plugins
    ca_plugin = plugins['ca']
    connections = ca_plugin.connections
    counts = {k: v.listener_count for k, v in connections.items()}
    logger.debug('Pydm connection counts: %s', counts)
