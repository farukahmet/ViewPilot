"""
Utility functions and global state for ViewPilot
"""

import bpy
import time
from mathutils import Vector, Quaternion
from bpy.app.handlers import persistent


# ============================================================================
# GLOBAL STATE
# ============================================================================

VIEW_HISTORY_MAX = 20
view_history = []             # List of state dictionaries
view_history_index = -1       # Current position in history (-1 means "Live/Newest")
active_popup_operator = None  # Reference to active popup for UI updates
monitor_running = False       # Prevents multiple monitor instances

# NOTE: Lock state is now managed by StateController - see state_controller.py
# Removed: restoration_lock_until, skip_enum_load, property_update_lock_until


# ============================================================================
# VIEW_3D CONTEXT UTILITIES
# ============================================================================

def _get_view3d_space_region(area):
    """Return (space, region_3d) for a VIEW_3D area, else (None, None)."""
    if not area or area.type != 'VIEW_3D':
        return (None, None)
    for space in area.spaces:
        if space.type == 'VIEW_3D':
            region = getattr(space, "region_3d", None)
            if region:
                return (space, region)
    return (None, None)


def _get_view3d_window_region(area):
    """Return WINDOW region for a VIEW_3D area, else None."""
    if not area or area.type != 'VIEW_3D':
        return None
    for region in area.regions:
        if region.type == 'WINDOW':
            return region
    return None


def _find_view3d_area_for_space(context, target_space):
    """Resolve VIEW_3D area containing target space across all windows/screens."""
    if not target_space:
        return None

    wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
    if wm:
        for window in wm.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for space in area.spaces:
                    if space == target_space:
                        return area

    if context.screen:
        for area in context.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space == target_space:
                    return area

    return None


def find_window_for_area(context, target_area):
    """Resolve the Blender window that owns a given area."""
    if not target_area:
        return None

    wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
    if not wm:
        return None

    for window in wm.windows:
        screen = window.screen
        if not screen:
            continue
        for area in screen.areas:
            if area == target_area:
                return window
    return None


def find_view3d_area_at_mouse(context, mouse_x, mouse_y, exclude_area=None):
    """Find the VIEW_3D area under global mouse coordinates."""
    wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
    if not wm:
        return None

    for window in wm.windows:
        screen = window.screen
        if not screen:
            continue
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            if exclude_area is not None and area == exclude_area:
                continue
            if (
                area.x <= mouse_x < area.x + area.width and
                area.y <= mouse_y < area.y + area.height
            ):
                return area
    return None


def tag_redraw_all_view3d(context=None):
    """Tag redraw on all VIEW_3D areas across all windows."""
    try:
        ctx = context or bpy.context
        wm = getattr(ctx, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if not wm:
            return
        for window in wm.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _resolve_preferred_view3d_area(context, preferred_area):
    """Validate and resolve preferred VIEW_3D area across all open windows."""
    if not preferred_area or preferred_area.type != 'VIEW_3D':
        return None

    wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
    if wm:
        for window in wm.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area == preferred_area and area.type == 'VIEW_3D':
                    return area

    if context.screen:
        for area in context.screen.areas:
            if area == preferred_area and area.type == 'VIEW_3D':
                return area

    return None


def find_view3d_context(context, preferred_area=None):
    """
    Find VIEW_3D area, space, and region from any context.
    
    Optionally tries a preferred VIEW_3D area first (for cross-area workflows
    like modal gallery actions invoked from non-VIEW_3D regions).
    
    Useful when operating from non-3D contexts (TOPBAR, timers, etc.)
    Returns (area, space, region_3d) tuple, or (None, None, None) if not found.
    """
    # Preferred area first (if provided and still valid)
    area = _resolve_preferred_view3d_area(context, preferred_area)
    if area:
        space, region = _get_view3d_space_region(area)
        if space and region:
            return (area, space, region)

    # Direct context first (fastest path)
    if context.space_data and context.space_data.type == 'VIEW_3D':
        area = context.area if context.area and context.area.type == 'VIEW_3D' else None
        region = context.region_data or getattr(context.space_data, "region_3d", None)
        if region:
            return (area, context.space_data, region)
    
    # Fall back to searching screen
    if context.screen:
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                space, region = _get_view3d_space_region(area)
                if space and region:
                    return (area, space, region)

    # Last resort: scan all windows/screens.
    wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
    if wm:
        for window in wm.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    space, region = _get_view3d_space_region(area)
                    if space and region:
                        return (area, space, region)

    return (None, None, None)


def find_view3d_override_context(context, preferred_area=None):
    """
    Find VIEW_3D area/space/WINDOW-region tuple for temp overrides.

    This wraps find_view3d_context() and converts region_3d lookup to the
    corresponding WINDOW region required by context.temp_override().
    """
    area, space, region_3d = find_view3d_context(context, preferred_area=preferred_area)
    if not space or not region_3d:
        return (None, None, None)

    if not area and context.area and context.area.type == 'VIEW_3D':
        area = context.area
    if not area:
        area = _find_view3d_area_for_space(context, space)
    if not area:
        return (None, None, None)

    window_region = _get_view3d_window_region(area)
    if not window_region:
        return (None, None, None)
    return (area, space, window_region)


# ============================================================================
# VIEW LOCATION HELPERS
# ============================================================================

def get_view_location(context):
    """Calculate the actual 'Eye' position of the viewport camera."""
    region = context.region_data
    if not region:
        if context.space_data and context.space_data.type == 'VIEW_3D':
            region = context.space_data.region_3d
    if not region: return Vector((0,0,0))
    
    rot = region.view_rotation
    view_z = Vector((0.0, 0.0, 1.0))
    dist = region.view_distance
    return region.view_location + (rot @ view_z) * dist


def set_view_location(context, target_pos, target_rot_euler):
    """Reverse engineer the Pivot Point to place the Eye at target_pos."""
    region = context.region_data
    if not region:
        if context.space_data and context.space_data.type == 'VIEW_3D':
            region = context.space_data.region_3d
    if not region: return

    dist = region.view_distance
    new_quat = target_rot_euler.to_quaternion()
    region.view_rotation = new_quat
    
    view_z = Vector((0.0, 0.0, 1.0))
    offset = (new_quat @ view_z) * dist
    region.view_location = target_pos - offset


_ORBIT_FOCUS_TYPES = {
    'MESH',
    'CURVE',
    'CURVES',
    'SURFACE',
    'FONT',
    'META',
    'VOLUME',
    'POINTCLOUD',
    'GREASEPENCIL',
    'GPENCIL',
}


def _is_orbit_focus_object(obj):
    """Return True when object should contribute to orbit-selection focus."""
    if not obj:
        return False
    if obj.type in _ORBIT_FOCUS_TYPES:
        return True

    # Include instancers (for example Empty instancing a Collection).
    instance_type = getattr(obj, "instance_type", 'NONE')
    if instance_type and instance_type != 'NONE':
        if instance_type == 'COLLECTION':
            return bool(getattr(obj, "instance_collection", None))
        return True
    if getattr(obj, "is_instancer", False):
        return True
    return False


def get_orbit_focus_selection(context):
    """Get selected objects that should affect orbit-selection mode."""
    selected = getattr(context, "selected_objects", None) or []
    return [obj for obj in selected if _is_orbit_focus_object(obj)]


def get_orbit_focus_view_layer_objects(context):
    """Get visible geometry/instancer objects from the current view layer."""
    view_layer = getattr(context, "view_layer", None) or getattr(bpy.context, "view_layer", None)
    if not view_layer:
        return []

    objects = []
    for obj in view_layer.objects:
        if not _is_orbit_focus_object(obj):
            continue
        try:
            if not obj.visible_get(view_layer=view_layer):
                continue
        except Exception:
            if getattr(obj, "hide_viewport", False):
                continue
        objects.append(obj)
    return objects


def get_selection_center(context):
    """Get bounding box center of selected geometry/instancer objects."""
    selected = get_orbit_focus_selection(context)
    if not selected:
        return None
    
    # Calculate combined world-space bounding box
    min_co = Vector((float('inf'), float('inf'), float('inf')))
    max_co = Vector((float('-inf'), float('-inf'), float('-inf')))
    
    for obj in selected:
        for corner in obj.bound_box:
            world_co = obj.matrix_world @ Vector(corner)
            min_co.x = min(min_co.x, world_co.x)
            min_co.y = min(min_co.y, world_co.y)
            min_co.z = min(min_co.z, world_co.z)
            max_co.x = max(max_co.x, world_co.x)
            max_co.y = max(max_co.y, world_co.y)
            max_co.z = max(max_co.z, world_co.z)
    
    return (min_co + max_co) / 2


# ============================================================================
# CLEANUP / GARBAGE COLLECTION
# ============================================================================

def cleanup_world_fake_users():
    """Remove fake_user from Worlds not referenced by any saved view."""
    try:
        # Build set of World names used by saved views
        used_worlds = set()
        for scene in bpy.data.scenes:
            if hasattr(scene, 'saved_views'):
                for view in scene.saved_views:
                    if hasattr(view, 'shading_selected_world') and view.shading_selected_world:
                        used_worlds.add(view.shading_selected_world)
        
        # Clear fake_user from Worlds not referenced by any saved view
        # Don't check world.users - we only care about ViewPilot's usage
        for world in bpy.data.worlds:
            if world.name not in used_worlds and world.use_fake_user:
                world.use_fake_user = False
    except Exception as e:
        print(f"[ViewPilot] Error cleaning up World fake users: {e}")


# ============================================================================
# CAMERA CREATION UTILITY
# ============================================================================

# Blender's internal viewport sensor reference (2× standard 36mm camera default)
# See: https://projects.blender.org/blender/blender/issues/114507
VIEWPORT_SENSOR = 72.0  # mm

def create_camera_from_view_data(
    context,
    name: str,
    location,          # Vector - eye position
    rotation,          # Quaternion - camera rotation
    is_perspective: bool,
    lens: float,       # Focal length
    distance: float,   # View distance (for ortho_scale calculation)
    clip_start: float,
    clip_end: float,
    passepartout: float = 0.95,
    show_passepartout: bool = True,
    show_name: bool = True,
    show_sensor: bool = True,
    use_collection: bool = True,
    collection_name: str = "ViewPilot",
    collection_color: str = 'COLOR_04',
    scene = None  # Optional: target scene for camera (defaults to context.scene)
):
    """
    Create a camera object from view data.
    
    Works with either live viewport data or saved view data.
    Returns the created camera object.
    """
    # Use provided scene or fall back to context.scene
    target_scene = scene if scene else context.scene
    
    # Create camera data
    cam_data = bpy.data.cameras.new(name)
    cam_data.passepartout_alpha = passepartout
    cam_data.show_passepartout = show_passepartout
    cam_data.clip_start = clip_start
    cam_data.clip_end = clip_end
    
    # Get viewport dimensions for sensor calculation
    viewport_width = context.region.width if context.region else 1920
    viewport_height = context.region.height if context.region else 1080
    viewport_aspect = viewport_width / viewport_height
    
    if is_perspective:
        cam_data.type = 'PERSP'
        cam_data.lens = lens
    else:
        cam_data.type = 'ORTHO'
        # Viewport ortho view_distance is scaled relative to the viewport's lens setting
        # The relationship is: camera_ortho_scale = view_distance * (72.0 / lens)
        cam_data.ortho_scale = distance * (VIEWPORT_SENSOR / lens)
    
    # Set sensor fit and dimensions to match viewport aspect ratio
    if viewport_aspect >= 1.0:
        # Landscape: fit to width (horizontal)
        cam_data.sensor_fit = 'HORIZONTAL'
        cam_data.sensor_width = VIEWPORT_SENSOR
        cam_data.sensor_height = VIEWPORT_SENSOR / viewport_aspect
    else:
        # Portrait: fit to height (vertical)
        cam_data.sensor_fit = 'VERTICAL'
        cam_data.sensor_height = VIEWPORT_SENSOR
        cam_data.sensor_width = VIEWPORT_SENSOR * viewport_aspect
    
    # Create camera object
    cam_obj = bpy.data.objects.new(name, cam_data)
    
    # Display options
    cam_obj.show_name = show_name
    cam_data.show_name = show_name
    cam_data.show_sensor = show_sensor
    
    # Position camera
    cam_obj.location = Vector(location)
    cam_obj.rotation_euler = rotation.to_euler() if hasattr(rotation, 'to_euler') else rotation
    
    # Link to collection
    if use_collection:
        # Find existing viewport cameras collection within this scene only
        cam_collection = None
        
        def find_collection_recursive(parent_collection):
            """Search for ViewPilot camera collection within a collection tree."""
            for child in parent_collection.children:
                if child.get("is_viewport_cameras_collection"):
                    return child
                found = find_collection_recursive(child)
                if found:
                    return found
            return None
        
        cam_collection = find_collection_recursive(target_scene.collection)
        
        # Create if not found in this scene
        if cam_collection is None:
            # Include scene name for unique collection names across scenes
            scene_collection_name = f"{collection_name} [{target_scene.name}]"
            cam_collection = bpy.data.collections.new(scene_collection_name)
            cam_collection["is_viewport_cameras_collection"] = True
            cam_collection["viewpilot_base_name"] = collection_name  # For scene rename sync
            cam_collection.color_tag = collection_color
            target_scene.collection.children.link(cam_collection)
        
        cam_collection.objects.link(cam_obj)
    else:
        target_scene.collection.objects.link(cam_obj)
    
    return cam_obj


def sync_viewpilot_collection_names():
    """
    Sync ViewPilot camera collection names with their parent scene names.
    
    Called from depsgraph_update_post handler to handle scene renames.
    """
    for scene in bpy.data.scenes:
        # Search for ViewPilot camera collection in this scene
        def find_collection_recursive(parent_collection):
            for child in parent_collection.children:
                if child.get("is_viewport_cameras_collection"):
                    return child
                found = find_collection_recursive(child)
                if found:
                    return found
            return None
        
        cam_coll = find_collection_recursive(scene.collection)
        if cam_coll:
            base_name = cam_coll.get("viewpilot_base_name", "ViewPilot")
            expected_name = f"{base_name} [{scene.name}]"
            if cam_coll.name != expected_name:
                cam_coll.name = expected_name


@persistent
def viewpilot_depsgraph_handler(scene, depsgraph):
    """Check for scene renames and sync collection names."""
    # Check if any scene was updated (could be a rename)
    for update in depsgraph.updates:
        if isinstance(update.id, bpy.types.Scene):
            sync_viewpilot_collection_names()
            break  # Only need to sync once per update batch


# ============================================================================
# VIEW STATE CAPTURE & RESTORE
# ============================================================================

def get_current_view_state(context):
    """Capture the complete state of the current 3D View."""
    try:
        area, space, r3d = find_view3d_context(context)
        if not space or not r3d:
            return None

        return {
            'view_location': r3d.view_location.copy(),
            'view_rotation': r3d.view_rotation.copy(), # Quaternion
            'view_distance': r3d.view_distance,
            'view_perspective': r3d.view_perspective,  # 'PERSP', 'ORTHO', or 'CAMERA'
            'is_perspective': r3d.is_perspective,
            'lens': space.lens,
            'clip_start': space.clip_start,
            'clip_end': space.clip_end,
            'timestamp': time.time()
        }
    except Exception as e:
        print(f"Error getting view state: {e}")
    return None


def restore_view_state(context, state):
    """Apply a saved state to the current 3D View."""
    try:
        if not state: return False

        # Works in any context including timers.
        target_area, space, region = find_view3d_context(context)
        
        if not space or not region:
            print("[View History] Could not find 3D View to restore state")
            return False

        # First, exit camera view if we're in it (set view_perspective directly)
        if region.view_perspective == 'CAMERA':
            region.view_perspective = 'PERSP' if state['is_perspective'] else 'ORTHO'

        # Apply properties
        region.view_location = state['view_location'].copy()
        region.view_rotation = state['view_rotation'].copy()
        region.view_distance = state['view_distance']
        
        # Restore perspective mode if it has changed (use direct property, not operator)
        stored_perspective = state.get('view_perspective', 'PERSP' if state['is_perspective'] else 'ORTHO')
        if stored_perspective != 'CAMERA':
            if state['is_perspective'] and not region.is_perspective:
                region.view_perspective = 'PERSP'
            elif not state['is_perspective'] and region.is_perspective:
                region.view_perspective = 'ORTHO'
            
        space.lens = state['lens']
        space.clip_start = state['clip_start']
        space.clip_end = state['clip_end']
        
        # Force viewport redraw (important for timer context)
        if target_area:
            target_area.tag_redraw()
        
        return True
        
    except Exception as e:
        print(f"Error restoring view state: {e}")
        return False


def states_are_similar(state1, state2, threshold=0.0001):
    """Check if two states are effectively identical."""
    if state1 is None or state2 is None: return False
    
    # Compare Location
    if (state1['view_location'] - state2['view_location']).length_squared > threshold: return False
    
    # Compare Rotation (Quaternion dot product)
    # q1.dot(q2) is close to 1 or -1 if they are similar
    rot_diff = abs(state1['view_rotation'].dot(state2['view_rotation']))
    if rot_diff < 0.9999: return False
    
    # Compare Distance
    if abs(state1['view_distance'] - state2['view_distance']) > threshold: return False
    
    # Compare Perspective mode
    if state1['is_perspective'] != state2['is_perspective']: return False
    
    # Compare Lens (focal length) - use relative threshold for lens values
    lens_threshold = 0.1  # 0.1mm difference is negligible
    if abs(state1['lens'] - state2['lens']) > lens_threshold: return False
    
    return True


# ============================================================================
# HISTORY MANAGEMENT
# ============================================================================

def add_to_history(state):
    """Add a state to history, handling branching."""
    global view_history, view_history_index
    
    if state is None: return
    
    # 1. If we are not at the end of history, we are creating a NEW branch.
    #    Discard all "future" states.
    if view_history_index != -1 and view_history_index < len(view_history) - 1:
        view_history = view_history[:view_history_index + 1]
    
    # 2. Check if this new state is different enough from the LAST state
    if view_history:
        last_state = view_history[-1]
        if states_are_similar(state, last_state):
            return # Don't save duplicates
            
    # 3. Add to history
    view_history.append(state)
    
    # 4. Cap size - use preference if available, fallback to default
    try:
        from .preferences import get_preferences
        max_size = get_preferences().history_max_size
    except Exception:
        max_size = 20
    if len(view_history) > max_size:
        view_history.pop(0)
        
    # 5. Reset index to "Live" (end of list)
    view_history_index = len(view_history) - 1


def _reset_orbit_sliders_after_history(context, state):
    """Reset orbit sliders to zero after history navigation.
    
    Since orbit values are view-relative, keeping old values after
    navigating to a different view state would cause jumps.
    """
    try:
        props = context.scene.viewpilot
        
        # Only reset if orbit mode is active
        if not props.orbit_around_selection or not props.orbit_initialized:
            return
        
        # Reset sliders to zero
        props['orbit_pitch'] = 0.0
        props['orbit_yaw'] = 0.0
        props['screen_rotation'] = 0.0
        props['orbit_active_axis'] = ""
        
        # Update orbit base to match restored view
        # The orbit center stays the same (selection hasn't changed)
        # but base offset and rotation need to match the restored state
        from mathutils import Vector
        
        center = Vector(props.orbit_center)
        
        # Calculate new eye position from restored state
        view_rot = state['view_rotation']
        view_z = Vector((0.0, 0.0, 1.0))
        dist = state['view_distance']
        eye_pos = state['view_location'] + (view_rot @ view_z) * dist
        
        # Update base offset (from center to camera)
        base_offset = eye_pos - center
        props['orbit_base_offset'] = (base_offset.x, base_offset.y, base_offset.z)
        props['orbit_base_rotation'] = (view_rot.w, view_rot.x, view_rot.y, view_rot.z)
        props['orbit_distance'] = base_offset.length
        
    except Exception as e:
        print(f"[ViewPilot] Failed to reset orbit after history: {e}")


def history_go_back(context):
    """Move history index back and restore state. Returns the new state or None."""
    global view_history_index
    from .state_controller import get_controller, UpdateSource, LockPriority
    
    if not view_history:
        return None
    
    controller = get_controller()
    
    # If index is -1 (Live), jump to the last saved state first
    if view_history_index == -1:
        view_history_index = len(view_history) - 1
        
    # Move back
    new_index = max(0, view_history_index - 1)
    
    if new_index == view_history_index:
            return None # Reached start
            
    view_history_index = new_index
    state = view_history[view_history_index]
    
    # Lock history recording for a short time to prevent the monitor from
    # detecting the restoration (especially perspective toggle) as new movement
    controller.start_grace_period(0.5, UpdateSource.HISTORY_NAV)
    
    # Restore
    restore_view_state(context, state)
    
    # Reset orbit sliders to zero (they are view-relative, so resetting prevents jumps)
    _reset_orbit_sliders_after_history(context, state)
    
    return state


def history_go_forward(context):
    """Move history index forward and restore state. Returns the new state or None."""
    global view_history_index
    from .state_controller import get_controller, UpdateSource, LockPriority
    
    if not view_history:
        return None
    
    controller = get_controller()
    
    if view_history_index == -1:
        return None # Already at live
        
    new_index = view_history_index + 1
    
    if new_index >= len(view_history):
        view_history_index = -1
        return None # Back to live (caller might want to handle this specific case message)
        
    view_history_index = new_index
    state = view_history[view_history_index]
    
    # Lock history recording for a short time to prevent the monitor from
    # detecting the restoration (especially perspective toggle) as new movement
    controller.start_grace_period(0.5, UpdateSource.HISTORY_NAV)
    
    restore_view_state(context, state)
    
    # Reset orbit sliders to zero (they are view-relative, so resetting prevents jumps)
    _reset_orbit_sliders_after_history(context, state)
    
    return state


# ============================================================================
# FILE LOAD HANDLER
# ============================================================================

@persistent
def reset_history_handler(dummy):
    """Clear history, initialize data storage, and restart monitor when loading a new file."""
    global view_history, view_history_index
    view_history.clear()
    view_history_index = -1
    print("[View History] Reset on Load")
    
    # Initialize data storage (creates Text datablock if needed)
    # This is deferred to load_post because bpy.data.texts isn't available during registration
    try:
        from . import data_storage
        data_storage.ensure_data_initialized()
        
        # Migrate from old per-scene storage if needed (one-time migration)
        migrated = data_storage.migrate_from_scene_storage()
        if migrated > 0:
            print(f"[ViewPilot] Migrated {migrated} views from scene storage to JSON")
        
        # Sync JSON to PropertyGroup for UIList/UI compatibility
        import bpy
        if hasattr(bpy.context, 'scene') and bpy.context.scene:
            # Initialize UUIDs for all scenes and view layers
            data_storage.initialize_all_uuids()
            
            synced = data_storage.sync_to_all_scenes()
            if synced > 0:
                print(f"[ViewPilot] Synced {synced} views to PropertyGroup")
    except Exception as e:
        print(f"[ViewPilot] Data storage init failed: {e}")
    
    # Restart the monitor because loading a file kills modal operators
    # We use a timer to let the load finish completely
    bpy.app.timers.register(start_monitor, first_interval=1.0)


def start_monitor():
    """Start the modal monitor after a short delay"""
    global monitor_running
    if monitor_running:
        return None  # Already running
    try:
        bpy.ops.view3d.view_history_monitor('INVOKE_DEFAULT')
    except Exception:
        pass
    return None
