"""
Data Storage Module for ViewPilot.

Provides JSON-based storage for saved views in a Text datablock.
This allows views to persist across all scenes and survive scene deletion.
"""

import bpy
import json
from typing import List, Dict, Any, Optional


# The name of the Text datablock used for storage
DATA_TEXT_NAME = ".ViewPilot_Data"

# Flag to prevent _sync_view_to_json callback during sync_to_all_scenes
# This prevents O(N*M) IO explosion (Views * Properties)
IS_SYNCING = False

# Current schema version for migrations
SCHEMA_VERSION = 1

# Custom property key for UUID tracking
UUID_PROP_KEY = "viewpilot_uuid"


# =============================================================================
# UUID HELPERS - For tracking scenes/view layers by persistent ID
# =============================================================================

import uuid


def is_scene_writable(scene: bpy.types.Scene) -> bool:
    """Check if a scene is writable (not linked without override)."""
    # Linked without override → read-only
    if scene.library is not None and scene.override_library is None:
        return False
    return True


def is_view_layer_writable(view_layer: bpy.types.ViewLayer, scene: bpy.types.Scene) -> bool:
    """Check if a view layer is writable (parent scene must be writable)."""
    # View layers inherit writability from their scene
    return is_scene_writable(scene)


def get_scene_identity(scene: bpy.types.Scene) -> str:
    """Get a unique identity string for a scene.
    
    For local scenes: returns the UUID (creates if needed)
    For linked scenes: returns 'lib::filepath::scene_name' format
    """
    if is_scene_writable(scene):
        return ensure_scene_uuid(scene)
    else:
        # Linked scene - use normalized library path + name as identity
        lib_path = bpy.path.abspath(scene.library.filepath) if scene.library else ""
        return f"lib::{lib_path}::{scene.name}"


def get_view_layer_identity(view_layer: bpy.types.ViewLayer, scene: bpy.types.Scene) -> str:
    """Get a unique identity string for a view layer.
    
    For local view layers: returns the UUID (creates if needed)
    For view layers in linked scenes: returns composite identity
    """
    if is_view_layer_writable(view_layer, scene):
        return ensure_view_layer_uuid(view_layer)
    else:
        # Linked - use normalized library path + scene name + view layer name
        lib_path = bpy.path.abspath(scene.library.filepath) if scene.library else ""
        return f"lib::{lib_path}::{scene.name}::{view_layer.name}"


def ensure_scene_uuid(scene: bpy.types.Scene) -> Optional[str]:
    """Ensure a scene has a UUID custom property and return it.
    
    Returns None for read-only (linked) scenes.
    """
    if not is_scene_writable(scene):
        return None  # Can't write to linked scene
    
    if UUID_PROP_KEY not in scene:
        scene[UUID_PROP_KEY] = str(uuid.uuid4())
    return scene[UUID_PROP_KEY]


def ensure_view_layer_uuid(view_layer: bpy.types.ViewLayer) -> Optional[str]:
    """Ensure a view layer has a UUID custom property and return it.
    
    Returns None for read-only (linked) view layers.
    """
    # Note: We can't check writability here without scene context
    # This function assumes the caller has already checked writability
    if UUID_PROP_KEY not in view_layer:
        view_layer[UUID_PROP_KEY] = str(uuid.uuid4())
    return view_layer[UUID_PROP_KEY]


def find_scene_by_identity(identity_str: str) -> Optional[bpy.types.Scene]:
    """Find a scene by its identity (UUID or lib::path::name format)."""
    if not identity_str:
        return None
    
    # Check if it's a linked scene identity
    if identity_str.startswith("lib::"):
        parts = identity_str.split("::", 2)  # lib, filepath, name
        if len(parts) == 3:
            _, lib_path, scene_name = parts
            for scene in bpy.data.scenes:
                if (scene.library is not None and 
                    scene.override_library is None and  # Only match pure linked (read-only)
                    bpy.path.abspath(scene.library.filepath) == lib_path and 
                    scene.name == scene_name):
                    return scene
        return None
    
    # Otherwise it's a UUID
    for scene in bpy.data.scenes:
        if scene.get(UUID_PROP_KEY) == identity_str:
            return scene
    return None


def find_view_layer_by_identity(identity_str: str, scene: bpy.types.Scene) -> Optional[bpy.types.ViewLayer]:
    """Find a view layer by its identity within a scene."""
    if not identity_str or not scene:
        return None
    
    # Check if it's a linked view layer identity
    if identity_str.startswith("lib::"):
        parts = identity_str.split("::")  # lib, filepath, scene_name, vl_name
        if len(parts) == 4:
            vl_name = parts[3]
            for view_layer in scene.view_layers:
                if view_layer.name == vl_name:
                    return view_layer
        return None
    
    # Otherwise it's a UUID
    for view_layer in scene.view_layers:
        if view_layer.get(UUID_PROP_KEY) == identity_str:
            return view_layer
    return None


# Keep old functions as aliases for backwards compatibility
def find_scene_by_uuid(uuid_str: str) -> Optional[bpy.types.Scene]:
    """Find a scene by its UUID (wrapper for find_scene_by_identity)."""
    return find_scene_by_identity(uuid_str)


def find_view_layer_by_uuid(uuid_str: str, scene: bpy.types.Scene) -> Optional[bpy.types.ViewLayer]:
    """Find a view layer by its UUID (wrapper for find_view_layer_by_identity)."""
    return find_view_layer_by_identity(uuid_str, scene)

from collections import defaultdict


def find_duplicate_scene_uuids() -> dict:
    """Find writable scenes that share the same UUID (from duplication)."""
    uuid_map = defaultdict(list)
    for scene in bpy.data.scenes:
        if not is_scene_writable(scene):
            continue  # Skip linked scenes
        uid = scene.get(UUID_PROP_KEY)
        if uid:
            uuid_map[uid].append(scene)
    return {u: scenes for u, scenes in uuid_map.items() if len(scenes) > 1}


def fix_duplicate_scene_uuids() -> int:
    """Regenerate UUIDs for duplicate scenes. Returns count of fixed scenes."""
    duplicates = find_duplicate_scene_uuids()
    fixed = 0
    for uuid_str, scenes in duplicates.items():
        # Keep the first, regenerate the rest
        for scene in scenes[1:]:
            if is_scene_writable(scene):  # Double-check writability
                scene[UUID_PROP_KEY] = str(uuid.uuid4())
                fixed += 1
    return fixed


def find_duplicate_view_layer_uuids(scene: bpy.types.Scene) -> dict:
    """Find view layers in a writable scene that share the same UUID."""
    if not is_scene_writable(scene):
        return {}  # Can't have duplicates in linked scenes (read-only)
    
    uuid_map = defaultdict(list)
    for view_layer in scene.view_layers:
        uid = view_layer.get(UUID_PROP_KEY)
        if uid:
            uuid_map[uid].append(view_layer)
    return {u: vls for u, vls in uuid_map.items() if len(vls) > 1}


def fix_duplicate_view_layer_uuids(scene: bpy.types.Scene) -> int:
    """Regenerate UUIDs for duplicate view layers. Returns count of fixed."""
    if not is_scene_writable(scene):
        return 0  # Can't fix linked scenes
    
    duplicates = find_duplicate_view_layer_uuids(scene)
    fixed = 0
    for uuid_str, view_layers in duplicates.items():
        for vl in view_layers[1:]:
            vl[UUID_PROP_KEY] = str(uuid.uuid4())
            fixed += 1
    return fixed


def initialize_all_uuids() -> None:
    """Initialize UUIDs for all writable scenes and view layers.
    
    Skips linked (read-only) scenes.
    Also detects and regenerates duplicate UUIDs that can occur when
    scenes or view layers are duplicated.
    """
    # Fix any duplicate scene UUIDs (only processes writable scenes)
    fix_duplicate_scene_uuids()
    
    # Ensure all writable scenes have UUIDs
    for scene in bpy.data.scenes:
        if not is_scene_writable(scene):
            continue  # Skip linked scenes
        
        ensure_scene_uuid(scene)
        
        # Fix any duplicate view layer UUIDs within this scene
        fix_duplicate_view_layer_uuids(scene)
        
        # Ensure all view layers have UUIDs
        for view_layer in scene.view_layers:
            ensure_view_layer_uuid(view_layer)


def get_data_text() -> bpy.types.Text:
    """Get or create the ViewPilot data Text datablock."""
    text = bpy.data.texts.get(DATA_TEXT_NAME)
    if text is None:
        text = bpy.data.texts.new(DATA_TEXT_NAME)
        text.use_fake_user = True  # Prevent accidental deletion
        # Initialize with empty structure
        _save_raw_data(text, _get_empty_data())
    return text


def _get_empty_data() -> Dict[str, Any]:
    """Return the empty data structure."""
    return {
        "version": SCHEMA_VERSION,
        "saved_views": [],
        "style_presets": [],
        "next_view_number": 1,
    }


def _load_raw_data(text: bpy.types.Text) -> Dict[str, Any]:
    """Load and parse JSON from Text datablock."""
    try:
        content = text.as_string()
        if not content.strip():
            return _get_empty_data()
        return json.loads(content)
    except (json.JSONDecodeError, Exception):
        return _get_empty_data()


def _save_raw_data(text: bpy.types.Text, data: Dict[str, Any]) -> None:
    """Save data as JSON to Text datablock."""
    text.clear()
    text.write(json.dumps(data, indent=2))


def load_data() -> Dict[str, Any]:
    """Load all ViewPilot data from storage."""
    text = get_data_text()
    return _load_raw_data(text)


def save_data(data: Dict[str, Any]) -> None:
    """Save all ViewPilot data to storage."""
    text = get_data_text()
    _save_raw_data(text, data)


# =============================================================================
# SAVED VIEWS API
# =============================================================================

def get_saved_views() -> List[Dict[str, Any]]:
    """Get the list of saved views."""
    data = load_data()
    return data.get("saved_views", [])


def get_saved_view(index: int) -> Optional[Dict[str, Any]]:
    """Get a single saved view by index."""
    views = get_saved_views()
    if 0 <= index < len(views):
        return views[index]
    return None


def add_saved_view(view_dict: Dict[str, Any], auto_sync: bool = True) -> int:
    """Add a new saved view. Returns the index of the new view.
    
    Args:
        view_dict: The view data to add
        auto_sync: If True, sync to PropertyGroup after saving
    """
    data = load_data()
    data["saved_views"].append(view_dict)
    save_data(data)
    if auto_sync:
        sync_to_all_scenes()
    return len(data["saved_views"]) - 1


def update_saved_view(index: int, view_dict: Dict[str, Any], auto_sync: bool = True) -> bool:
    """Update an existing saved view. Returns True if successful.
    
    Args:
        index: Index of the view to update
        view_dict: The new view data
        auto_sync: If True, sync to PropertyGroup after saving
    """
    data = load_data()
    if 0 <= index < len(data["saved_views"]):
        data["saved_views"][index] = view_dict
        save_data(data)
        if auto_sync:
            sync_to_all_scenes()
        return True
    return False


def delete_saved_view(index: int, auto_sync: bool = True) -> bool:
    """Delete a saved view by index. Returns True if successful.
    
    Args:
        index: Index of the view to delete
        auto_sync: If True, sync to PropertyGroup after saving
    """
    data = load_data()
    if 0 <= index < len(data["saved_views"]):
        del data["saved_views"][index]
        save_data(data)
        if auto_sync:
            sync_to_all_scenes()
        return True
    return False


def reorder_saved_views(new_order: List[int], auto_sync: bool = True) -> bool:
    """Reorder saved views based on new index order. Returns True if successful.
    
    Args:
        new_order: List of indices representing new order
        auto_sync: If True, sync to PropertyGroup after saving
    """
    data = load_data()
    views = data["saved_views"]
    if len(new_order) != len(views):
        return False
    try:
        data["saved_views"] = [views[i] for i in new_order]
        save_data(data)
        if auto_sync:
            sync_to_all_scenes()
        return True
    except IndexError:
        return False


def get_next_view_number() -> int:
    """Get and increment the next view number for naming."""
    data = load_data()
    num = data.get("next_view_number", 1)
    data["next_view_number"] = num + 1
    save_data(data)
    return num


# =============================================================================
# CONVERSION UTILITIES
# =============================================================================

def capture_viewport_as_dict(space, region, context, name: str = "Saved View") -> Dict[str, Any]:
    """Capture the current viewport state as a dictionary.
    
    Args:
        space: The SpaceView3D
        region: The RegionView3D 
        context: Blender context
        name: Name for the saved view
        
    Returns:
        Dictionary containing all view data
    """
    shading = space.shading
    overlay = space.overlay
    
    # Store quaternion as (w, x, y, z)
    q = region.view_rotation
    
    view_dict = {
        # Identity
        "name": name,
        
        # Transform
        "location": list(region.view_location),
        "rotation": [q.w, q.x, q.y, q.z],
        "distance": region.view_distance,
        
        # Lens
        "lens": space.lens,
        "is_perspective": region.is_perspective,
        "clip_start": space.clip_start,
        "clip_end": space.clip_end,
        
        # Shading
        "shading_type": shading.type,
        "shading_light": shading.light,
        "shading_color_type": shading.color_type,
        "shading_single_color": list(shading.single_color),
        "shading_background_type": shading.background_type,
        "shading_background_color": list(shading.background_color),
        # Only read studio_light when not in WIREFRAME mode (WIREFRAME has no valid studio_light)
        "shading_studio_light": shading.studio_light if shading.type != 'WIREFRAME' else "",
        "shading_studiolight_rotate_z": shading.studiolight_rotate_z,
        "shading_studiolight_intensity": shading.studiolight_intensity,
        "shading_studiolight_background_alpha": shading.studiolight_background_alpha,
        "shading_studiolight_background_blur": shading.studiolight_background_blur,
        "shading_use_world_space_lighting": shading.use_world_space_lighting,
        "shading_selected_world": context.scene.world.name if context.scene.world else "",
        "shading_show_cavity": shading.show_cavity,
        "shading_cavity_type": shading.cavity_type,
        "shading_cavity_ridge_factor": shading.cavity_ridge_factor,
        "shading_cavity_valley_factor": shading.cavity_valley_factor,
        "shading_curvature_ridge_factor": shading.curvature_ridge_factor,
        "shading_curvature_valley_factor": shading.curvature_valley_factor,
        "shading_show_object_outline": shading.show_object_outline,
        "shading_object_outline_color": list(shading.object_outline_color),
        "shading_show_xray": shading.show_xray,
        "shading_xray_alpha": shading.xray_alpha,
        "shading_show_shadows": shading.show_shadows,
        "shading_shadow_intensity": shading.shadow_intensity,
        "shading_use_scene_lights": shading.use_scene_lights,
        "shading_use_scene_world": shading.use_scene_world,
        
        # Overlays
        "overlays_show_overlays": overlay.show_overlays,
        "overlays_show_floor": overlay.show_floor,
        "overlays_show_axis_x": overlay.show_axis_x,
        "overlays_show_axis_y": overlay.show_axis_y,
        "overlays_show_axis_z": overlay.show_axis_z,
        "overlays_show_text": overlay.show_text,
        "overlays_show_cursor": overlay.show_cursor,
        "overlays_show_outline_selected": overlay.show_outline_selected,
        "overlays_show_wireframes": overlay.show_wireframes,
        "overlays_wireframe_threshold": overlay.wireframe_threshold,
        "overlays_wireframe_opacity": overlay.wireframe_opacity,
        "overlays_show_face_orientation": overlay.show_face_orientation,
        "overlays_show_relationship_lines": overlay.show_relationship_lines,
        "overlays_show_bones": overlay.show_bones,
        "overlays_show_motion_paths": overlay.show_motion_paths,
        "overlays_show_object_origins": overlay.show_object_origins,
        "overlays_show_annotation": overlay.show_annotation,
        "overlays_show_extras": overlay.show_extras,
        
        # Composition (store both name and identity for compatibility/fallback)
        # Identity can be UUID for local scenes or lib::path::name for linked
        "composition_scene": context.scene.name,
        "composition_scene_uuid": get_scene_identity(context.scene),
        "composition_view_layer": context.view_layer.name if hasattr(context, 'view_layer') and context.view_layer else "",
        "composition_view_layer_uuid": get_view_layer_identity(context.view_layer, context.scene) if hasattr(context, 'view_layer') and context.view_layer else "",
        
        # Remember toggles (defaults)
        "remember_perspective": True,
        "remember_shading": True,
        "remember_overlays": True,
        "remember_composition": True,
    }
    
    # Protect World from being purged if referenced
    if context.scene.world:
        context.scene.world.use_fake_user = True
    
    return view_dict


def view_to_dict(view: 'bpy.types.PropertyGroup') -> Dict[str, Any]:
    """Convert a SavedViewItem PropertyGroup to a dictionary."""
    return {
        # Identity
        "name": view.name,
        
        # Transform
        "location": list(view.location),
        "rotation": list(view.rotation),
        "distance": view.distance,
        
        # Lens
        "lens": view.lens,
        "is_perspective": view.is_perspective,
        "clip_start": view.clip_start,
        "clip_end": view.clip_end,
        
        # Shading
        "shading_type": view.shading_type,
        "shading_light": view.shading_light,
        "shading_color_type": view.shading_color_type,
        "shading_single_color": list(view.shading_single_color),
        "shading_background_type": view.shading_background_type,
        "shading_background_color": list(view.shading_background_color),
        "shading_studio_light": view.shading_studio_light,
        "shading_studiolight_rotate_z": view.shading_studiolight_rotate_z,
        "shading_studiolight_intensity": view.shading_studiolight_intensity,
        "shading_studiolight_background_alpha": view.shading_studiolight_background_alpha,
        "shading_studiolight_background_blur": view.shading_studiolight_background_blur,
        "shading_use_world_space_lighting": view.shading_use_world_space_lighting,
        "shading_selected_world": view.shading_selected_world,
        "shading_show_cavity": view.shading_show_cavity,
        "shading_cavity_type": view.shading_cavity_type,
        "shading_cavity_ridge_factor": view.shading_cavity_ridge_factor,
        "shading_cavity_valley_factor": view.shading_cavity_valley_factor,
        "shading_curvature_ridge_factor": view.shading_curvature_ridge_factor,
        "shading_curvature_valley_factor": view.shading_curvature_valley_factor,
        "shading_show_object_outline": view.shading_show_object_outline,
        "shading_object_outline_color": list(view.shading_object_outline_color),
        "shading_show_xray": view.shading_show_xray,
        "shading_xray_alpha": view.shading_xray_alpha,
        "shading_show_shadows": view.shading_show_shadows,
        "shading_shadow_intensity": view.shading_shadow_intensity,
        "shading_use_scene_lights": view.shading_use_scene_lights,
        "shading_use_scene_world": view.shading_use_scene_world,
        
        # Overlays
        "overlays_show_overlays": view.overlays_show_overlays,
        "overlays_show_floor": view.overlays_show_floor,
        "overlays_show_axis_x": view.overlays_show_axis_x,
        "overlays_show_axis_y": view.overlays_show_axis_y,
        "overlays_show_axis_z": view.overlays_show_axis_z,
        "overlays_show_text": view.overlays_show_text,
        "overlays_show_cursor": view.overlays_show_cursor,
        "overlays_show_outline_selected": view.overlays_show_outline_selected,
        "overlays_show_wireframes": view.overlays_show_wireframes,
        "overlays_wireframe_threshold": view.overlays_wireframe_threshold,
        "overlays_wireframe_opacity": view.overlays_wireframe_opacity,
        "overlays_show_face_orientation": view.overlays_show_face_orientation,
        "overlays_show_relationship_lines": view.overlays_show_relationship_lines,
        "overlays_show_bones": view.overlays_show_bones,
        "overlays_show_motion_paths": view.overlays_show_motion_paths,
        "overlays_show_object_origins": view.overlays_show_object_origins,
        "overlays_show_annotation": view.overlays_show_annotation,
        "overlays_show_extras": view.overlays_show_extras,
        
        # Composition
        "composition_scene": view.composition_scene,
        "composition_view_layer": view.composition_view_layer,
        
        # Remember toggles
        "remember_perspective": view.remember_perspective,
        "remember_shading": view.remember_shading,
        "remember_overlays": view.remember_overlays,
        "remember_composition": view.remember_composition,
    }


def dict_to_view(view_dict: Dict[str, Any], view: 'bpy.types.PropertyGroup') -> None:
    """Apply a dictionary to a SavedViewItem PropertyGroup."""
    for key, value in view_dict.items():
        if hasattr(view, key):
            if isinstance(value, list):
                setattr(view, key, tuple(value))
            else:
                setattr(view, key, value)


def apply_view_to_viewport(view_dict: Dict[str, Any], space, region, context) -> None:
    """Apply a view dict to the 3D viewport.
    
    Args:
        view_dict: The saved view data dictionary
        space: The SpaceView3D
        region: The RegionView3D
        context: Blender context
    """
    from mathutils import Vector, Quaternion
    
    # Helper to get value with default
    def get(key, default=None):
        return view_dict.get(key, default)
    
    # =========================================================================
    # Apply Perspective (if remember_perspective is True)
    # =========================================================================
    if get("remember_perspective", True):
        rotation = get("rotation", [1.0, 0.0, 0.0, 0.0])
        rot_quat = Quaternion((rotation[0], rotation[1], rotation[2], rotation[3]))
        
        location = get("location", [0.0, 0.0, 0.0])
        region.view_location = Vector(location)
        region.view_rotation = rot_quat
        region.view_distance = get("distance", 10.0)
        
        # Set perspective/ortho mode
        if get("is_perspective", True):
            region.view_perspective = 'PERSP'
        else:
            region.view_perspective = 'ORTHO'
        
        space.lens = get("lens", 50.0)
        space.clip_start = get("clip_start", 0.1)
        space.clip_end = get("clip_end", 1000.0)
    
    # =========================================================================
    # Apply Shading (if remember_shading is True)
    # =========================================================================
    if get("remember_shading", False):
        shading = space.shading
        shading.type = get("shading_type", "SOLID")
        shading.light = get("shading_light", "STUDIO")
        shading.color_type = get("shading_color_type", "MATERIAL")
        shading.single_color = tuple(get("shading_single_color", [0.8, 0.8, 0.8]))
        shading.background_type = get("shading_background_type", "THEME")
        shading.background_color = tuple(get("shading_background_color", [0.05, 0.05, 0.05]))
        
        studio_light = get("shading_studio_light", "")
        if studio_light:
            shading.studio_light = studio_light
        
        shading.studiolight_rotate_z = get("shading_studiolight_rotate_z", 0.0)
        shading.studiolight_intensity = get("shading_studiolight_intensity", 1.0)
        shading.studiolight_background_alpha = get("shading_studiolight_background_alpha", 0.0)
        shading.studiolight_background_blur = get("shading_studiolight_background_blur", 0.5)
        shading.use_world_space_lighting = get("shading_use_world_space_lighting", False)
        shading.show_cavity = get("shading_show_cavity", False)
        shading.cavity_type = get("shading_cavity_type", "WORLD")
        shading.cavity_ridge_factor = get("shading_cavity_ridge_factor", 1.0)
        shading.cavity_valley_factor = get("shading_cavity_valley_factor", 1.0)
        shading.curvature_ridge_factor = get("shading_curvature_ridge_factor", 1.0)
        shading.curvature_valley_factor = get("shading_curvature_valley_factor", 1.0)
        shading.show_object_outline = get("shading_show_object_outline", False)
        shading.object_outline_color = tuple(get("shading_object_outline_color", [0.0, 0.0, 0.0]))
        shading.show_xray = get("shading_show_xray", False)
        shading.xray_alpha = get("shading_xray_alpha", 0.5)
        shading.show_shadows = get("shading_show_shadows", False)
        shading.shadow_intensity = get("shading_shadow_intensity", 0.5)
        shading.use_scene_lights = get("shading_use_scene_lights", False)
        shading.use_scene_world = get("shading_use_scene_world", False)
        
        # Apply saved World datablock if stored and exists
        selected_world = get("shading_selected_world", "")
        if selected_world and selected_world in bpy.data.worlds:
            context.scene.world = bpy.data.worlds[selected_world]
    
    # =========================================================================
    # Apply Overlays (if remember_overlays is True)
    # =========================================================================
    if get("remember_overlays", False):
        overlay = space.overlay
        overlay.show_overlays = get("overlays_show_overlays", True)
        overlay.show_floor = get("overlays_show_floor", True)
        overlay.show_axis_x = get("overlays_show_axis_x", True)
        overlay.show_axis_y = get("overlays_show_axis_y", True)
        overlay.show_axis_z = get("overlays_show_axis_z", False)
        overlay.show_text = get("overlays_show_text", True)
        overlay.show_cursor = get("overlays_show_cursor", True)
        overlay.show_outline_selected = get("overlays_show_outline_selected", True)
        overlay.show_wireframes = get("overlays_show_wireframes", False)
        overlay.wireframe_threshold = get("overlays_wireframe_threshold", 1.0)
        overlay.wireframe_opacity = get("overlays_wireframe_opacity", 1.0)
        overlay.show_face_orientation = get("overlays_show_face_orientation", False)
        overlay.show_relationship_lines = get("overlays_show_relationship_lines", True)
        overlay.show_bones = get("overlays_show_bones", True)
        overlay.show_motion_paths = get("overlays_show_motion_paths", True)
        overlay.show_object_origins = get("overlays_show_object_origins", True)
        overlay.show_annotation = get("overlays_show_annotation", True)
        overlay.show_extras = get("overlays_show_extras", True)
    
    # =========================================================================
    # Apply Composition (if remember_composition is True)
    # =========================================================================
    if get("remember_composition", False):
        # Switch scene if different and valid
        # Try UUID first, fall back to name
        target_scene = None
        scene_uuid = get("composition_scene_uuid", "")
        composition_scene = get("composition_scene", "")
        
        if scene_uuid:
            target_scene = find_scene_by_uuid(scene_uuid)
        if not target_scene and composition_scene and composition_scene in bpy.data.scenes:
            target_scene = bpy.data.scenes[composition_scene]
        
        if target_scene and context.window.scene != target_scene:
            context.window.scene = target_scene
        
        # Switch view layer if different and valid
        # Try UUID first, fall back to name
        target_vl = None
        vl_uuid = get("composition_view_layer_uuid", "")
        composition_view_layer = get("composition_view_layer", "")
        current_scene = context.window.scene
        
        if vl_uuid:
            target_vl = find_view_layer_by_uuid(vl_uuid, current_scene)
        if not target_vl and composition_view_layer:
            if composition_view_layer in [vl.name for vl in current_scene.view_layers]:
                target_vl = current_scene.view_layers[composition_view_layer]
        
        if target_vl and context.window.view_layer != target_vl:
            context.window.view_layer = target_vl


# =============================================================================
# MIGRATION FROM OLD PROPERTYGROUP STORAGE
# =============================================================================

def migrate_from_scene_storage() -> int:
    """
    Migrate saved views from old per-Scene PropertyGroup storage to JSON.
    Returns the number of views migrated.
    Only migrates if JSON storage is empty (one-time migration).
    """
    import re
    
    # Skip migration if JSON storage already has views
    existing_views = get_saved_views()
    if len(existing_views) > 0:
        return 0  # Already have views in JSON, skip migration
    
    migrated_count = 0
    
    # Check all scenes for saved views
    for scene in bpy.data.scenes:
        if not hasattr(scene, 'saved_views'):
            continue
            
        old_views = scene.saved_views
        if len(old_views) == 0:
            continue
        
        # Convert each view to dict and add to JSON storage
        for view in old_views:
            view_dict = view_to_dict(view)
            add_saved_view(view_dict, auto_sync=False)  # Skip sync during migration
            migrated_count += 1
        
        # Clear old storage after migration
        old_views.clear()
    
    # Update next_view_number to avoid name collisions
    # Parse existing view names to find the highest "View N" number
    if migrated_count > 0:
        max_num = 0
        for view in get_saved_views():
            match = re.search(r"View (\d+)", view.get("name", ""))
            if match:
                max_num = max(max_num, int(match.group(1)))
        
        if max_num > 0:
            data = load_data()
            data["next_view_number"] = max_num + 1
            save_data(data)
        
        # Sync once at the end
        sync_to_all_scenes()
    
    return migrated_count


def ensure_data_initialized() -> None:
    """Ensure the data storage is initialized. Called on addon register."""
    get_data_text()  # Creates if not exists


def sync_to_scene_storage(scene) -> int:
    """
    Sync saved views from JSON storage to Scene PropertyGroup.
    This allows existing UI code to continue working while we migrate.
    Returns the number of views synced.
    """
    if not hasattr(scene, 'saved_views'):
        return 0
    
    # Clear existing PropertyGroup data
    scene.saved_views.clear()
    
    # Copy from JSON to PropertyGroup
    views = get_saved_views()
    for view_dict in views:
        new_view = scene.saved_views.add()
        dict_to_view(view_dict, new_view)
    
    return len(views)


def sync_to_all_scenes() -> int:
    """
    Sync saved views from JSON storage to ALL Scenes' PropertyGroups.
    This ensures views are visible regardless of which scene is active.
    Returns the number of views synced.
    """
    global IS_SYNCING
    IS_SYNCING = True
    
    try:
        views = get_saved_views()
        view_count = len(views)
        
        for scene in bpy.data.scenes:
            if not hasattr(scene, 'saved_views'):
                continue
            
            # Clear and repopulate
            scene.saved_views.clear()
            for view_dict in views:
                new_view = scene.saved_views.add()
                dict_to_view(view_dict, new_view)
        
        return view_count
    finally:
        IS_SYNCING = False

