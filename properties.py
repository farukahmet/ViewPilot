"""
Properties and State Management for ViewPilot.
Contains PropertyGroups and their update callbacks.
"""

import bpy
import math
import time
from mathutils import Vector, Euler, Quaternion
from .state_controller import get_controller, UpdateSource, LockPriority
from . import debug_tools
from .utils import (
    get_view_location, set_view_location, add_to_history,
    get_selection_center, find_view3d_context,
    find_view3d_override_context, find_window_for_area
)

# ============================================================================
# PROPERTY UPDATE CALLBACKS
# ============================================================================

def update_view_transform(self, context):
    if not self.init_complete: return
    if self.internal_lock: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        # Start grace period to prevent reinitialize_from_context during drag
        controller.start_grace_period(0.2)

        if context.space_data.type == 'VIEW_3D':
            target_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
            target_rot = Euler((self.rot_x, self.rot_y, self.rot_z), 'XYZ')
            
            # Camera mode: move the camera object
            if self.is_camera_mode and context.scene.camera:
                cam = context.scene.camera
                cam.location = target_pos
                cam.rotation_euler = target_rot
            else:
                # Viewport mode
                set_view_location(context, target_pos, target_rot)
            
            # Invalidate all relative state - absolute position changed
            self.invalidate_all_relative_state(target_pos, target_rot, target_rot.to_quaternion())
            
            # Disable orbit mode when loc/rot values are changed directly
            # (orbit mode requires pivoting around fixed center, direct edits break that)
            self.invalidate_orbit_state(target_pos, disable_mode=True)
    finally:
        controller.end_update()

def update_screen_space_transform(self, context):
    if not self.init_complete: return
    if self.internal_lock: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        # Start grace period to prevent reinitialize_from_context during drag
        controller.start_grace_period(0.2)

        if context.space_data.type == 'VIEW_3D':
            # Get rotation quaternion
            if self.is_camera_mode and context.scene.camera:
                cam = context.scene.camera
                rot_quat = cam.rotation_euler.to_quaternion()
            else:
                region = context.region_data
                if not region and context.space_data and context.space_data.type == 'VIEW_3D':
                    region = context.space_data.region_3d
                
                if region:
                    rot_quat = region.view_rotation
                else:
                    return # Still failed to find region
            
            right = rot_quat @ Vector((1.0, 0.0, 0.0))
            up = rot_quat @ Vector((0.0, 1.0, 0.0))
            
            # If zoom has moved us from base, update base to current position first
            # This prevents jump when switching from zoom to pan
            if abs(self.zoom_level) > 0.001:
                current_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
                self.invalidate_zoom_state(current_pos)
            
            # Screen space panning: U (horizontal) and V (vertical)
            offset = (right * self.screen_x) + (up * self.screen_z)
            base_pos = Vector(self.base_world_pos)
            new_pos = base_pos + offset
            
            # Use direct property assignment (no internal_lock needed with controller)
            self['loc_x'] = new_pos.x
            self['loc_y'] = new_pos.y
            self['loc_z'] = new_pos.z
            
            # Apply to camera or viewport
            if self.is_camera_mode and context.scene.camera:
                context.scene.camera.location = new_pos
            else:
                target_rot = Euler((self.rot_x, self.rot_y, self.rot_z), 'XYZ')
                set_view_location(context, new_pos, target_rot)
            
            # Pan movement breaks orbit (selection no longer centered)
            self.invalidate_orbit_state(new_pos, disable_mode=True)
    finally:
        controller.end_update()

def update_screen_rotation(self, context):
    """Rotate the viewport around the center of the screen (Z-axis roll)."""
    if not self.init_complete: return
    if self.internal_lock: return
    
    # If in Orbit mode, dispatch to orbit transform logic
    if self.orbit_around_selection:
        update_orbit_transform(self, context)
        return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        # Start grace period during drag
        controller.start_grace_period(0.2)

        if context.space_data.type == 'VIEW_3D':
            # Calculate new rotation by adding screen rotation to base rotation
            base_rot = Euler((self.base_rotation[0], self.base_rotation[1], self.base_rotation[2]), 'XYZ')
            base_quat = base_rot.to_quaternion()
            
            # Apply screen rotation around the view's Z axis (roll)
            roll_quat = Quaternion((0.0, 0.0, 1.0), self.screen_rotation)
            new_quat = base_quat @ roll_quat
            new_euler = new_quat.to_euler()
            
            # Use direct property assignment (no callbacks triggered)
            self['rot_x'] = new_euler.x
            self['rot_y'] = new_euler.y
            self['rot_z'] = new_euler.z
            
            # Reset pan values since the screen axes have changed after roll
            current_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
            self.invalidate_pan_state(current_pos)
            
            # Apply to camera or viewport
            if self.is_camera_mode and context.scene.camera:
                context.scene.camera.rotation_euler = new_euler
            else:
                target_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
                set_view_location(context, target_pos, new_euler)
    finally:
        controller.end_update()

def update_orbit_mode_toggle(self, context):
    """Initialize turntable orbit mode when toggled on.
    
    Uses native Blender operators (view_selected/view_all) for smooth framing,
    then sets all orbit values to zero as the reference point.
    In camera mode, skips framing and uses camera's current position.
    """
    if not self.init_complete: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        if self.orbit_around_selection:
            # --- ENABLE ORBIT MODE ---
            
            # Check if we're in camera mode
            in_camera_mode = self.is_camera_mode and context.scene.camera
            
            # 1. Find the 3D View area and WINDOW region for proper override context.
            target_area, _, target_region = find_view3d_override_context(context)
            target_window = find_window_for_area(context, target_area)
            
            # 2. Calculate pivot point from selection center FIRST
            center = get_selection_center(context)
            if center:
                self['orbit_center'] = (center.x, center.y, center.z)
            else:
                self['orbit_center'] = (0.0, 0.0, 0.0)
            
            # 3. Frame the view (ONLY in viewport mode, NOT camera mode)
            if not in_camera_mode:
                if target_area and target_region:
                    try:
                        override_kwargs = {"area": target_area, "region": target_region}
                        if target_window:
                            override_kwargs["window"] = target_window
                        with bpy.context.temp_override(**override_kwargs):
                            if bpy.context.selected_objects:
                                bpy.ops.view3d.view_selected('INVOKE_DEFAULT')
                            else:
                                bpy.ops.view3d.view_all('INVOKE_DEFAULT')
                    except Exception as e:
                        print(f"[ViewPilot] Framing failed: {e}")
            
            # 4. Start grace period
            controller.start_grace_period(0.5)
            
            # 5. Reset UI values and set temporary "pending" state
            self['orbit_pitch'] = 0.0
            self['orbit_yaw'] = 0.0
            self['screen_rotation'] = 0.0
            self['orbit_active_axis'] = ""  # No axis active yet
            self['orbit_base_yaw'] = 0.0
            self['orbit_base_pitch'] = 0.0
            self['orbit_distance'] = 10.0  # Temporary default
            self['orbit_initialized'] = False  # Will be set True by timer
            
            # 6. Schedule delayed initialization after framing completes
            def delayed_orbit_init():
                """Timer callback to calculate base values after framing animation."""
                try:
                    scene = bpy.context.scene
                    props = scene.viewpilot
                    
                    # Abort if orbit was disabled in the meantime
                    if not props.orbit_around_selection:
                        return None
                    
                    # Check camera mode
                    cam = scene.camera
                    in_cam_mode = props.is_camera_mode and cam
                    
                    if in_cam_mode:
                        # CAMERA MODE: Use camera's position directly
                        eye_pos = cam.location.copy()
                        rot = cam.rotation_euler.to_quaternion()
                        dist = 10.0  # Doesn't matter for camera mode, we use actual position
                        
                        # Get stored center
                        stored_center = Vector(props.orbit_center)
                        if stored_center.length < 0.001:
                            # No selection - use point 10 units in front of camera
                            forward = rot @ Vector((0.0, 0.0, -1.0))
                            center = eye_pos + forward * 10.0
                            props['orbit_center'] = (center.x, center.y, center.z)
                        else:
                            center = stored_center
                        
                        # Calculate distance from camera to center
                        base_offset = eye_pos - center
                        dist = base_offset.length
                        
                    else:
                        # VIEWPORT MODE: Use region_3d
                        _, _, region_3d = find_view3d_context(bpy.context)
                        
                        if not region_3d:
                            print("[ViewPilot] Could not find 3D view for orbit init")
                            return None
                        
                        # Get framed camera position
                        rot = region_3d.view_rotation
                        view_z = Vector((0.0, 0.0, 1.0))
                        dist = region_3d.view_distance
                        eye_pos = region_3d.view_location + (rot @ view_z) * dist
                        
                        # Get stored center
                        stored_center = Vector(props.orbit_center)
                        if stored_center.length < 0.001:
                            center = region_3d.view_location.copy()
                            props['orbit_center'] = (center.x, center.y, center.z)
                        else:
                            center = stored_center
                        
                        base_offset = eye_pos - center
                    
                    # Store everything
                    props['orbit_distance'] = dist
                    props['orbit_base_offset'] = (base_offset.x, base_offset.y, base_offset.z)
                    props['orbit_base_rotation'] = (rot.w, rot.x, rot.y, rot.z)  # Quaternion as WXYZ
                    
                    # Sync UI loc/rot to framed position
                    props['loc_x'] = eye_pos.x
                    props['loc_y'] = eye_pos.y
                    props['loc_z'] = eye_pos.z
                    
                    euler = rot.to_euler('XYZ')
                    props['rot_x'] = euler.x
                    props['rot_y'] = euler.y
                    props['rot_z'] = euler.z
                    
                    # Rebase pan/zoom for orbit mode without forcing visible slider jumps.
                    props.invalidate_pan_state(eye_pos, euler, disable_mode=True)
                    props.invalidate_zoom_state(eye_pos, rot, preserve_value=True)
                    
                    # Mark as ready
                    props['orbit_initialized'] = True
                    print(f"[ViewPilot] Orbit initialized: offset={base_offset}, dist={dist:.2f}")
                    
                except Exception as e:
                    print(f"[ViewPilot] Delayed orbit init failed: {e}")
                
                return None  # Don't repeat timer
            
            # Schedule the callback (shorter delay for camera mode since no animation)
            delay = 0.1 if in_camera_mode else 0.3
            bpy.app.timers.register(delayed_orbit_init, first_interval=delay)
            
        else:
            # --- DISABLE ORBIT MODE ---
            self['orbit_initialized'] = False
            self['orbit_distance'] = 0.0
            self['orbit_base_yaw'] = 0.0
            self['orbit_base_pitch'] = 0.0
            
    finally:
        controller.end_update()


def update_orbit_transform(self, context):
    """Apply orbit yaw/pitch/roll changes - trackball style rotation around selection.
    
    Uses camera-local axes translated to the pivot point:
    - Pitch: rotates around axis parallel to camera's local X, through pivot
    - Yaw: rotates around axis parallel to camera's local Y, through pivot
    - Roll: via screen_rotation (camera tilts around view axis)
    
    Only ONE axis can be active at a time. When switching axes:
    1. Commit the current rotated position as the new base
    2. Reset ALL sliders to zero
    3. Start the new axis from this committed base
    """
    if not self.init_complete: return
    if not self.orbit_around_selection: return
    if not self.orbit_initialized: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        # Start grace period during drag
        controller.start_grace_period(0.2)
        
        # Determine which axis the user is currently interacting with
        pitch_val = self.orbit_pitch
        yaw_val = self.orbit_yaw
        roll_val = self.screen_rotation
        
        pitch_nonzero = abs(pitch_val) > 0.0001
        yaw_nonzero = abs(yaw_val) > 0.0001
        roll_nonzero = abs(roll_val) > 0.0001
        
        # Determine which axis is being used NOW
        # If multiple are non-zero, we need to figure out which one just changed
        current_axis = ""
        if pitch_nonzero and not yaw_nonzero and not roll_nonzero:
            current_axis = "pitch"
        elif yaw_nonzero and not pitch_nonzero and not roll_nonzero:
            current_axis = "yaw"
        elif roll_nonzero and not pitch_nonzero and not yaw_nonzero:
            current_axis = "roll"
        elif pitch_nonzero or yaw_nonzero or roll_nonzero:
            # Multiple non-zero: a new axis was just touched
            # The new axis is whichever one JUST became non-zero
            # We detect this by comparing with orbit_active_axis
            prev_axis = self.orbit_active_axis
            if prev_axis == "pitch" and (yaw_nonzero or roll_nonzero):
                # Was pitch, now yaw or roll was added
                current_axis = "yaw" if yaw_nonzero else "roll"
            elif prev_axis == "yaw" and (pitch_nonzero or roll_nonzero):
                current_axis = "pitch" if pitch_nonzero else "roll"
            elif prev_axis == "roll" and (pitch_nonzero or yaw_nonzero):
                current_axis = "pitch" if pitch_nonzero else "yaw"
            elif prev_axis == "":
                # No previous axis - pick any that's non-zero
                if pitch_nonzero: current_axis = "pitch"
                elif yaw_nonzero: current_axis = "yaw"
                elif roll_nonzero: current_axis = "roll"
            else:
                current_axis = prev_axis  # Fallback: keep previous
        
        # Get stored orbit parameters
        center = Vector(self.orbit_center)
        base_offset = Vector(self.orbit_base_offset)
        base_quat = Quaternion(self.orbit_base_rotation)
        
        # If axis changed, commit current state and reset others
        prev_axis = self.orbit_active_axis
        if current_axis != "" and current_axis != prev_axis and prev_axis != "":
            # Commit the rotation from the PREVIOUS axis before switching
            if prev_axis == "pitch":
                local_x = base_quat @ Vector((1.0, 0.0, 0.0))
                prev_rot = Quaternion(local_x, -pitch_val)
            elif prev_axis == "yaw":
                local_y = base_quat @ Vector((0.0, 1.0, 0.0))
                prev_rot = Quaternion(local_y, -yaw_val)
            elif prev_axis == "roll":
                prev_rot = Quaternion((0, 0, 1), roll_val)
                # Roll is applied to quat, not offset
                base_quat = base_quat @ prev_rot
                prev_rot = Quaternion()  # Don't apply to offset
            else:
                prev_rot = Quaternion()
            
            # Apply previous rotation to get committed state
            if prev_axis != "roll":
                new_base_offset = prev_rot @ base_offset
                new_base_quat = prev_rot @ base_quat
            else:
                new_base_offset = base_offset
                new_base_quat = base_quat
            
            # Update base to committed state
            self['orbit_base_offset'] = (new_base_offset.x, new_base_offset.y, new_base_offset.z)
            self['orbit_base_rotation'] = (new_base_quat.w, new_base_quat.x, new_base_quat.y, new_base_quat.z)
            
            # Reset ALL sliders to zero
            self['orbit_pitch'] = 0.0
            self['orbit_yaw'] = 0.0
            self['screen_rotation'] = 0.0
            
            # Update local variables
            base_offset = new_base_offset
            base_quat = new_base_quat
            pitch_val = 0.0
            yaw_val = 0.0
            roll_val = 0.0
        
        # Update active axis tracker
        self['orbit_active_axis'] = current_axis
        
        # Get camera-local axes from the BASE orientation
        local_x = base_quat @ Vector((1.0, 0.0, 0.0))  # Camera's right
        local_y = base_quat @ Vector((0.0, 1.0, 0.0))  # Camera's up
        
        # Build rotation quaternion based on active axis
        if current_axis == "pitch":
            delta_rot = Quaternion(local_x, -pitch_val)
        elif current_axis == "yaw":
            delta_rot = Quaternion(local_y, -yaw_val)
        else:
            delta_rot = Quaternion()  # No orbit rotation
        
        # Apply delta rotation to get new camera position
        new_offset = delta_rot @ base_offset
        new_pos = center + new_offset
        
        # Apply delta rotation to get new orientation
        new_quat = delta_rot @ base_quat
        
        # Apply Roll (screen_rotation) - always on top of orbit rotation
        if current_axis == "roll" or abs(roll_val) > 0.0001:
            roll_quat = Quaternion((0, 0, 1), roll_val)
            new_quat = new_quat @ roll_quat
        
        new_euler = new_quat.to_euler('XYZ')
        
        # Update internal properties directly (no callback triggers)
        self['loc_x'] = new_pos.x
        self['loc_y'] = new_pos.y
        self['loc_z'] = new_pos.z
        self['rot_x'] = new_euler.x
        self['rot_y'] = new_euler.y
        self['rot_z'] = new_euler.z
        
        # Apply to camera or viewport
        if self.is_camera_mode and context.scene.camera:
            context.scene.camera.location = new_pos
            context.scene.camera.rotation_euler = new_euler
        else:
            set_view_location(context, new_pos, new_euler)

        # Keep zoom continuity after orbit changes by rebasing its hidden base
        # while preserving the current zoom slider value.
        self.invalidate_zoom_state(new_pos, new_quat, preserve_value=True)
    finally:
        controller.end_update()


def update_space_toggle(self, context):
    if not self.init_complete: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.INTERNAL_SYNC, LockPriority.NORMAL):
        return
    
    try:
        if self.use_screen_space:
            # Entering pan mode - reset pan values and disable orbit (modes are exclusive)
            current_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
            current_rot = (self.rot_x, self.rot_y, self.rot_z)
            self.invalidate_pan_state(current_pos, current_rot)
            self.invalidate_orbit_state(current_pos, disable_mode=True)
    finally:
        controller.end_update()

def update_zoom_level(self, context):
    if not self.init_complete: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        # Start grace period during drag
        controller.start_grace_period(0.2)
        
        if context.space_data.type == 'VIEW_3D':
            # Camera mode: zoom is direct dolly distance along camera's local Z
            if self.is_camera_mode and context.scene.camera:
                cam = context.scene.camera
                
                # Note: zoom_level UI is hidden for ortho cameras (handled via ortho_scale slider)
                # For persp cameras: zoom is direct dolly distance
                # zoom_level = 0 means camera at base position
                dolly_value = self.zoom_level
                
                # If pan has moved us from base, update base to current position first
                if abs(self.screen_x) > 0.001 or abs(self.screen_z) > 0.001:
                    current_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
                    self.invalidate_pan_state(current_pos)
                
                # Apply dolly movement along camera's forward axis
                rot_quat = cam.rotation_euler.to_quaternion()
                forward = rot_quat @ Vector((0.0, 0.0, -1.0))  # Camera looks -Z
                
                # Base position is where camera was when screen space mode started
                base_pos = Vector(self.base_world_pos)
                # Move along forward axis by -dolly
                new_pos = base_pos + (forward * dolly_value)
                
                cam.location = new_pos
                self['loc_x'] = new_pos.x
                self['loc_y'] = new_pos.y
                self['loc_z'] = new_pos.z
                
                # Update orbit base while preserving current orbit slider values.
                self.invalidate_orbit_state(new_pos, rot_quat, preserve_slider_values=True)
            else:
                # Viewport mode: dolly exactly like camera mode - move eye along forward axis
                # This gives unlimited zoom range with consistent linear feel
                
                region = context.region_data
                if not region and context.space_data and context.space_data.type == 'VIEW_3D':
                    region = context.space_data.region_3d
                
                if region:
                    dolly_value = self.zoom_level
                    
                    # If pan has moved us from base, update base to current position first
                    # This prevents jump when switching from pan to zoom
                    if abs(self.screen_x) > 0.001 or abs(self.screen_z) > 0.001:
                        current_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
                        self.invalidate_pan_state(current_pos)
                    
                    # Get view's forward direction
                    rot_quat = region.view_rotation
                    forward = rot_quat @ Vector((0.0, 0.0, -1.0))  # View looks -Z
                    
                    # Calculate new eye position from base position
                    base_pos = Vector(self.base_world_pos)
                    new_eye = base_pos + (forward * dolly_value)
                    
                    # Convert eye position back to view_location (pivot point)
                    # view_location = eye - (rotation @ forward) * view_distance
                    view_z = rot_quat @ Vector((0.0, 0.0, 1.0))
                    new_view_location = new_eye - (view_z * region.view_distance)
                    
                    region.view_location = new_view_location
                    
                    if not region.is_perspective:
                        self['focal_length'] = region.view_distance
                    
                    self['loc_x'] = new_eye.x
                    self['loc_y'] = new_eye.y
                    self['loc_z'] = new_eye.z
                    
                    # Update orbit base while preserving current orbit slider values.
                    self.invalidate_orbit_state(new_eye, rot_quat, preserve_slider_values=True)
    finally:
        controller.end_update()

def update_reset_axis(self, context):
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        # Track which screen axis was reset for per-axis restoration
        reset_x = self.reset_screen_x
        reset_z = self.reset_screen_z
        reset_rot = self.reset_screen_rotation
        
        if self.reset_loc_x: self['loc_x'] = 0.0; self['reset_loc_x'] = False
        if self.reset_loc_y: self['loc_y'] = 0.0; self['reset_loc_y'] = False
        if self.reset_loc_z: self['loc_z'] = 0.0; self['reset_loc_z'] = False
        if self.reset_screen_x: self['screen_x'] = 0.0; self['reset_screen_x'] = False
        if self.reset_screen_z: self['screen_z'] = 0.0; self['reset_screen_z'] = False
        if self.reset_screen_rotation: 
            self['screen_rotation'] = 0.0
            # Restore rotation to base
            self['rot_x'] = self.base_rotation[0]
            self['rot_y'] = self.base_rotation[1]
            self['rot_z'] = self.base_rotation[2]
            self['reset_screen_rotation'] = False
        if self.reset_rot_x: self['rot_x'] = 1.5707964; self['reset_rot_x'] = False
        if self.reset_rot_y: self['rot_y'] = 0.0; self['reset_rot_y'] = False
        if self.reset_rot_z: self['rot_z'] = 0.0; self['reset_rot_z'] = False
        
        # Orbit resets - return to framed position
        orbit_reset = False
        if self.reset_orbit_pitch: 
            self['orbit_pitch'] = 0.0
            self['reset_orbit_pitch'] = False
            orbit_reset = True
        if self.reset_orbit_yaw: 
            self['orbit_yaw'] = 0.0
            self['reset_orbit_yaw'] = False
            orbit_reset = True
        
        # If any orbit value was reset, apply the orbit transform
        if orbit_reset and self.orbit_around_selection and self.orbit_initialized:
            update_orbit_transform(self, context)
            return  # Skip the normal transform application
        
        if context.space_data.type == 'VIEW_3D':
            # Recalculate position using remaining offsets
            # This will use the current (some reset to 0) screen_x/z values
            if reset_x or reset_z:
                # Get rotation for screen space calculation
                if self.is_camera_mode and context.scene.camera:
                    if hasattr(context.scene.camera, 'rotation_euler'):
                        rot_quat = context.scene.camera.rotation_euler.to_quaternion()
                    else: 
                         rot_quat = Quaternion((1,0,0,0))
                else:
                    region = context.region_data
                    if not region and context.space_data and context.space_data.type == 'VIEW_3D':
                        region = context.space_data.region_3d
                    
                    if region:
                        rot_quat = region.view_rotation
                    else: 
                         # Fallback
                         rot_quat = Quaternion((1,0,0,0))
                
                right = rot_quat @ Vector((1.0, 0.0, 0.0))
                up = rot_quat @ Vector((0.0, 1.0, 0.0))
                
                # Screen space panning: only U (horizontal) and V (vertical)
                offset = (right * self.screen_x) + (up * self.screen_z)
                base_pos = Vector(self.base_world_pos)
                target_pos = base_pos + offset
                
                self['loc_x'] = target_pos.x
                self['loc_y'] = target_pos.y
                self['loc_z'] = target_pos.z
            else:
                target_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
            
            target_rot = Euler((self.rot_x, self.rot_y, self.rot_z), 'XYZ')
            
            # Camera mode
            if self.is_camera_mode and context.scene.camera:
                cam = context.scene.camera
                cam.location = target_pos
                cam.rotation_euler = target_rot
            else:
                set_view_location(context, target_pos, target_rot)
    finally:
        controller.end_update() 

def update_lens_clip(self, context):
    if not self.init_complete: return
    if self.internal_lock: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        # Start grace period during drag
        controller.start_grace_period(0.2)

        if context.space_data.type == 'VIEW_3D':
            # Camera mode: modify camera data
            if self.is_camera_mode and context.scene.camera:
                cam_data = context.scene.camera.data
                cam_data.clip_start = self.clip_start
                cam_data.clip_end = self.clip_end
                if cam_data.type == 'PERSP':
                    cam_data.lens = self.focal_length
                else:
                    cam_data.ortho_scale = self.focal_length
                    # Reset pan values when ortho_scale changes to prevent jumps
                    current_pos = Vector((self.loc_x, self.loc_y, self.loc_z))
                    self['base_world_pos'] = (current_pos.x, current_pos.y, current_pos.z)
                    self['screen_x'] = 0.0
                    self['screen_z'] = 0.0
                    self['zoom_level'] = 0.0
            else:
                # Viewport mode
                if hasattr(context.space_data, 'region_3d'):
                    region = context.space_data.region_3d
                elif hasattr(context, 'region_data'):
                    region = context.region_data
                else:
                    return

                context.space_data.clip_start = self.clip_start
                context.space_data.clip_end = self.clip_end
                if region.is_perspective:
                    context.space_data.lens = self.focal_length
                else:
                    region.view_distance = self.focal_length
                    safe_dist = max(0.001, self.focal_length)
                    self['zoom_level'] = 10.0 / safe_dist
                    current_eye = get_view_location(context)
                    self['loc_x'] = current_eye.x
                    self['loc_y'] = current_eye.y
                    self['loc_z'] = current_eye.z
                    # Reset pan values for new ortho position
                    if self.use_screen_space:
                        self.invalidate_pan_state(current_eye)
                    # Keep orbit continuity for orthographic scale changes.
                    self.invalidate_orbit_state(
                        current_eye,
                        region.view_rotation,
                        preserve_slider_values=True
                    )
    finally:
        controller.end_update()

def update_fov(self, context):
    """Update focal length when FOV changes."""
    if not self.init_complete: return
    if self.internal_lock: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        if context.space_data.type == 'VIEW_3D':
            # Convert FOV (radians) to focal length using sensor width
            sensor_width = 36.0  # Default horizontal sensor size in mm
            fov_rad = self.field_of_view
            if fov_rad > 0.001:
                focal = sensor_width / (2.0 * math.tan(fov_rad / 2.0))
                
                self['focal_length'] = focal
                
                # Apply to camera or viewport
                if self.is_camera_mode and context.scene.camera:
                    context.scene.camera.data.lens = focal
                else:
                    context.space_data.lens = focal
    finally:
        controller.end_update()

def update_use_fov(self, context):
    """Sync use_fov toggle with camera's lens_unit property."""
    if not self.init_complete: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.INTERNAL_SYNC, LockPriority.NORMAL):
        return
    
    try:
        # Only sync lens_unit for camera mode
        if self.is_camera_mode and context.scene.camera:
            cam_data = context.scene.camera.data
            if self.use_fov:
                cam_data.lens_unit = 'FOV'
            else:
                cam_data.lens_unit = 'MILLIMETERS'
    finally:
        controller.end_update()

def update_perspective_toggle(self, context):
    """Toggle between perspective and orthographic view."""
    if not self.init_complete: return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.USER_DRAG, LockPriority.NORMAL):
        return
    
    try:
        if context.space_data.type == 'VIEW_3D':
            # Camera mode: change camera type
            if self.is_camera_mode and context.scene.camera:
                cam_data = context.scene.camera.data
                new_type = 'PERSP' if self.is_perspective else 'ORTHO'
                if cam_data.type != new_type:
                    cam_data.type = new_type
                    # Update focal_length to match new type
                    if self.is_perspective:
                        self['focal_length'] = cam_data.lens
                    else:
                        self['focal_length'] = cam_data.ortho_scale
            else:
                # Viewport mode
                if hasattr(context.space_data, 'region_3d'):
                    region = context.space_data.region_3d
                elif hasattr(context, 'region_data'):
                    region = context.region_data
                else:
                    return

                if region.is_perspective != self.is_perspective:
                    bpy.ops.view3d.view_persportho()
                    
                    if self.is_perspective:
                        self['focal_length'] = context.space_data.lens
                    else:
                        self['focal_length'] = region.view_distance
    finally:
        controller.end_update()

# Camera Dropdown Callbacks
def get_camera_items(self, context):
    """Callback for EnumProperty to list all cameras in the scene."""
    items = []
    
    # List all cameras in the scene
    if context and hasattr(context, 'scene') and context.scene:
        for obj in context.scene.objects:
            if obj.type == 'CAMERA':
                items.append((obj.name, obj.name, f"Selected camera: {obj.name}"))
    
    # Fallback if no cameras exist
    if not items:
        items.append(('NONE', "No Cameras", "No cameras in scene"))
    
    return items

def update_camera_enum(self, context):
    """Handle camera selection change - switch cameras or exit."""
    if not self.init_complete:
        return
    
    controller = get_controller()
    if not controller.begin_update(UpdateSource.CAMERA_SWITCH, LockPriority.HIGH):
        return
    
    try:
        selected = self.camera_enum
        
        # Resolve region robustly
        _, _, region = find_view3d_context(context)
        
        was_in_camera = (region and region.view_perspective == 'CAMERA')
        
        if selected == 'NONE':
            # User selected "exit" - use proper exit mechanism
            if was_in_camera:
                bpy.ops.view3d.view_camera()  # Exit using Blender's mechanism
            # Note: we don't clear scene.camera here, just exit the view
        else:
            # User selected a different camera - switch to it
            cam_obj = context.scene.objects.get(selected)
            if cam_obj and cam_obj.type == 'CAMERA':
                context.scene.camera = cam_obj
                
                # If keep_camera_active mode is on, also make this camera the active object
                if self.keep_camera_active:
                    cam_obj.select_set(True)
                    context.view_layer.objects.active = cam_obj
                
                # If we weren't in camera view, enter it now
                if not was_in_camera:
                    bpy.ops.view3d.view_camera()
        
        # Sync UI properties immediately
        self.reinitialize_from_context(context)
    finally:
        controller.end_update()


# Saved Views Callbacks
def get_saved_views_items(self, context):
    """Callback for EnumProperty to list saved views.
    
    Note: Items must be cached to prevent Python garbage collection,
    which causes garbled Unicode text in Blender's EnumProperty.
    """
    from . import data_storage

    # Guard against nested RNA callback re-entry.
    if getattr(get_saved_views_items, "_building", False):
        cached = getattr(get_saved_views_items, "_cached_items", [])
        if cached:
            return cached
        return [('NONE', "-", "No saved view selected")]

    get_saved_views_items._building = True
    items = []
    saved_views = []
    stale_idx = -1
    
    # Determine the name for the 'Not Saved' / Ghost option
    none_label = "—"  # Default (Em Dash)
    
    if context and hasattr(context, 'scene') and context.scene:
        props = context.scene.viewpilot
        current_idx = context.scene.saved_views_index
        stale_idx = current_idx
        last_idx = props.last_active_view_index
        
        # Get views from JSON storage
        saved_views = data_storage.get_saved_views()
        
        # If we are effectively "unsaved" (index -1) but have a ghost tracking history
        if current_idx == -1 and last_idx != -1:
            try:
                # Get name of the last active view
                if 0 <= last_idx < len(saved_views):
                    last_view_name = saved_views[last_idx].get("name", "View")
                    # Safely format with asterisks (Unicode-safe)
                    none_label = f"*{last_view_name}*"
            except:
                pass

    try:
        # Always include the blank/None option to ensure list indices don't shift
        items.append(('NONE', none_label, "No saved view selected"))
        
        if not saved_views:
            saved_views = data_storage.get_saved_views()
        for i, view in enumerate(saved_views):
            # Use index as identifier (ASCII-safe), name for display
            items.append((str(i), view.get("name", f"View {i+1}"), f"View {i+1}"))

        # Transitional compatibility: if Blender is still holding a stale
        # enum value while scenes/files are syncing, include it once so RNA
        # doesn't spam warnings before our clamping logic runs.
        if stale_idx >= 0:
            stale_id = str(stale_idx)
            valid_ids = {item[0] for item in items}
            if stale_id not in valid_ids:
                items.append((stale_id, "(syncing)", "Temporary stale selection"))
        
        # CRITICAL: Cache items to prevent garbage collection of Unicode strings
        get_saved_views_items._cached_items = items
        return items
    finally:
        get_saved_views_items._building = False

# Initialize cache
get_saved_views_items._cached_items = []
get_saved_views_items._building = False



def invalidate_saved_views_enum_cache():
    """Invalidate cached items for saved views EnumProperty."""
    try:
        get_saved_views_items._cached_items = []
    except Exception:
        pass


def invalidate_saved_views_ui_caches():
    """Invalidate caches related to saved views UI (dropdown + panel icon view)."""
    invalidate_saved_views_enum_cache()
    try:
        from .preview_manager import invalidate_panel_gallery_cache
        invalidate_panel_gallery_cache()
    except Exception:
        pass


def _set_panel_gallery_enum_safe(self, preferred_value=None):
    """Set panel_gallery_enum to a valid runtime identifier."""
    controller = get_controller()
    prev_skip = controller.skip_enum_load

    seen = set()
    candidates = []

    if preferred_value is not None:
        candidates.append(str(preferred_value))

    scene = getattr(self, "id_data", None)
    view_count = 0
    current_idx = -1
    try:
        if scene and hasattr(scene, "saved_views"):
            view_count = len(scene.saved_views)
            current_idx = int(getattr(scene, "saved_views_index", -1))
    except Exception:
        view_count = 0
        current_idx = -1

    if view_count > 0:
        valid_ids = {str(i) for i in range(view_count)}
    else:
        valid_ids = {"NONE"}

    if view_count > 0:
        if 0 <= current_idx < view_count:
            candidates.append(str(current_idx))
        if 0 <= getattr(self, "last_active_view_index", -1) < view_count:
            candidates.append(str(self.last_active_view_index))
        candidates.append("0")
        candidates.append(str(view_count - 1))

    candidates.append("NONE")

    try:
        controller.skip_enum_load = True
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate not in valid_ids:
                continue
            try:
                self.panel_gallery_enum = candidate
                return True
            except Exception:
                continue
        return False
    finally:
        controller.skip_enum_load = prev_skip


def _sync_saved_view_selection_enums(self, controller, enum_value: str):
    """Keep dropdown and panel gallery enums in sync without triggering loads."""
    prev_skip = controller.skip_enum_load
    try:
        controller.skip_enum_load = True
        try:
            self.saved_views_enum = enum_value
        except Exception:
            pass
        # Normalize panel enum without reading current value first. Reading an
        # already-invalid enum can emit RNA warnings before we can fix it.
        _set_panel_gallery_enum_safe(self, enum_value)
    finally:
        controller.skip_enum_load = prev_skip


def _handle_saved_view_selection(self, context, enum_value: str):
    """Shared handler for selecting/loading a saved view from any UI source."""
    from . import data_storage

    controller = get_controller()

    # Always sync both enums for UI consistency (even during skip_enum_load)
    _sync_saved_view_selection_enums(self, controller, enum_value)

    # Skip LOADING if we're just syncing the display
    if controller.skip_enum_load:
        return

    # Handle blank/unselected option
    if enum_value == 'NONE':
        context.scene.saved_views_index = -1
        return

    if not controller.begin_update(UpdateSource.VIEW_RESTORE, LockPriority.CRITICAL):
        return

    try:
        with debug_tools.timed("saved_view.load.total"):
            index = int(enum_value)
            # Note: saved_views_index will be set AFTER apply_view_to_viewport
            # because apply may switch scenes, and we need to update the NEW scene's index

            # Reset ghost tracking when manually loading a view
            self.last_active_view_index = -1

            # Get view from JSON storage
            view_dict = data_storage.get_saved_view(index)
            if not view_dict:
                return

            # Resolve space and region robustly.
            _, space, region = find_view3d_context(context)

            if not space or not region:
                return

            # Apply the view to the viewport using data_storage helper
            # NOTE: This may switch scenes if remember_composition is True!
            data_storage.apply_view_to_viewport(view_dict, space, region, context)

            # IMPORTANT: After apply, context.scene may be different!
            # Update saved_views_index and enums on the CURRENT scene (which may be new)
            try:
                controller.skip_enum_load = True
                context.scene.saved_views_index = index
                context.scene.viewpilot.saved_views_enum = str(index)
                _set_panel_gallery_enum_safe(context.scene.viewpilot, str(index))
            finally:
                controller.skip_enum_load = False

            # Get rotation for history and property updates
            rotation = view_dict.get("rotation", [1.0, 0.0, 0.0, 0.0])
            rot_quat = Quaternion((rotation[0], rotation[1], rotation[2], rotation[3]))

            # Add this view state to history
            new_state = {
                'view_location': Vector(view_dict.get("location", [0, 0, 0])),
                'view_rotation': rot_quat,
                'view_distance': view_dict.get("distance", 10.0),
                'view_perspective': 'PERSP' if view_dict.get("is_perspective", True) else 'ORTHO',
                'is_perspective': view_dict.get("is_perspective", True),
                'lens': view_dict.get("lens", 50.0),
                'clip_start': view_dict.get("clip_start", 0.1),
                'clip_end': view_dict.get("clip_end", 1000.0),
                'timestamp': time.time()
            }

            add_to_history(new_state)

            # Lock history recording using grace period
            controller.start_grace_period(0.5, UpdateSource.VIEW_RESTORE)

            # Update properties using direct assignment (no callback triggers)
            pivot_pos = Vector(view_dict.get("location", [0, 0, 0]))
            view_z = Vector((0.0, 0.0, 1.0))
            distance = view_dict.get("distance", 10.0)
            eye_pos = pivot_pos + (rot_quat @ view_z) * distance

            self['loc_x'] = eye_pos.x
            self['loc_y'] = eye_pos.y
            self['loc_z'] = eye_pos.z

            euler = rot_quat.to_euler()
            self['rot_x'] = euler.x
            self['rot_y'] = euler.y
            self['rot_z'] = euler.z

            is_persp = view_dict.get("is_perspective", True)
            if is_persp:
                self['focal_length'] = view_dict.get("lens", 50.0)
            else:
                self['focal_length'] = distance
            self['clip_start'] = view_dict.get("clip_start", 0.1)
            self['clip_end'] = view_dict.get("clip_end", 1000.0)

            self['is_perspective'] = is_persp

            # Reset zoom to 0 (it's a relative delta, not absolute)
            # base_world_pos is the reference point for future zoom changes
            self['zoom_level'] = 0.0
            self['base_world_pos'] = (eye_pos.x, eye_pos.y, eye_pos.z)

            self['screen_x'] = 0.0
            self['screen_z'] = 0.0
    except (ValueError, AttributeError):
        pass
    finally:
        controller.end_update()



def update_panel_gallery_enum(self, context):
    """Load the selected view when panel gallery enum changes."""
    controller = get_controller()
    if controller.skip_enum_load:
        return

    enum_value = self.panel_gallery_enum

    # Handle blank/unselected option
    if enum_value == 'NONE':
        return

    _handle_saved_view_selection(self, context, enum_value)


def update_saved_views_enum(self, context):
    """Load the selected view when enum changes (for saved_views_enum dropdown)."""
    controller = get_controller()
    if controller.skip_enum_load:
        return
    _handle_saved_view_selection(self, context, self.saved_views_enum)


# ============================================================================
# HELPER: Sync SavedViewItem property changes to JSON storage
# ============================================================================

def _sync_view_to_json(view_item, context, prop_name):
    """Callback to sync a SavedViewItem property change to JSON storage."""
    # Skip during sync_to_all_scenes to prevent O(N*M) IO explosion
    from . import data_storage
    if data_storage.IS_SYNCING:
        return
    
    if not context or not hasattr(context, 'scene') or not context.scene:
        return
    
    # Find the index of this view item in the collection
    try:
        saved_views = context.scene.saved_views
        idx = -1
        for i, v in enumerate(saved_views):
            if v == view_item:
                idx = i
                break
        
        if idx < 0:
            return
        
        # Update the JSON storage
        from . import data_storage
        views = data_storage.get_saved_views()
        if 0 <= idx < len(views):
            # Get the current value from the PropertyGroup
            new_value = getattr(view_item, prop_name)
            views[idx][prop_name] = new_value
            
            # Save back to JSON
            data = data_storage.load_data()
            data["saved_views"] = views
            data_storage.save_data(data)
    except Exception as e:
        print(f"[ViewPilot] Failed to sync {prop_name} to JSON: {e}")


# ============================================================================
# PROPERTY GROUPS
# ============================================================================

class SavedViewItem(bpy.types.PropertyGroup):
    """Property group for a single saved view."""
    name: bpy.props.StringProperty(
        name="View Name",
        description="Name of the saved view",
        default="Saved View"
    )
    # Transform
    location: bpy.props.FloatVectorProperty(
        name="Location",
        size=3,
        default=(0.0, 0.0, 0.0)
    )
    rotation: bpy.props.FloatVectorProperty(
        name="Rotation",
        size=4,  # Quaternion (w, x, y, z)
        default=(1.0, 0.0, 0.0, 0.0)
    )
    distance: bpy.props.FloatProperty(
        name="Distance",
        default=10.0
    )
    # Lens
    view_location: bpy.props.FloatVectorProperty(name="Location")
    view_rotation: bpy.props.FloatVectorProperty(name="Rotation")  # Stored as Euler angles
    lens: bpy.props.FloatProperty(name="Lens")
    is_perspective: bpy.props.BoolProperty(name="Perspective")
    clip_start: bpy.props.FloatProperty(name="Clip Start")
    clip_end: bpy.props.FloatProperty(name="Clip End")
    view_distance: bpy.props.FloatProperty(name="View Distance")
    # Thumbnail
    thumbnail_image: bpy.props.StringProperty(
        name="Thumbnail Image",
        description="Name of the packed image used as thumbnail"
    )
    
    # =========================================================================
    # VIEW STYLES - Shading
    # =========================================================================
    shading_type: bpy.props.StringProperty(default="SOLID")  # WIREFRAME, SOLID, MATERIAL
    shading_light: bpy.props.StringProperty(default="STUDIO")  # STUDIO, MATCAP, FLAT
    shading_color_type: bpy.props.StringProperty(default="MATERIAL")  # MATERIAL, SINGLE, OBJECT, RANDOM, VERTEX, TEXTURE
    shading_single_color: bpy.props.FloatVectorProperty(size=3, default=(0.8, 0.8, 0.8))
    shading_background_type: bpy.props.StringProperty(default="THEME")  # THEME, WORLD, VIEWPORT
    shading_background_color: bpy.props.FloatVectorProperty(size=3, default=(0.05, 0.05, 0.05))  # Custom background
    shading_studio_light: bpy.props.StringProperty(default="")
    shading_studiolight_rotate_z: bpy.props.FloatProperty(default=0.0)
    shading_studiolight_intensity: bpy.props.FloatProperty(default=1.0)
    shading_studiolight_background_alpha: bpy.props.FloatProperty(default=0.0)
    shading_studiolight_background_blur: bpy.props.FloatProperty(default=0.5)
    shading_use_world_space_lighting: bpy.props.BoolProperty(default=False)
    shading_selected_world: bpy.props.StringProperty(default="")  # World datablock name for HDRI
    shading_show_cavity: bpy.props.BoolProperty(default=False)
    shading_cavity_type: bpy.props.StringProperty(default="WORLD")  # WORLD, SCREEN, BOTH
    shading_cavity_ridge_factor: bpy.props.FloatProperty(default=1.0)
    shading_cavity_valley_factor: bpy.props.FloatProperty(default=1.0)
    shading_curvature_ridge_factor: bpy.props.FloatProperty(default=1.0)
    shading_curvature_valley_factor: bpy.props.FloatProperty(default=1.0)
    shading_show_object_outline: bpy.props.BoolProperty(default=False)
    shading_object_outline_color: bpy.props.FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    shading_show_xray: bpy.props.BoolProperty(default=False)
    shading_xray_alpha: bpy.props.FloatProperty(default=0.5)
    shading_show_shadows: bpy.props.BoolProperty(default=False)
    shading_shadow_intensity: bpy.props.FloatProperty(default=0.5)
    # Material Preview specific
    shading_use_scene_lights: bpy.props.BoolProperty(default=False)
    shading_use_scene_world: bpy.props.BoolProperty(default=False)
    
    # =========================================================================
    # VIEW STYLES - Overlays
    # =========================================================================
    overlays_show_overlays: bpy.props.BoolProperty(default=True)
    overlays_show_floor: bpy.props.BoolProperty(default=True)
    overlays_show_axis_x: bpy.props.BoolProperty(default=True)
    overlays_show_axis_y: bpy.props.BoolProperty(default=True)
    overlays_show_axis_z: bpy.props.BoolProperty(default=False)
    overlays_show_text: bpy.props.BoolProperty(default=True)
    overlays_show_cursor: bpy.props.BoolProperty(default=True)
    overlays_show_outline_selected: bpy.props.BoolProperty(default=True)
    overlays_show_wireframes: bpy.props.BoolProperty(default=False)
    overlays_wireframe_threshold: bpy.props.FloatProperty(default=1.0)
    overlays_wireframe_opacity: bpy.props.FloatProperty(default=1.0)
    overlays_show_face_orientation: bpy.props.BoolProperty(default=False)
    overlays_show_relationship_lines: bpy.props.BoolProperty(default=True)
    overlays_show_bones: bpy.props.BoolProperty(default=True)
    overlays_show_motion_paths: bpy.props.BoolProperty(default=True)
    overlays_show_object_origins: bpy.props.BoolProperty(default=True)
    overlays_show_annotation: bpy.props.BoolProperty(default=True)
    overlays_show_extras: bpy.props.BoolProperty(default=True)
    
    # =========================================================================
    # VIEW STYLES - Composition
    # =========================================================================
    composition_scene: bpy.props.StringProperty(default="")
    composition_view_layer: bpy.props.StringProperty(default="")
    
    # =========================================================================
    # VIEW STYLES - Per-View Toggles (control navigation, not storage)
    # with update callback to sync changes to JSON storage
    # =========================================================================
    remember_perspective: bpy.props.BoolProperty(
        name="Perspective",
        description="Apply saved camera position when navigating to this view",
        default=True,
        update=lambda self, ctx: _sync_view_to_json(self, ctx, "remember_perspective")
    )
    remember_shading: bpy.props.BoolProperty(
        name="Shading",
        description="Apply saved viewport shading options when navigating to this view",
        default=True,
        update=lambda self, ctx: _sync_view_to_json(self, ctx, "remember_shading")
    )
    remember_overlays: bpy.props.BoolProperty(
        name="Overlays",
        description="Apply saved overlay options when navigating to this view",
        default=True,
        update=lambda self, ctx: _sync_view_to_json(self, ctx, "remember_overlays")
    )
    remember_composition: bpy.props.BoolProperty(
        name="Composition",
        description="Switch to saved Scene/View Layer when navigating to this view",
        default=True,
        update=lambda self, ctx: _sync_view_to_json(self, ctx, "remember_composition")
    )

class ViewPilotProperties(bpy.types.PropertyGroup):
    """Shared properties for ViewPilot (Popup, Popover, N-Panel)."""
    
    init_complete: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})
    internal_lock: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})
    
    is_perspective: bpy.props.BoolProperty(
        name="Perspective",
        default=True,
        description="Toggle between Perspective and Orthographic view",
        update=update_perspective_toggle,
        options={'SKIP_SAVE'}
    )
    
    use_screen_space: bpy.props.BoolProperty(
        name="Panning mode", 
        default=False, 
        description="Control camera in screen space, instead of 3D",
        update=update_space_toggle
    )
    base_world_pos: bpy.props.FloatVectorProperty(size=3, default=(0,0,0), options={'SKIP_SAVE'})

    focal_length: bpy.props.FloatProperty(name="Focal Length", default=50.0, min=0.01, step=100, update=update_lens_clip)
    use_fov: bpy.props.BoolProperty(
        name="Use FOV",
        default=True,
        description="Switch between field of view (degrees) or focal length (mm)",
        update=update_use_fov,
        options={'SKIP_SAVE'}
    )
    field_of_view: bpy.props.FloatProperty(
        name="Field of View",
        default=0.6911,  # ~39.6 degrees (equivalent to 50mm)
        min=0.01,
        max=3.14,  # ~180 degrees
        step=10,
        unit='ROTATION',
        update=update_fov,
        options={'SKIP_SAVE'}
    )

    clip_start: bpy.props.FloatProperty(name="Start", description="Clipping near distance", default=0.1, step=1, precision=2, min=0.001, subtype='DISTANCE', update=update_lens_clip)
    clip_end: bpy.props.FloatProperty(name="End", description="Clipping far distance", default=1000.0, step=100, precision=2, subtype='DISTANCE', update=update_lens_clip)

    zoom_level: bpy.props.FloatProperty(name="Zoom Level", description="Dolly distance (negative=in, positive=out)", default=0.0, step=10, precision=2, update=update_zoom_level)

    loc_x: bpy.props.FloatProperty(name="X", description="Move camera object on X axis", precision=2, step=10, update=update_view_transform)
    loc_y: bpy.props.FloatProperty(name="Y", description="Move camera object on Y axis", precision=2, step=10, update=update_view_transform)
    loc_z: bpy.props.FloatProperty(name="Z", description="Move camera object on Z axis", precision=2, step=10, update=update_view_transform)
    
    reset_loc_x: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)
    reset_loc_y: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)
    reset_loc_z: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)
    
    screen_x: bpy.props.FloatProperty(name="X", description="Move screen horizontally", precision=2, step=1, update=update_screen_space_transform)
    screen_z: bpy.props.FloatProperty(name="Z", description="Move screen vertically", precision=2, step=1, update=update_screen_space_transform)
    screen_rotation: bpy.props.FloatProperty(name="Rot", description="Rotate screen around its center", unit='ROTATION', precision=3, step=100, update=update_screen_rotation)
    
    base_rotation: bpy.props.FloatVectorProperty(size=3, default=(0,0,0), options={'SKIP_SAVE'})
    base_view_distance: bpy.props.FloatProperty(default=10.0, options={'SKIP_SAVE'})  # For viewport zoom
    
    reset_screen_x: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)
    reset_screen_z: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)
    reset_screen_rotation: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)

    rot_x: bpy.props.FloatProperty(name="X", description="Rotate camera object on X axis", unit='ROTATION', precision=3, step=100, update=update_view_transform)
    rot_y: bpy.props.FloatProperty(name="Y", description="Rotate camera object on Y axis", unit='ROTATION', precision=3, step=100, update=update_view_transform)
    rot_z: bpy.props.FloatProperty(name="Z", description="Rotate camera object on Z axis", unit='ROTATION', precision=3, step=100, update=update_view_transform)

    reset_rot_x: bpy.props.BoolProperty(description="Reset to 90", update=update_reset_axis)
    reset_rot_y: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)
    reset_rot_z: bpy.props.BoolProperty(description="Reset to 0", update=update_reset_axis)
    
    # Orbit Mode Properties
    orbit_around_selection: bpy.props.BoolProperty(
        name="Orbit Selection",
        description="Orbit camera around selected object(s) (Trackball mode)",
        default=False,
        update=update_orbit_mode_toggle
    )
    orbit_pitch: bpy.props.FloatProperty(name="Pitch", description="Vertical orbit", unit='ROTATION', precision=3, step=100, update=update_orbit_transform)
    orbit_yaw: bpy.props.FloatProperty(name="Yaw", description="Horizontal orbit", unit='ROTATION', precision=3, step=100, update=update_orbit_transform)
    
    reset_orbit_pitch: bpy.props.BoolProperty(description="Reset Pitch to 0", update=update_reset_axis)
    reset_orbit_yaw: bpy.props.BoolProperty(description="Reset Yaw to 0", update=update_reset_axis)
    
    orbit_center: bpy.props.FloatVectorProperty(size=3, default=(0,0,0), options={'SKIP_SAVE'})
    orbit_distance: bpy.props.FloatProperty(default=0.0, options={'SKIP_SAVE'})
    orbit_initialized: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})
    orbit_base_offset: bpy.props.FloatVectorProperty(size=3, default=(0,0,0), options={'SKIP_SAVE'})
    orbit_base_rotation: bpy.props.FloatVectorProperty(size=4, default=(1,0,0,0), options={'SKIP_SAVE'})  # Quaternion WXYZ
    orbit_active_axis: bpy.props.StringProperty(default="", options={'SKIP_SAVE'})  # "pitch", "yaw", "roll", or "" for none
    
    # Camera mode properties
    is_camera_mode: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})
    tracked_camera_name: bpy.props.StringProperty(default="", options={'SKIP_SAVE'})
    keep_camera_active: bpy.props.BoolProperty(name="Keep Camera Active", default=False, options={'SKIP_SAVE'})
    
    # Track which saved view was active before modification (Ghost View tracking)
    last_active_view_index: bpy.props.IntProperty(default=-1, options={'SKIP_SAVE'})
    
    # Camera dropdown (for switching)
    camera_enum: bpy.props.EnumProperty(
        name="Camera",
        description="Switch cameras",
        items=get_camera_items,
        update=update_camera_enum,
        options={'SKIP_SAVE'}
    )
    
    # Saved views dropdown
    saved_views_enum: bpy.props.EnumProperty(
        name="Saved Views",
        description="Select a saved view",
        items=get_saved_views_items,
        update=update_saved_views_enum,
        options={'SKIP_SAVE'}
    )
    
    # Panel gallery (icon view with thumbnails)
    # Uses preview collection for custom thumbnail icons
    def _get_panel_gallery_items(self, context):
        """Generate items with thumbnail icons for panel gallery."""
        try:
            from .preview_manager import get_panel_gallery_items
            return get_panel_gallery_items(self, context)
        except:
            return [('NONE', "No Views", "", 0, 0)]
    
    panel_gallery_enum: bpy.props.EnumProperty(
        name="Panel Gallery",
        description="Select a saved view (thumbnail preview)",
        items=_get_panel_gallery_items,
        update=update_panel_gallery_enum,
        options={'SKIP_SAVE'}
    )
    
    # =========================================================================
    # STATE INVALIDATION METHODS
    # =========================================================================
    # These centralize the logic for resetting relative properties when position
    # changes. Instead of scattering resets throughout update functions, call
    # the appropriate invalidation method.
    
    def invalidate_zoom_state(self, new_pos, new_rot_quat=None, preserve_value=False):
        """Rebase dolly zoom after position changes.
        
        Call when: Position changes from any source except zoom itself.
        Args:
            new_rot_quat: Quaternion rotation (required when preserve_value=True).
            preserve_value: Keep current zoom slider value and shift the hidden
                base reference so only future deltas are applied.
        """
        if preserve_value and new_rot_quat is not None:
            forward = new_rot_quat @ Vector((0.0, 0.0, -1.0))
            base_pos = new_pos - (forward * self.zoom_level)
            self['base_world_pos'] = (base_pos.x, base_pos.y, base_pos.z)
            return
        
        self['base_world_pos'] = (new_pos.x, new_pos.y, new_pos.z)
        self['zoom_level'] = 0.0
    
    def invalidate_pan_state(self, new_pos, new_rot=None, disable_mode=False):
        """Reset screen-space pan values to work from new position.
        
        Call when: Position changes from any source except pan itself.
        Args:
            disable_mode: If True, also turn off use_screen_space toggle.
        """
        if disable_mode:
            self['use_screen_space'] = False
        
        self['base_world_pos'] = (new_pos.x, new_pos.y, new_pos.z)
        self['screen_x'] = 0.0
        self['screen_z'] = 0.0
        
        if new_rot:
            if hasattr(new_rot, '__iter__') and len(new_rot) == 3:
                self['base_rotation'] = (new_rot[0], new_rot[1], new_rot[2])
            else:
                self['base_rotation'] = (new_rot.x, new_rot.y, new_rot.z)
            self['screen_rotation'] = 0.0
    
    def _resolve_orbit_axis_for_rebase(self, epsilon=0.0001):
        """Resolve active orbit axis for continuity rebasing.
        
        Returns:
            "pitch" | "yaw" | "roll" when a single axis is active,
            "" when all sliders are approximately zero,
            None when multiple axes are non-zero (ambiguous state).
        """
        pitch_nonzero = abs(self.orbit_pitch) > epsilon
        yaw_nonzero = abs(self.orbit_yaw) > epsilon
        roll_nonzero = abs(self.screen_rotation) > epsilon
        
        nonzero_axes = []
        if pitch_nonzero:
            nonzero_axes.append("pitch")
        if yaw_nonzero:
            nonzero_axes.append("yaw")
        if roll_nonzero:
            nonzero_axes.append("roll")
        
        if not nonzero_axes:
            return ""
        if len(nonzero_axes) > 1:
            return None
        return nonzero_axes[0]
    
    def invalidate_orbit_state(
        self,
        new_pos,
        new_rot_quat=None,
        disable_mode=False,
        preserve_slider_values=False
    ):
        """Update orbit base to work from new position.
        
        Call when: Position changes from any source except orbit itself.
        Args:
            new_rot_quat: Quaternion rotation (optional, for recalculating base)
            disable_mode: If True, completely disable orbit mode.
            preserve_slider_values: Keep current orbit slider values and
                back-solve the base so only future deltas are applied.
        """
        if disable_mode:
            self['orbit_around_selection'] = False
            self['orbit_initialized'] = False
            return
        
        # Only update base if orbit is currently active and initialized
        if self.orbit_around_selection and self.orbit_initialized:
            center = Vector(self.orbit_center)
            current_offset = new_pos - center
            base_offset = current_offset
            base_quat = new_rot_quat if new_rot_quat is not None else None
            
            if preserve_slider_values and new_rot_quat is not None:
                pose_quat = new_rot_quat.normalized()
                axis = self._resolve_orbit_axis_for_rebase()
                
                if axis is None:
                    # Ambiguous multi-axis state: safest fallback is to commit
                    # current pose and clear orbit sliders.
                    self['orbit_pitch'] = 0.0
                    self['orbit_yaw'] = 0.0
                    self['screen_rotation'] = 0.0
                    self['orbit_active_axis'] = ""
                    base_offset = current_offset
                    base_quat = pose_quat
                elif axis == "pitch":
                    pitch_val = self.orbit_pitch
                    axis_world = pose_quat @ Vector((1.0, 0.0, 0.0))
                    delta_rot = Quaternion(axis_world, -pitch_val)
                    delta_inv = delta_rot.inverted()
                    base_offset = delta_inv @ current_offset
                    base_quat = delta_inv @ pose_quat
                    self['orbit_active_axis'] = "pitch"
                elif axis == "yaw":
                    yaw_val = self.orbit_yaw
                    axis_world = pose_quat @ Vector((0.0, 1.0, 0.0))
                    delta_rot = Quaternion(axis_world, -yaw_val)
                    delta_inv = delta_rot.inverted()
                    base_offset = delta_inv @ current_offset
                    base_quat = delta_inv @ pose_quat
                    self['orbit_active_axis'] = "yaw"
                elif axis == "roll":
                    roll_val = self.screen_rotation
                    roll_quat = Quaternion((0.0, 0.0, 1.0), roll_val)
                    base_offset = current_offset
                    base_quat = pose_quat @ roll_quat.inverted()
                    self['orbit_active_axis'] = "roll"
                else:
                    base_offset = current_offset
                    base_quat = pose_quat
                    self['orbit_active_axis'] = ""
            
            self['orbit_base_offset'] = (base_offset.x, base_offset.y, base_offset.z)
            self['orbit_distance'] = base_offset.length
            
            if base_quat is not None:
                self['orbit_base_rotation'] = (base_quat.w, base_quat.x, base_quat.y, base_quat.z)
    
    def invalidate_all_relative_state(self, new_pos, new_rot=None, new_rot_quat=None):
        """Reset ALL relative state when absolute position changes.
        
        Call when: loc/rot changed directly, view restored, reinitialize, etc.
        Respects mode exclusivity - doesn't disable modes, just resets values.
        """
        # Reset dolly zoom
        self.invalidate_zoom_state(new_pos)
        
        # Reset pan (screen space) values
        self.invalidate_pan_state(new_pos, new_rot)
        
        # Reset orbit values (if orbit mode is active)
        self.invalidate_orbit_state(new_pos, new_rot_quat)
    
    def reinitialize_from_context(self, context):
        """Reinitialize all properties from current context (viewport or camera)."""
        # Prevent re-entry
        if getattr(self, '_is_reinitializing', False):
            return
        self._is_reinitializing = True
        
        try:
            # 1. Resolve Space and Region robustly.
            _, space, region = find_view3d_context(context)
            if not space or not region:
                return

            # 2. Begin Update - Now it is safe to flag as incomplete
            self.init_complete = False
            self.internal_lock = True
            
            # Check if we're in camera view
            active_cam = context.scene.camera
            self.is_camera_mode = (region.view_perspective == 'CAMERA' and active_cam is not None)
            
            self.tracked_camera_name = active_cam.name if active_cam else ""
            
            # Sync camera dropdown to show current camera
            # Note: Dynamic enums require try/except as the item might not exist yet
            try:
                if active_cam:
                    self.camera_enum = active_cam.name
                else:
                    self.camera_enum = 'NONE'
            except TypeError:
                pass  # Enum items not yet populated
            
            # Sync panel gallery enum to current saved view index
            try:
                controller = get_controller()
                controller.skip_enum_load = True
                if hasattr(context.scene, 'saved_views_index'):
                    idx = context.scene.saved_views_index
                    if idx >= 0:
                        _set_panel_gallery_enum_safe(self, str(idx))
                    else:
                        _set_panel_gallery_enum_safe(self, 'NONE')
                controller.skip_enum_load = False
            except:
                if 'controller' in locals():
                    controller.skip_enum_load = False
            
            if self.is_camera_mode and active_cam:
                # Initialize from active camera properties
                cam_data = active_cam.data
                
                self.is_perspective = (cam_data.type == 'PERSP')
                self.clip_start = cam_data.clip_start
                self.clip_end = cam_data.clip_end
                
                self.use_fov = (cam_data.lens_unit == 'FOV')
                
                if self.is_perspective:
                    self.focal_length = cam_data.lens
                    self.field_of_view = cam_data.angle
                else:
                    self.focal_length = cam_data.ortho_scale
                
                self.loc_x, self.loc_y, self.loc_z = active_cam.location
                rot = active_cam.rotation_euler
                self.rot_x, self.rot_y, self.rot_z = rot.x, rot.y, rot.z
                
                self.zoom_level = 0.0  # Start at base position
                
                self.base_world_pos = (active_cam.location.x, active_cam.location.y, active_cam.location.z)
                self.base_rotation = (rot.x, rot.y, rot.z)
                self.screen_x = 0.0
                self.screen_z = 0.0
                self.screen_rotation = 0.0
            else:
                # Initialize from viewport
                self.is_perspective = region.is_perspective
                
                self.clip_start = space.clip_start
                self.clip_end = space.clip_end
                
                # Set use_fov from preference
                try:
                    from .preferences import get_preferences
                    self.use_fov = get_preferences().default_lens_unit == 'FOV'
                except:
                    self.use_fov = True  # Default to FOV
                
                if self.is_perspective:
                    self.focal_length = space.lens
                    sensor_width = 36.0
                    if space.lens > 0.1:
                        self.field_of_view = 2.0 * math.atan(sensor_width / (2.0 * space.lens))
                    else:
                            self.field_of_view = 1.0
                else:
                    self.focal_length = region.view_distance
                
                # Calculate View Location (Eye) manually
                view_rot = region.view_rotation
                view_z = Vector((0.0, 0.0, 1.0))
                view_dist = region.view_distance
                current_pos = region.view_location + (view_rot @ view_z) * view_dist
                
                self.loc_x, self.loc_y, self.loc_z = current_pos
                
                if hasattr(region, 'view_rotation'):
                    current_rot = region.view_rotation.to_euler()
                    self.rot_x, self.rot_y, self.rot_z = current_rot
                
                # Store base view distance for zoom calculations
                self.base_view_distance = region.view_distance
                self.zoom_level = 0.0  # Start at base position
                
                self.base_world_pos = (current_pos.x, current_pos.y, current_pos.z)
                self.base_rotation = (self.rot_x, self.rot_y, self.rot_z)
                self.screen_x = 0.0
                self.screen_z = 0.0
                # Reset screen space values
                self.screen_rotation = 0.0
                # Note: orbit_around_selection is auto-disabled by the modal monitor
                # when external movement is detected, so no sync logic needed here.
            
            self.internal_lock = False
            self.init_complete = True
        finally:
            self._is_reinitializing = False

def register():
    bpy.utils.register_class(SavedViewItem)
    bpy.utils.register_class(ViewPilotProperties)

def unregister():
    bpy.utils.unregister_class(ViewPilotProperties)
    bpy.utils.unregister_class(SavedViewItem)
