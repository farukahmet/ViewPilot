# operators.py

import bpy
import time
import traceback
from contextlib import contextmanager
from mathutils import Vector, Quaternion

from . import utils, debug_tools
from .utils import (
    get_current_view_state,
    states_are_similar,
    add_to_history,
    history_go_back,
    history_go_forward,
    get_view_location,
)
from .state_controller import get_controller, UpdateSource, LockPriority
from .preferences import get_preferences
from .thumbnail_generator import generate_thumbnail, delete_thumbnail
from .modal_gallery import VIEW3D_OT_thumbnail_gallery


# ========================================================================
# VIEW HISTORY OPERATORS
# ========================================================================

class VIEW3D_OT_view_history_monitor(bpy.types.Operator):
    """Background monitor to save view state after movement settles."""
    bl_idname = "view3d.view_history_monitor"
    bl_label = "View History Monitor"
    
    _timer = None
    last_known_state = None
    is_moving = False
    settle_start_time = 0.0
    was_in_camera_view = False  # Track camera view transitions
    last_selection_hash = None  # Track selection changes for orbit mode
    last_orbit_mode = False  # Track orbit mode transitions
    last_scene_count = 0  # Track scene count for UUID duplicate detection
    last_view_layer_counts = {}  # Track view layer count per scene {scene_name: count}
    last_camera_count = 0  # Track camera count for dropdown sync
    last_maintenance_time = 0.0  # Last periodic maintenance timestamp
    
    # Settings
    CHECK_INTERVAL = 0.1
    MAINTENANCE_INTERVAL_ACTIVE = 0.5
    MAINTENANCE_INTERVAL_IDLE = 2.0

    def _pass_through_tick(self, tick_start=None):
        if tick_start is not None:
            debug_tools.add_timing(
                "history.monitor.tick.total",
                (time.perf_counter() - tick_start) * 1000.0
            )
        return {'PASS_THROUGH'}

    def _run_periodic_maintenance(self, context):
        """Run lower-frequency checks that do not need to execute every timer tick."""
        from . import data_storage

        # --- SCENE COUNT CHANGE DETECTION ---
        current_scene_count = len(bpy.data.scenes)
        if current_scene_count != self.last_scene_count:
            if self.last_scene_count > 0:  # Skip initial detection
                fixed = data_storage.fix_duplicate_scene_uuids()
                if fixed:
                    debug_tools.log(f"fixed {fixed} duplicate scene UUID(s)")
                # Also check for new scenes needing UUIDs
                for scene in bpy.data.scenes:
                    data_storage.ensure_scene_uuid(scene)
            self.last_scene_count = current_scene_count

        # --- VIEW LAYER COUNT CHANGE DETECTION ---
        current_scene_names = set()
        for scene in bpy.data.scenes:
            current_scene_names.add(scene.name)
            current_vl_count = len(scene.view_layers)
            last_vl_count = self.last_view_layer_counts.get(scene.name)

            # Initialize baseline without triggering duplicate-fix logic.
            if last_vl_count is None:
                self.last_view_layer_counts[scene.name] = current_vl_count
                continue

            if current_vl_count != last_vl_count:
                fixed = data_storage.fix_duplicate_view_layer_uuids(scene)
                if fixed:
                    debug_tools.log(f"fixed {fixed} duplicate view layer UUID(s) in '{scene.name}'")
                # Also ensure new view layers have UUIDs
                for vl in scene.view_layers:
                    data_storage.ensure_view_layer_uuid(vl)
                self.last_view_layer_counts[scene.name] = current_vl_count

        # Remove stale tracking entries for scenes that no longer exist.
        for scene_name in tuple(self.last_view_layer_counts.keys()):
            if scene_name not in current_scene_names:
                del self.last_view_layer_counts[scene_name]

        # --- CAMERA COUNT CHANGE DETECTION ---
        camera_count = sum(1 for obj in context.scene.objects if obj.type == 'CAMERA')
        if camera_count != self.last_camera_count:
            self.last_camera_count = camera_count
            # Resync camera dropdown to current scene camera.
            props = context.scene.viewpilot
            active_cam = context.scene.camera
            if active_cam:
                try:
                    props.camera_enum = active_cam.name
                except TypeError:
                    pass  # Enum items not yet populated

    def _current_maintenance_interval(self, context):
        """Return maintenance cadence based on current activity level."""
        try:
            props = context.scene.viewpilot
            if (
                self.is_moving or
                self.was_in_camera_view or
                props.orbit_around_selection or
                props.keep_camera_active
            ):
                return self.MAINTENANCE_INTERVAL_ACTIVE
        except (AttributeError, ReferenceError, RuntimeError):
            pass
        return self.MAINTENANCE_INTERVAL_IDLE

    def _maybe_run_periodic_maintenance(self, context, now):
        interval = self._current_maintenance_interval(context)
        if (now - self.last_maintenance_time) < interval:
            debug_tools.inc("history.monitor.maintenance.skipped")
            if interval == self.MAINTENANCE_INTERVAL_ACTIVE:
                debug_tools.inc("history.monitor.maintenance.skipped.active")
            else:
                debug_tools.inc("history.monitor.maintenance.skipped.idle")
            return
        debug_tools.inc("history.monitor.maintenance.run")
        if interval == self.MAINTENANCE_INTERVAL_ACTIVE:
            debug_tools.inc("history.monitor.maintenance.run.active")
        else:
            debug_tools.inc("history.monitor.maintenance.run.idle")
        self.last_maintenance_time = now
        with debug_tools.timed("history.monitor.maintenance.total"):
            self._run_periodic_maintenance(context)
    
    def modal(self, context, event):
        if event.type == 'TIMER':
            debug_tools.inc("history.monitor.tick")
            tick_start = time.perf_counter()

            controller = get_controller()
            now = time.time()
            self._maybe_run_periodic_maintenance(context, now)
            
            # Auto-initialize if needed
            if not context.scene.viewpilot.init_complete:
                context.scene.viewpilot.reinitialize_from_context(context)

            with debug_tools.timed("history.monitor.capture_state.total"):
                current_state = get_current_view_state(context)
            if not current_state:
                debug_tools.inc("history.monitor.tick.no_state")
                return self._pass_through_tick(tick_start)
            
            # Check if we're in a grace period
            # During grace period: DON'T reinitialize (to avoid fighting slider input)
            # But DO continue tracking movement so history can be recorded after settle
            in_grace = controller.is_in_grace_period()
            props = context.scene.viewpilot
            is_in_camera = current_state.get('view_perspective') == 'CAMERA'

            # Orbit mode can be enabled while idle; seed selection baseline on transition.
            if props.orbit_around_selection and not self.last_orbit_mode:
                current_sel = frozenset(obj.name for obj in context.selected_objects)
                self.last_selection_hash = hash(current_sel)
            self.last_orbit_mode = bool(props.orbit_around_selection)

            # Fast path: unchanged idle ticks with no special sync modes enabled.
            if (
                self.last_known_state is not None and
                not self.is_moving and
                not in_grace and
                not self.was_in_camera_view and
                not is_in_camera and
                not props.orbit_around_selection and
                not props.keep_camera_active and
                states_are_similar(current_state, self.last_known_state)
            ):
                debug_tools.inc("history.monitor.tick.fast_path_idle")
                return self._pass_through_tick(tick_start)
            
            # --- SELECTION CHANGE DETECTION ---
            # If orbit mode is active and selection changes, disable orbit
            if props.orbit_around_selection:
                # Calculate hash of current selection (names of selected objects)
                current_sel = frozenset(obj.name for obj in context.selected_objects)
                current_hash = hash(current_sel)
                
                if self.last_selection_hash is not None and current_hash != self.last_selection_hash:
                    # Selection changed! Disable orbit mode
                    props['orbit_around_selection'] = False
                    props['orbit_initialized'] = False
                    debug_tools.log("orbit mode auto-disabled (selection changed)")
                
                self.last_selection_hash = current_hash
            else:
                # Keep tracking selection even when orbit is off
                current_sel = frozenset(obj.name for obj in context.selected_objects)
                self.last_selection_hash = hash(current_sel)
            
            # --- KEEP CAMERA ACTIVE MODE DETECTION ---
            # If mode is on but camera is no longer active, turn off the mode
            if props.keep_camera_active:
                cam = context.scene.camera
                if cam:
                    is_cam_active = (context.view_layer.objects.active == cam)
                    if not is_cam_active:
                        # External selection change - disable the mode
                        props['keep_camera_active'] = False
            
            # Handle camera view - sync UI when camera properties change externally
            if is_in_camera:
                cam = context.scene.camera
                if cam:
                    # Check if camera properties have changed externally
                    cam_loc = cam.location
                    cam_rot = cam.rotation_euler
                    
                    # Compare with ViewPilot's tracked values (with small threshold)
                    loc_changed = (abs(props.loc_x - cam_loc.x) > 0.0001 or
                                   abs(props.loc_y - cam_loc.y) > 0.0001 or
                                   abs(props.loc_z - cam_loc.z) > 0.0001)
                    rot_changed = (abs(props.rot_x - cam_rot.x) > 0.0001 or
                                   abs(props.rot_y - cam_rot.y) > 0.0001 or
                                   abs(props.rot_z - cam_rot.z) > 0.0001)
                    
                    # Reinitialize when first entering OR when camera changed externally
                    # Skip if an update is in progress (grace period active)
                    if not self.was_in_camera_view or (loc_changed or rot_changed):
                        if not in_grace:
                            context.scene.viewpilot.reinitialize_from_context(context)
                
                self.was_in_camera_view = True
                self.last_known_state = current_state
                self.is_moving = False
                return self._pass_through_tick(tick_start)
            else:
                # Just exited camera view - reinitialize to viewport mode
                if self.was_in_camera_view and not in_grace:
                    context.scene.viewpilot.reinitialize_from_context(context)
                self.was_in_camera_view = False
            
            # Initialize if empty (and not in camera view)
            if self.last_known_state is None:
                self.last_known_state = current_state
                debug_tools.inc("history.monitor.history.seed")
                add_to_history(current_state)
                return self._pass_through_tick(tick_start)
            
            # Check for difference
            if not states_are_similar(current_state, self.last_known_state):
                
                # Update UI properties to match the new viewport state (Live Sync)
                # BUT skip if we're in a grace period (property update in progress)
                if not in_grace:
                    try:
                        context.scene.viewpilot.reinitialize_from_context(context)
                    except (RuntimeError, ReferenceError, AttributeError, ValueError) as error:
                        debug_tools.log(f"monitor reinitialize failed outside grace period: {error}")

                    # Detected movement away from the current state.
                    # If we are currently "on" a saved view, mark it as ghost (last active) and reset current index
                    if context.scene.saved_views_index != -1:
                        context.scene.viewpilot.last_active_view_index = context.scene.saved_views_index
                        context.scene.saved_views_index = -1
                        try:
                            with _suppress_saved_view_enum_load():
                                context.scene.viewpilot.saved_views_enum = 'NONE'
                                _set_panel_gallery_enum_safe(context, 'NONE')
                        except (TypeError, ValueError, RuntimeError, AttributeError) as error:
                            debug_tools.log(f"monitor ghost-mode enum clear failed: {error}")
                else:
                    # We are in a grace period.
                    
                    # Special Case: USER_DRAG (Panel Sliders)
                    # If the user is dragging the UI sliders, we ARE modifying the state.
                    # We should NOT reinitialize (fight the user), but we SHOULD trigger Ghost Mode
                    # because the view is no longer the pristine saved view.
                    if controller.grace_period_source == UpdateSource.USER_DRAG:
                        if context.scene.saved_views_index != -1:
                             context.scene.viewpilot.last_active_view_index = context.scene.saved_views_index
                             context.scene.saved_views_index = -1
                             try:
                                 with _suppress_saved_view_enum_load():
                                     context.scene.viewpilot.saved_views_enum = 'NONE'
                                     _set_panel_gallery_enum_safe(context, 'NONE')
                             except (TypeError, ValueError, RuntimeError, AttributeError) as error:
                                 debug_tools.log(f"monitor USER_DRAG enum clear failed: {error}")

                    # If this is due to VIEW_RESTORE (loading a view), we should accept this new state
                    # as the baseline immediately to prevent "Ghost View" triggering once grace ends.
                    if controller.grace_period_source == UpdateSource.VIEW_RESTORE:
                        self.last_known_state = current_state
                        
                # Check if this change is just us restoring a history state within grace period
                # OR if the state is actually identical (floating point drift)
                if states_are_similar(current_state, self.last_known_state):
                    # False alarm or drift
                    self.is_moving = False
                    return self._pass_through_tick(tick_start)

                # Check if this change is just us restoring a history state
                if utils.view_history_index != -1 and utils.view_history:
                    # Safely get the state at the current index
                    if 0 <= utils.view_history_index < len(utils.view_history):
                        target_state = utils.view_history[utils.view_history_index]
                        if states_are_similar(current_state, target_state):
                            # We just restored this state. Update tracker but DON'T save as new.
                            self.last_known_state = current_state
                            self.is_moving = False
                            return self._pass_through_tick(tick_start)

                # --- AUTO-DISABLE ORBIT MODE ON EXTERNAL MOVEMENT ---
                # Only disable orbit if the camera POSITION or ROTATION actually changed.
                # Ignore perspective mode changes (ortho/persp toggle).
                props = context.scene.viewpilot
                if props.orbit_around_selection and not in_grace:
                    # Check if position/rotation actually changed (not just perspective mode)
                    pos_changed = (current_state['view_location'] - self.last_known_state['view_location']).length_squared > 0.0001
                    rot_diff = abs(current_state['view_rotation'].dot(self.last_known_state['view_rotation']))
                    rot_changed = rot_diff < 0.9999
                    
                    if pos_changed or rot_changed:
                        props['orbit_around_selection'] = False
                        props['orbit_initialized'] = False
                        debug_tools.log("orbit mode auto-disabled (external movement detected)")

                # Movement detected!
                debug_tools.inc("history.monitor.movement.detected")
                self.is_moving = True
                self.settle_start_time = now
                self.last_known_state = current_state
            
            elif self.is_moving:
                # No movement, but we were moving recently. Check settle timer.
                try:
                    settle_delay = get_preferences().settle_delay
                except (AttributeError, RuntimeError, ValueError):
                    settle_delay = 0.3
                if (now - self.settle_start_time) > settle_delay:
                    # Check if we should record this to history
                    # (suppressed during VIEW_RESTORE, HISTORY_NAV, or grace periods)
                    if controller.should_record_history():
                        debug_tools.inc("history.monitor.history.record_allowed")
                        debug_tools.log(f"history saved (size={len(utils.view_history)})")
                        with debug_tools.timed("history.monitor.history_add.total"):
                            add_to_history(current_state)
                        debug_tools.inc("history.monitor.history.add_called")
                    else:
                        debug_tools.inc("history.monitor.history.record_suppressed")
                    self.is_moving = False

            return self._pass_through_tick(tick_start)
                    
        return {'PASS_THROUGH'}
    
    def invoke(self, context, event):
        if utils.monitor_running:
            return {'CANCELLED'}
        utils.monitor_running = True
        self.last_known_state = None
        self.is_moving = False
        self.settle_start_time = 0.0
        self.was_in_camera_view = False
        self.last_selection_hash = None
        self.last_orbit_mode = bool(context.scene.viewpilot.orbit_around_selection)
        self.last_scene_count = len(bpy.data.scenes)
        self.last_view_layer_counts = {scene.name: len(scene.view_layers) for scene in bpy.data.scenes}
        self.last_camera_count = sum(1 for obj in context.scene.objects if obj.type == 'CAMERA')
        self.last_maintenance_time = 0.0
        self._timer = context.window_manager.event_timer_add(self.CHECK_INTERVAL, window=context.window)
        context.window_manager.modal_handler_add(self)
        debug_tools.log("view history monitor started")
        return {'RUNNING_MODAL'}
    
    def cancel(self, context):
        utils.monitor_running = False
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
        debug_tools.log("view history monitor stopped")


class VIEW3D_OT_view_history_back(bpy.types.Operator):
    """Go back in view history"""
    bl_idname = "view3d.view_history_back"
    bl_label = "View History Back"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.space_data and context.space_data.type == 'VIEW_3D'
    
    def execute(self, context):
        # Exit camera view if we're in it - history is for viewport navigation
        if context.region_data.view_perspective == 'CAMERA':
            bpy.ops.view3d.view_camera()  # Toggle out of camera view
        
        state = history_go_back(context)
        if state:
            self.report({'INFO'}, f"◄ History {utils.view_history_index + 1}/{len(utils.view_history)}")
            # Sync UI properties to new view state
            try:
                context.scene.viewpilot.reinitialize_from_context(context)
            except (RuntimeError, ReferenceError, AttributeError, ValueError) as e:
                debug_tools.log(f"error syncing ViewPilot properties: {e}")
            return {'FINISHED'}
        else:
            if not utils.view_history:
                self.report({'WARNING'}, "No history")
            else:
                self.report({'INFO'}, "◄ Reached start of history")
            return {'FINISHED'}


class VIEW3D_OT_view_history_forward(bpy.types.Operator):
    """Go forward in view history"""
    bl_idname = "view3d.view_history_forward"
    bl_label = "View History Forward"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.space_data and context.space_data.type == 'VIEW_3D'
    
    def execute(self, context):
        # Exit camera view if we're in it - history is for viewport navigation
        if context.region_data.view_perspective == 'CAMERA':
            bpy.ops.view3d.view_camera()  # Toggle out of camera view
        
        state = history_go_forward(context)
        if state:
            self.report({'INFO'}, f"► History {utils.view_history_index + 1}/{len(utils.view_history)}")
            # Sync UI properties to new view state
            try:
                context.scene.viewpilot.reinitialize_from_context(context)
            except (RuntimeError, ReferenceError, AttributeError, ValueError) as e:
                debug_tools.log(f"error syncing ViewPilot properties: {e}")
            return {'FINISHED'}
        else:
            # Show current position even when can't go forward
            display_index = utils.view_history_index + 1 if utils.view_history_index != -1 else len(utils.view_history)
            self.report({'INFO'}, f"► History {display_index}/{len(utils.view_history)}")
            return {'FINISHED'}


# ========================================================================
# SYNC OPERATOR (for N-Panel/Popover initialization)
# ========================================================================

class VIEW3D_OT_sync_viewpilot(bpy.types.Operator):
    """Sync ViewPilot controls with current viewport state"""
    bl_idname = "view3d.sync_viewpilot"
    bl_label = "Sync to View"
    bl_description = "Initialize or refresh ViewPilot controls from current view"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    def execute(self, context):
        try:
            context.scene.viewpilot.reinitialize_from_context(context)
            self.report({'INFO'}, "ViewPilot Synced")
            return {'FINISHED'}
        except (RuntimeError, ReferenceError, AttributeError, ValueError) as e:
            self.report({'ERROR'}, f"Sync failed: {str(e)}")
            return {'CANCELLED'}


class VIEW3D_OT_open_viewpilot_prefs(bpy.types.Operator):
    """Toggle ViewPilot addon preferences"""
    bl_idname = "view3d.open_viewpilot_prefs"
    bl_label = "ViewPilot Preferences"
    bl_description = "Open ViewPilot addon preferences"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    def execute(self, context):
        # Open preferences window, switch to addons tab, filter by addon name, expand panel
        bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
        context.preferences.active_section = 'ADDONS'
        context.window_manager.addon_search = "ViewPilot"
        bpy.ops.preferences.addon_expand(module=__package__)
        return {'FINISHED'}

# ========================================================================
# CAMERA UTILITY OPERATORS
# ========================================================================

class VIEW3D_OT_toggle_camera_selection(bpy.types.Operator):
    """Toggle 'Keep Camera Active' mode - camera stays selected as active object"""
    bl_idname = "view3d.toggle_camera_selection"
    bl_label = "Keep Camera Active"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.scene.camera is not None
    
    def execute(self, context):
        props = context.scene.viewpilot
        cam = context.scene.camera
        
        # Toggle the mode
        props.keep_camera_active = not props.keep_camera_active
        
        if props.keep_camera_active:
            # Mode ON: Select and make camera active
            cam.select_set(True)
            context.view_layer.objects.active = cam
        else:
            # Mode OFF: Deselect camera
            cam.select_set(False)
            if context.view_layer.objects.active == cam:
                context.view_layer.objects.active = None
        
        # Sync camera dropdown to current scene camera (fixes blank dropdown after camera deletion)
        try:
            props.camera_enum = cam.name
        except TypeError:
            pass  # Enum items not yet populated
        
        return {'FINISHED'}


class VIEW3D_OT_toggle_camera_name(bpy.types.Operator):
    """Toggle camera name visibility in viewport"""
    bl_idname = "view3d.toggle_camera_name"
    bl_label = "Toggle Camera Name"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.scene.camera is not None
    
    def execute(self, context):
        cam_obj = context.scene.camera
        cam_data = cam_obj.data
        
        # Toggle both properties together
        new_state = not cam_data.show_name
        cam_data.show_name = new_state
        cam_obj.show_name = new_state
        return {'FINISHED'}


class VIEW3D_OT_exit_camera_view(bpy.types.Operator):
    """Exit camera view (Numpad 0)"""
    bl_idname = "view3d.exit_camera_view"
    bl_label = "Exit Camera View"
    bl_options = {'REGISTER', 'UNDO'}
    
    clear_camera: bpy.props.BoolProperty(
        name="Clear Camera",
        description="Also clear the scene camera after exiting",
        default=False
    )
    
    @classmethod
    def poll(cls, context):
        return (context.space_data and 
                context.space_data.type == 'VIEW_3D' and
                context.region_data and
                context.region_data.view_perspective == 'CAMERA')
    
    def execute(self, context):
        # Use Blender's view_camera operator - this properly restores the pre-camera viewport state
        bpy.ops.view3d.view_camera()
        
        # Optionally clear the scene camera
        if self.clear_camera:
            context.scene.camera = None
        
        # Sync UI properties immediately
        try:
            context.scene.viewpilot.reinitialize_from_context(context)
        except (RuntimeError, ReferenceError, AttributeError, ValueError) as error:
            debug_tools.log(f"exit_camera_view reinitialize failed: {error}")
        
        return {'FINISHED'}


class VIEW3D_OT_create_camera_from_view(bpy.types.Operator):
    """Create a new camera matching the current viewport look"""
    bl_idname = "view3d.create_camera_from_view"
    bl_label = "Create Camera from Current"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.space_data and context.space_data.type == 'VIEW_3D'
    
    def execute(self, context):
        from .utils import get_view_location, create_camera_from_view_data
        
        space = context.space_data
        region = context.region_data
        
        # Get preferences with fallbacks
        try:
            prefs = get_preferences()
            passepartout = prefs.camera_passepartout
            show_passepartout = prefs.show_passepartout
            show_name = prefs.show_camera_name
            show_sensor = prefs.show_camera_sensor
            use_collection = prefs.use_camera_collection
            make_active = prefs.make_camera_active
            camera_name_prefix = prefs.camera_name_prefix
            collection_name = prefs.camera_collection_name
            collection_color = prefs.camera_collection_color
        except (AttributeError, RuntimeError, ValueError):
            passepartout = 0.95
            show_passepartout = True
            show_name = True
            show_sensor = True
            use_collection = True
            make_active = True
            camera_name_prefix = "ViewCam"
            collection_name = "ViewPilot"
            collection_color = 'COLOR_04'
        
        # Use active view name if available, otherwise just the prefix
        camera_name = camera_name_prefix
        active_view_index = context.scene.saved_views_index
        if active_view_index >= 0 and active_view_index < len(context.scene.saved_views):
            view_name = context.scene.saved_views[active_view_index].name
            camera_name = f"{camera_name_prefix} [{view_name}]"
        
        # Get current view data
        eye_pos = get_view_location(context)
        
        # Create camera using centralized utility
        cam_obj = create_camera_from_view_data(
            context=context,
            name=camera_name,
            location=eye_pos,
            rotation=region.view_rotation,
            is_perspective=region.is_perspective,
            lens=space.lens,
            distance=region.view_distance,
            clip_start=space.clip_start,
            clip_end=space.clip_end,
            passepartout=passepartout,
            show_passepartout=show_passepartout,
            show_name=show_name,
            show_sensor=show_sensor,
            use_collection=use_collection,
            collection_name=collection_name,
            collection_color=collection_color
        )
        cam_data = cam_obj.data
        
        # Make it the active camera if enabled
        if make_active:
            context.view_layer.objects.active = cam_obj
            cam_obj.select_set(True)
            context.scene.camera = cam_obj
            
            # Swap render resolution if orientation doesn't match sensor
            # This prevents the view from jumping when centering the camera
            render = context.scene.render
            sensor_is_horizontal = cam_data.sensor_fit == 'HORIZONTAL' or (
                cam_data.sensor_fit == 'AUTO' and cam_data.sensor_width >= cam_data.sensor_height
            )
            render_is_horizontal = render.resolution_x >= render.resolution_y
            
            rotated_resolution = False
            if sensor_is_horizontal != render_is_horizontal:
                # Swap resolution to match sensor orientation
                render.resolution_x, render.resolution_y = render.resolution_y, render.resolution_x
                rotated_resolution = True
            
            bpy.ops.view3d.view_camera()  # Use operator to properly store previous view state
            bpy.ops.view3d.view_center_camera()  # Center/zoom to fit camera frame
            
            if rotated_resolution:
                self.report({'INFO'}, "New Camera — Output resolution rotated to fit sensor")
            else:
                self.report({'INFO'}, "Created new Active Camera")
        else:
            self.report({'INFO'}, "Created Camera from View")
        
        return {'FINISHED'}


class VIEW3D_OT_dolly_to_obstacle(bpy.types.Operator):
    """Move camera backward until it hits an obstacle (useful for maximizing view in tight spaces)"""
    bl_idname = "view3d.dolly_to_obstacle"
    bl_label = "Dolly to Obstacle"
    bl_options = {'REGISTER', 'UNDO'}
    
    offset: bpy.props.FloatProperty(
        name="Offset",
        description="Distance to keep from the obstacle",
        default=0.05,
        min=0.001,
        max=1.0,
        unit='LENGTH'
    )
    
    @classmethod
    def poll(cls, context):
        # Available in 3D view (camera view or viewport)
        if not context.space_data or context.space_data.type != 'VIEW_3D':
            return False
        return True
    
    def execute(self, context):
        from mathutils import Vector
        
        space = context.space_data
        region = space.region_3d
        
        # Determine if we're in camera view or viewport mode
        in_camera_view = region.view_perspective == 'CAMERA' and context.scene.camera
        
        if in_camera_view:
            # Camera mode: move the actual camera object
            cam = context.scene.camera
            cam_pos = cam.matrix_world.translation.copy()
            # Camera looks down its negative local Z, so backward is positive local Z
            cam_backward = cam.matrix_world.to_3x3() @ Vector((0, 0, 1))
            cam_backward.normalize()
        else:
            # Viewport mode: move the viewport "eye" position
            cam_pos = get_view_location(context)
            # Backward is opposite of view direction
            cam_backward = region.view_rotation @ Vector((0, 0, 1))
            cam_backward.normalize()
        
        # Calculate max ray distance from scene bounding box (capped at 1km)
        max_distance = self._get_scene_diagonal(context)
        max_distance = min(max_distance, 1000.0)  # Cap at 1km
        
        # Use depsgraph for evaluated objects (visible meshes only)
        depsgraph = context.evaluated_depsgraph_get()
        
        # Raycast backward from camera/viewport
        result, location, normal, index, obj, matrix = context.scene.ray_cast(
            depsgraph,
            cam_pos,
            cam_backward,
            distance=max_distance
        )
        
        if result:
            # Calculate new position with offset
            hit_distance = (location - cam_pos).length
            new_distance = hit_distance - self.offset
            
            if new_distance > 0:
                new_pos = cam_pos + cam_backward * new_distance
                
                if in_camera_view:
                    cam.location = new_pos
                else:
                    # For viewport: compute new view_location from new eye position
                    # view_location = eye_position - (rotation @ view_z) * view_distance
                    from mathutils import Vector
                    view_z = Vector((0.0, 0.0, 1.0))
                    offset = (region.view_rotation @ view_z) * region.view_distance
                    region.view_location = new_pos - offset
                
                self.report({'INFO'}, f"Moved back {new_distance:.2f} units to obstacle")
            else:
                self.report({'WARNING'}, "Already at or past obstacle")
        else:
            self.report({'INFO'}, "No obstacle found behind")
        
        return {'FINISHED'}
    
    def _get_scene_diagonal(self, context):
        """Calculate the diagonal of the bounding box containing all visible mesh objects."""
        from mathutils import Vector
        
        min_corner = Vector((float('inf'), float('inf'), float('inf')))
        max_corner = Vector((float('-inf'), float('-inf'), float('-inf')))
        found_mesh = False
        
        for obj in context.visible_objects:
            if obj.type != 'MESH':
                continue
            
            found_mesh = True
            # Get world-space bounding box corners
            bbox_corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
            
            for corner in bbox_corners:
                min_corner.x = min(min_corner.x, corner.x)
                min_corner.y = min(min_corner.y, corner.y)
                min_corner.z = min(min_corner.z, corner.z)
                max_corner.x = max(max_corner.x, corner.x)
                max_corner.y = max(max_corner.y, corner.y)
                max_corner.z = max(max_corner.z, corner.z)
        
        if not found_mesh:
            return 100.0  # Default fallback
        
        # Return diagonal length
        return (max_corner - min_corner).length


# ========================================================================
# SAVED VIEWS OPERATORS
# ========================================================================

@contextmanager
def _suppress_saved_view_enum_load():
    """Temporarily suppress enum load callbacks and always restore prior state."""
    controller = get_controller()
    prev_skip = controller.skip_enum_load
    controller.skip_enum_load = True
    try:
        yield
    finally:
        controller.skip_enum_load = prev_skip


def _set_panel_gallery_enum_safe(context, preferred_value=None):
    """Delegate to the canonical enum-safe setter in properties.py."""
    try:
        from .properties import _set_panel_gallery_enum_safe as _set_safe
        return _set_safe(context.scene.viewpilot, preferred_value)
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
        return False


def _sync_saved_view_enums_safe(context, enum_value):
    """Synchronize dropdown + panel enums without assuming panel supports NONE."""
    props = context.scene.viewpilot
    try:
        props.saved_views_enum = enum_value
    except (AttributeError, TypeError, ValueError, RuntimeError):
        pass
    _set_panel_gallery_enum_safe(context, enum_value)


def _handle_storage_invalid(context, reporter, action_label="save view"):
    """Report invalid storage and prompt user with overwrite/cancel options."""
    reporter.report({'ERROR'}, f"Can't {action_label}: ViewPilot storage is corrupted")
    try:
        bpy.ops.viewpilot.recover_storage_overwrite('INVOKE_DEFAULT', action_label=action_label)
    except RuntimeError as error:
        reporter.report({'WARNING'}, "Recovery dialog unavailable (see console)")
        debug_tools.log(f"failed to show recovery dialog (runtime): {error}")
    except (TypeError, AttributeError, ValueError) as error:
        reporter.report({'WARNING'}, "Recovery dialog unavailable (see console)")
        debug_tools.log(f"unexpected error showing recovery dialog: {error}")


def _refresh_saved_views_ui(include_modal_gallery=True):
    """Invalidate saved-view UI caches and optionally refresh modal gallery."""
    try:
        from .properties import invalidate_saved_views_ui_caches
        invalidate_saved_views_ui_caches()
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError) as error:
        debug_tools.log(f"saved-view UI cache invalidation fallback path: {error}")
        try:
            from .preview_manager import invalidate_panel_gallery_cache
            invalidate_panel_gallery_cache()
        except (ImportError, AttributeError, TypeError, ValueError, RuntimeError) as fallback_error:
            debug_tools.log(f"panel gallery cache fallback failed: {fallback_error}")
            pass

    if include_modal_gallery and VIEW3D_OT_thumbnail_gallery._is_active:
        VIEW3D_OT_thumbnail_gallery.request_refresh()


class VIEWPILOT_OT_recover_storage_overwrite(bpy.types.Operator):
    """Overwrite corrupted ViewPilot storage with a fresh empty payload."""
    bl_idname = "viewpilot.recover_storage_overwrite"
    bl_label = "ViewPilot Storage Recovery"
    bl_options = {'INTERNAL'}

    action_label: bpy.props.StringProperty(default="save view", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        wm = context.window_manager
        try:
            return wm.invoke_props_dialog(
                self,
                width=520,
                title="ViewPilot Storage Error",
                confirm_text="Overwrite",
                cancel_default=True,
            )
        except TypeError:
            return wm.invoke_props_dialog(self, width=520)

    def draw(self, context):
        from . import data_storage

        layout = self.layout
        layout.label(text=f"Can't {self.action_label} because JSON storage is corrupted.")
        layout.label(text="Overwrite ViewPilot storage and start from scratch?")

        backup_name = data_storage.get_storage_error_backup_name()
        if backup_name:
            layout.label(text=f"Backup: {backup_name}", icon='FILE_TEXT')

        error_msg = data_storage.get_storage_error_message()
        if error_msg:
            layout.label(text=f"Details: {error_msg}")

    def execute(self, context):
        from . import data_storage

        if not data_storage.force_reset_storage():
            self.report({'ERROR'}, "Failed to overwrite ViewPilot storage (see console)")
            return {'CANCELLED'}

        try:
            data_storage.sync_to_all_scenes()
        except (RuntimeError, ReferenceError, AttributeError, ValueError) as error:
            debug_tools.log(f"sync_to_all_scenes failed after storage reset: {error}")

        _refresh_saved_views_ui()

        self.report({'INFO'}, "ViewPilot storage overwritten. You can save views again.")
        return {'FINISHED'}


class VIEW3D_OT_save_current_view(bpy.types.Operator):
    """Save the current viewport as a new view"""
    bl_idname = "view3d.save_current_view"
    bl_label = "Save Current View"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        _, space, region = utils.find_view3d_context(context)
        return bool(space and region)
    
    def execute(self, context):
        from . import data_storage
        
        preferred_area = VIEW3D_OT_thumbnail_gallery._context_area
        _, space, region = utils.find_view3d_context(context, preferred_area=preferred_area)
        
        if not space or not region:
            self.report({'ERROR'}, "No 3D View found")
            return {'CANCELLED'}
        
        # Auto-generate unique name using the counter
        view_number = data_storage.get_next_view_number()
        if view_number < 0:
            _handle_storage_invalid(context, self, "save view")
            return {'CANCELLED'}
        view_name = f"View {view_number}"
        
        # Capture viewport state as a dictionary
        view_dict = data_storage.capture_viewport_as_dict(space, region, context, view_name)
        
        # Apply default remember toggles from preferences
        try:
            prefs = get_preferences()
            view_dict["remember_perspective"] = prefs.default_remember_perspective
            view_dict["remember_shading"] = prefs.default_remember_shading
            view_dict["remember_overlays"] = prefs.default_remember_overlays
            view_dict["remember_composition"] = prefs.default_remember_composition
        except (AttributeError, RuntimeError, ValueError):
            pass  # Keep defaults from capture_viewport_as_dict
        
        # Add to JSON storage (auto-syncs to PropertyGroup)
        new_index = data_storage.add_saved_view(view_dict)
        if new_index < 0:
            _handle_storage_invalid(context, self, "save view")
            return {'CANCELLED'}
        
        # Generate thumbnail for this view
        # Create a temporary PropertyGroup-like object for thumbnail generator
        try:
            from types import SimpleNamespace
            temp_view = SimpleNamespace(**view_dict)
            # Convert lists to tuples for compatibility
            temp_view.location = tuple(view_dict["location"])
            temp_view.rotation = tuple(view_dict["rotation"])
            
            thumb_name = generate_thumbnail(context, temp_view, view_name)
            if thumb_name:
                view_dict["thumbnail_image"] = thumb_name
                if not data_storage.update_saved_view(new_index, view_dict):
                    self.report({'WARNING'}, "Thumbnail saved in-memory but ViewPilot storage update was blocked")
                
            # Notify gallery to refresh if open
            if VIEW3D_OT_thumbnail_gallery._is_active:
                VIEW3D_OT_thumbnail_gallery.request_refresh()
        except (RuntimeError, ReferenceError, AttributeError, TypeError, ValueError, OSError) as e:
            try:
                from . import thumbnail_generator as _thumb_mod
                thumb_module = getattr(_thumb_mod, "__file__", "<unknown>")
                thumb_version = getattr(_thumb_mod, "THUMBNAIL_RENDERER_VERSION", "<unknown>")
            except (ImportError, AttributeError):
                thumb_module = "<import-failed>"
                thumb_version = "<import-failed>"
            self.report({'WARNING'}, "Thumbnail generation failed (see console)")
            debug_tools.log(
                "thumbnail generation failed: "
                f"{e} (thumb_module={thumb_module}, thumb_version={thumb_version})"
            )
            traceback.print_exc()
        
        # Set as active
        context.scene.saved_views_index = new_index
        
        # Sync the shared property enum to show the new view
        # We temporarily skip loading because we are already AT the view
        with _suppress_saved_view_enum_load():
            _sync_saved_view_enums_safe(context, str(new_index))
        
        self.report({'INFO'}, f"Saved view: {view_name}")
        return {'FINISHED'}


class VIEW3D_OT_load_saved_view(bpy.types.Operator):
    """Load the selected saved view"""
    bl_idname = "view3d.load_saved_view"
    bl_label = "Load Saved View"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        views = data_storage.get_saved_views()
        _, space, region = utils.find_view3d_context(context)
        return bool(space and region and len(views) > 0 and context.scene.saved_views_index >= 0)
    
    def execute(self, context):
        from . import data_storage

        preferred_area = VIEW3D_OT_thumbnail_gallery._context_area
        _, space, region = utils.find_view3d_context(context, preferred_area=preferred_area)
        if not space or not region:
            self.report({'ERROR'}, "No 3D View found")
            return {'CANCELLED'}
        
        index = context.scene.saved_views_index
        view_dict = data_storage.get_saved_view(index)
        
        if not view_dict:
            self.report({'WARNING'}, "No saved view selected")
            return {'CANCELLED'}
        
        # Apply view using data_storage helper
        data_storage.apply_view_to_viewport(view_dict, space, region, context)
        
        # Lock history recording briefly using StateController
        get_controller().start_grace_period(0.5, UpdateSource.VIEW_RESTORE)
        
        # Sync properties to new view
        context.scene.viewpilot.reinitialize_from_context(context)
        
        # Reset ghost tracking
        context.scene.viewpilot.last_active_view_index = -1
        
        self.report({'INFO'}, f"Loaded view: {view_dict.get('name', 'View')}")
        return {'FINISHED'}


class VIEW3D_OT_delete_saved_view(bpy.types.Operator):
    """Delete the selected saved view"""
    bl_idname = "view3d.delete_saved_view"
    bl_label = "Delete Saved View"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Optional index - if >= 0, use this instead of saved_views_index
    index: bpy.props.IntProperty(default=-1)
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        views = data_storage.get_saved_views()
        return len(views) > 0
    
    def execute(self, context):
        from . import data_storage
        
        # Use index property if set, otherwise use saved_views_index
        index = self.index if self.index >= 0 else context.scene.saved_views_index
        view_dict = data_storage.get_saved_view(index)
        
        if not view_dict:
            self.report({'WARNING'}, "No saved view selected")
            return {'CANCELLED'}
        
        view_name = view_dict.get("name", "View")
        
        # Delete associated thumbnail
        delete_thumbnail(view_name)

        # Pre-clear dynamic enum selections so they never reference a soon-to-be
        # invalid index while sync_to_all_scenes updates the backing collection.
        with _suppress_saved_view_enum_load():
            context.scene.saved_views_index = -1
            context.scene.viewpilot.last_active_view_index = -1
            context.scene.viewpilot.saved_views_enum = 'NONE'
            try:
                context.scene.viewpilot.panel_gallery_enum = 'NONE'
            except (TypeError, ValueError, RuntimeError, AttributeError):
                pass
        
        # Remove the view from JSON storage (auto-syncs to PropertyGroup)
        if not data_storage.delete_saved_view(index):
            _handle_storage_invalid(context, self, "delete view")
            return {'CANCELLED'}

        # Invalidate dropdown/panel caches and refresh gallery after index shift.
        _refresh_saved_views_ui()
        
        # Deletion intentionally leaves the addon in "no view selected" state.
        # This avoids implying we're on another saved view when viewport state
        # has not been loaded from it.
        with _suppress_saved_view_enum_load():
            context.scene.saved_views_index = -1
            context.scene.viewpilot.last_active_view_index = -1
            context.scene.viewpilot.saved_views_enum = 'NONE'
            try:
                context.scene.viewpilot.panel_gallery_enum = 'NONE'
            except (TypeError, ValueError, RuntimeError, AttributeError):
                pass
        
        self.report({'INFO'}, f"Deleted view: {view_name}")
        
        # Clean up World fake users that may no longer be needed
        utils.cleanup_world_fake_users()
        
        return {'FINISHED'}


class VIEW3D_OT_update_saved_view(bpy.types.Operator):
    """Update the selected saved view with current viewport"""
    bl_idname = "view3d.update_saved_view"
    bl_label = "Update Saved View"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Optional index - if >= 0, use this instead of saved_views_index
    index: bpy.props.IntProperty(default=-1)
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        _, space, region = utils.find_view3d_context(context)
        has_view3d = bool(space and region)
        
        views = data_storage.get_saved_views()
        if not has_view3d or len(views) == 0:
            return False
            
        # Always enable if there are saved views (execute handles fallback)
        return True
    
    def execute(self, context):
        from . import data_storage
        
        preferred_area = VIEW3D_OT_thumbnail_gallery._context_area
        _, space, region = utils.find_view3d_context(context, preferred_area=preferred_area)
        
        if not space or not region:
            self.report({'ERROR'}, "No 3D View found")
            return {'CANCELLED'}
        
        views = data_storage.get_saved_views()
        
        # Use provided index if valid, otherwise use saved_views_index
        index = self.index if self.index >= 0 else context.scene.saved_views_index
        
        # Handle Ghost View case
        if index == -1:
            last_idx = context.scene.viewpilot.last_active_view_index
            if last_idx >= 0 and last_idx < len(views):
                index = last_idx
        
        if index < 0 or index >= len(views):
            self.report({'WARNING'}, "No saved view selected")
            return {'CANCELLED'}
        
        # Get existing view to preserve name and remember toggles
        existing_view = data_storage.get_saved_view(index)
        view_name = existing_view.get("name", "View")
        
        # Capture current viewport state
        view_dict = data_storage.capture_viewport_as_dict(space, region, context, view_name)
        
        # Preserve remember toggles from original view
        view_dict["remember_perspective"] = existing_view.get("remember_perspective", True)
        view_dict["remember_shading"] = existing_view.get("remember_shading", True)
        view_dict["remember_overlays"] = existing_view.get("remember_overlays", True)
        view_dict["remember_composition"] = existing_view.get("remember_composition", True)
        
        # Regenerate thumbnail with new view
        try:
            from types import SimpleNamespace
            temp_view = SimpleNamespace(**view_dict)
            temp_view.location = tuple(view_dict["location"])
            temp_view.rotation = tuple(view_dict["rotation"])
            
            thumb_name = generate_thumbnail(context, temp_view, view_name)
            if thumb_name:
                view_dict["thumbnail_image"] = thumb_name
            # Notify gallery to refresh if open
            if VIEW3D_OT_thumbnail_gallery._is_active:
                VIEW3D_OT_thumbnail_gallery.request_refresh()
        except (RuntimeError, ReferenceError, AttributeError, TypeError, ValueError, OSError) as e:
            try:
                from . import thumbnail_generator as _thumb_mod
                thumb_module = getattr(_thumb_mod, "__file__", "<unknown>")
                thumb_version = getattr(_thumb_mod, "THUMBNAIL_RENDERER_VERSION", "<unknown>")
            except (ImportError, AttributeError):
                thumb_module = "<import-failed>"
                thumb_version = "<import-failed>"
            self.report({'WARNING'}, "Thumbnail regeneration failed (see console)")
            debug_tools.log(
                "thumbnail regeneration failed: "
                f"{e} (thumb_module={thumb_module}, thumb_version={thumb_version})"
            )
            traceback.print_exc()
        
        # Update in JSON storage
        if not data_storage.update_saved_view(index, view_dict):
            _handle_storage_invalid(context, self, "update view")
            return {'CANCELLED'}
        
        # Clear modified flag and snap back to the view (it's now cleanly matched)
        params = context.scene.viewpilot
        # Revert to standard selection (exit ghost mode)
        context.scene.saved_views_index = index
        params.last_active_view_index = -1
        
        # Force dropdown update
        with _suppress_saved_view_enum_load():
            _sync_saved_view_enums_safe(context, str(index))
        
        self.report({'INFO'}, f"Updated view: {view_name}")
        return {'FINISHED'}


class VIEW3D_OT_rename_saved_view(bpy.types.Operator):
    """Rename the selected saved view"""
    bl_idname = "view3d.rename_saved_view"
    bl_label = "Rename Saved View"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    
    # Optional index - if >= 0, use this instead of saved_views_index
    index: bpy.props.IntProperty(default=-1)
    
    def get_target_index(self, context):
        """Get the view index to operate on."""
        if self.index >= 0:
            return self.index
        return context.scene.saved_views_index

    def _rename_associated_camera(self, old_name: str, new_name: str) -> None:
        """Rename camera datablocks associated with this view name."""
        try:
            from .preferences import get_preferences
            prefs = get_preferences()
            prefix = prefs.camera_name_prefix
        except (ImportError, AttributeError, RuntimeError, ValueError):
            prefix = "ViewCam"

        old_cam_name = f"{prefix} [{old_name}]"
        new_cam_name = f"{prefix} [{new_name}]"

        for obj in bpy.data.objects:
            if obj.type == 'CAMERA' and obj.name == old_cam_name:
                obj.name = new_cam_name
                if obj.data:
                    obj.data.name = new_cam_name
                break
    
    new_name: bpy.props.StringProperty(
        name="",
        description="New name for the saved view",
        default=""
    )
    
    @classmethod
    def poll(cls, context):
        # We can't check 'index' property here because it's not available in class method poll
        # But we can check if there are ANY saved views. The specific check happens in invoke/execute.
        # Ideally, buttons setting 'index' should do their own context checks.
        # For general panel buttons relying on saved_views_index, we check that.
        
        # NOTE: When called from menu with specific index, we can't see 'self.index' here.
        # So we have to be permissive in poll and stricter in invoke/execute/draw.
        # However, to support standard UI buttons (which rely on active selection), 
        # we check the active index.
        # BUT this breaks the modal gallery context menu item which calls this operator 
        # on an unselected view.
        # The correct fix: Be permissive here (just len > 0), and let UI layout. enabled handle the button state for the panel.
        from . import data_storage
        return len(data_storage.get_saved_views()) > 0
    
    def invoke(self, context, event):
        from . import data_storage
        views = data_storage.get_saved_views()
        idx = self.get_target_index(context)
        if 0 <= idx < len(views):
            self.new_name = views[idx].get("name", "View")
        return context.window_manager.invoke_props_dialog(self, width=260)
    
    def draw(self, context):
        layout = self.layout
        layout.label(text="Rename View:")
        
        row = layout.row()
        row.activate_init = True
        row.prop(self, "new_name", text="")
    
    def execute(self, context):
        from . import data_storage

        views = data_storage.get_saved_views()
        idx = self.get_target_index(context)
        if not (0 <= idx < len(views)):
            self.report({'WARNING'}, "No saved view selected")
            return {'CANCELLED'}

        view_dict = views[idx]
        old_name = view_dict.get("name", "View")
        new_name = self.new_name.strip()

        if not new_name:
            self.report({'WARNING'}, "View name cannot be empty")
            return {'CANCELLED'}

        if old_name == new_name:
            return {'FINISHED'}

        view_dict["name"] = new_name
        if not data_storage.update_saved_view(idx, view_dict):
            _handle_storage_invalid(context, self, "rename view")
            return {'CANCELLED'}

        self._rename_associated_camera(old_name, new_name)

        _refresh_saved_views_ui()

        self.report({'INFO'}, f"Renamed view: {new_name}")
        return {'FINISHED'}


class VIEW3D_OT_prev_saved_view(bpy.types.Operator):
    """Go to the previous saved view"""
    bl_idname = "view3d.prev_saved_view"
    bl_label = "Previous Saved View"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        return len(data_storage.get_saved_views()) > 0
    
    def execute(self, context):
        from . import data_storage
        views = data_storage.get_saved_views()
        current_index = context.scene.saved_views_index
        
        # Go to previous, wrap around
        new_index = current_index - 1
        if new_index < 0:
            new_index = len(views) - 1

        # Trigger normal enum callback path so the viewport actually loads.
        context.scene.viewpilot.saved_views_enum = str(new_index)
        
        # Report which view we're on
        view_name = views[new_index].get("name", "View")
        self.report({'INFO'}, f"◄ {view_name} ({new_index + 1}/{len(views)})")
        
        return {'FINISHED'}


class VIEW3D_OT_next_saved_view(bpy.types.Operator):
    """Go to the next saved view"""
    bl_idname = "view3d.next_saved_view"
    bl_label = "Next Saved View"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        return len(data_storage.get_saved_views()) > 0
    
    def execute(self, context):
        from . import data_storage
        views = data_storage.get_saved_views()
        current_index = context.scene.saved_views_index
        
        # Go to next, wrap around
        new_index = current_index + 1
        if new_index >= len(views):
            new_index = 0

        # Trigger normal enum callback path so the viewport actually loads.
        context.scene.viewpilot.saved_views_enum = str(new_index)
        
        # Report which view we're on
        view_name = views[new_index].get("name", "View")
        self.report({'INFO'}, f"► {view_name} ({new_index + 1}/{len(views)})")
        
        return {'FINISHED'}

# ========================================================================
# REORDER VIEWS
# ========================================================================

class VIEW3D_OT_set_saved_views_index(bpy.types.Operator):
    """Select a saved view for reordering without navigating to it"""
    bl_idname = "view3d.set_saved_views_index"
    bl_label = "Select View"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    index: bpy.props.IntProperty(default=-1)
    
    def execute(self, context):
        # Set index without triggering view load
        with _suppress_saved_view_enum_load():
            context.scene.saved_views_index = self.index
            _sync_saved_view_enums_safe(context, str(self.index))
        
        return {'FINISHED'}

class VIEWPILOT_UL_saved_views_reorder(bpy.types.UIList):
    """UIList for reordering saved views with drag-and-drop."""

    def _get_icon_map(self, context):
        """Build/reuse icon cache for current saved view ordering."""
        views = getattr(context.scene, "saved_views", [])
        signature = tuple((view.name, view.thumbnail_image) for view in views)

        cached_sig = getattr(self, "_icon_cache_signature", None)
        cached_map = getattr(self, "_icon_cache_map", None)
        if cached_sig == signature and cached_map is not None:
            return cached_map

        icon_map = {}
        try:
            from .preview_manager import get_view_icon_id_fast
            for idx, view in enumerate(views):
                icon_map[idx] = get_view_icon_id_fast(view.name, view.thumbnail_image)
        except (ImportError, AttributeError, RuntimeError, ReferenceError, ValueError) as error:
            debug_tools.log(f"reorder list icon map build failed: {error}")
            icon_map = {}

        self._icon_cache_signature = signature
        self._icon_cache_map = icon_map
        return icon_map
    
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        view = item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            # Main row with split for label and buttons
            row = layout.row(align=True)
            
            # Try to get thumbnail icon from cached map.
            icon_id = self._get_icon_map(context).get(index, 0)
            
            # View name with icon (takes most of the space)
            if icon_id:
                row.label(text=view.name, icon_value=icon_id)
            else:
                row.label(text=view.name, icon='BOOKMARKS')
            
            # Rename button
            op_rename = row.operator("view3d.rename_saved_view", text="", icon='FONT_DATA', emboss=False)
            op_rename.index = index
            
            # Delete button
            op_delete = row.operator("view3d.delete_saved_view", text="", icon='X', emboss=False)
            op_delete.index = index
            
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='BOOKMARKS')


class VIEW3D_OT_reorder_views(bpy.types.Operator):
    """Open a popup to reorder saved views via drag-and-drop"""
    bl_idname = "view3d.reorder_views"
    bl_label = "Reorder Views"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        return len(data_storage.get_saved_views()) > 1
    
    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=300)
    
    def draw(self, context):
        from . import data_storage
        
        layout = self.layout
        layout.label(text="Use buttons to reorder:", icon='SORTSIZE')
        
        views = data_storage.get_saved_views()
        
        # UIList with built-in drag-and-drop
        row = layout.row()
        row.template_list(
            "VIEWPILOT_UL_saved_views_reorder", "",
            context.scene, "saved_views",
            context.scene, "saved_views_index",
            rows=min(10, max(3, len(views)))
        )
        
        # Move buttons column
        col = row.column(align=True)
        col.operator("view3d.move_view_up", icon='TRIA_UP', text="")
        col.operator("view3d.move_view_down", icon='TRIA_DOWN', text="")
    
    def execute(self, context):
        # Refresh galleries after reordering.
        _refresh_saved_views_ui()
        return {'FINISHED'}


class VIEW3D_OT_move_view_up(bpy.types.Operator):
    """Move selected view up in the list"""
    bl_idname = "view3d.move_view_up"
    bl_label = "Move View Up"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        views = data_storage.get_saved_views()
        return (len(views) > 1 and 
                context.scene.saved_views_index > 0)
    
    def execute(self, context):
        from . import data_storage
        
        views = data_storage.get_saved_views()
        idx = context.scene.saved_views_index
        
        if idx > 0 and idx < len(views):
            # Swap the views in JSON storage
            views[idx], views[idx - 1] = views[idx - 1], views[idx]
            
            # Save the reordered list
            data = data_storage.load_data()
            data["saved_views"] = views
            if not data_storage.save_data(data):
                _handle_storage_invalid(context, self, "reorder views")
                return {'CANCELLED'}
            
            # Sync to PropertyGroup so UIList updates
            data_storage.sync_to_all_scenes()
            
            # Update index to follow the moved view
            new_index = idx - 1
            
            with _suppress_saved_view_enum_load():
                context.scene.saved_views_index = new_index
                _sync_saved_view_enums_safe(context, str(new_index))
            
            # Refresh galleries.
            _refresh_saved_views_ui()
        
        return {'FINISHED'}


class VIEW3D_OT_move_view_down(bpy.types.Operator):
    """Move selected view down in the list"""
    bl_idname = "view3d.move_view_down"
    bl_label = "Move View Down"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        from . import data_storage
        views = data_storage.get_saved_views()
        return (len(views) > 1 and 
                context.scene.saved_views_index < len(views) - 1)
    
    def execute(self, context):
        from . import data_storage
        
        views = data_storage.get_saved_views()
        idx = context.scene.saved_views_index
        
        if idx >= 0 and idx < len(views) - 1:
            # Swap the views in JSON storage
            views[idx], views[idx + 1] = views[idx + 1], views[idx]
            
            # Save the reordered list
            data = data_storage.load_data()
            data["saved_views"] = views
            if not data_storage.save_data(data):
                _handle_storage_invalid(context, self, "reorder views")
                return {'CANCELLED'}
            
            # Sync to PropertyGroup so UIList updates
            data_storage.sync_to_all_scenes()
            
            # Update index to follow the moved view
            new_index = idx + 1
            
            with _suppress_saved_view_enum_load():
                context.scene.saved_views_index = new_index
                _sync_saved_view_enums_safe(context, str(new_index))
            
            # Refresh galleries.
            _refresh_saved_views_ui()
        
        return {'FINISHED'}


# ========================================================================
# CLASSES LIST FOR REGISTRATION
# ========================================================================

classes = (
    VIEWPILOT_OT_recover_storage_overwrite,
    VIEW3D_OT_view_history_monitor,
    VIEW3D_OT_view_history_back,
    VIEW3D_OT_view_history_forward,
    VIEW3D_OT_sync_viewpilot,
    VIEW3D_OT_open_viewpilot_prefs,
    VIEW3D_OT_toggle_camera_selection,
    VIEW3D_OT_toggle_camera_name,
    VIEW3D_OT_exit_camera_view,
    VIEW3D_OT_create_camera_from_view,
    VIEW3D_OT_dolly_to_obstacle,
    VIEW3D_OT_save_current_view,
    VIEW3D_OT_load_saved_view,
    VIEW3D_OT_delete_saved_view,
    VIEW3D_OT_update_saved_view,
    VIEW3D_OT_rename_saved_view,
    VIEW3D_OT_prev_saved_view,
    VIEW3D_OT_next_saved_view,
    VIEW3D_OT_set_saved_views_index,
    VIEWPILOT_UL_saved_views_reorder,
    VIEW3D_OT_reorder_views,
    VIEW3D_OT_move_view_up,
    VIEW3D_OT_move_view_down,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
