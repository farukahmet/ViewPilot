"""Preview Collection Manager for ViewPilot panel icon gallery.

Preview collections only load file-backed images, so the packed thumbnail images
must be exported to temporary PNG files before loading into the preview cache.
"""

import os

import bpy
import bpy.utils.previews

from . import debug_tools
from .temp_paths import make_temp_png_path, sanitize_token

# Global storage for preview collections and per-view active preview ids.
preview_collections = {}
_active_preview_ids = {}
_preview_serial = 0


def _next_preview_id(view_name):
    """Generate a unique preview id for incremental replacement."""
    global _preview_serial
    _preview_serial += 1
    return f"vp_{sanitize_token(view_name)}_{_preview_serial}"


def _write_preview_temp_file(image, view_name):
    """Export a packed blender image to temp file for preview loading."""
    temp_path = make_temp_png_path("vp_preview_", view_name)
    try:
        image.save_render(temp_path)
        return temp_path
    except Exception as e:
        debug_tools.log(f"preview temp export failed for '{view_name}': {e}")
        return None


def get_preview_collection():
    """Get the ViewPilot preview collection, creating if needed."""
    if "viewpilot_previews" not in preview_collections:
        preview_collections["viewpilot_previews"] = bpy.utils.previews.new()
    return preview_collections["viewpilot_previews"]


def load_view_preview(view_name, thumbnail_path, replace_existing=True):
    """Load or replace a thumbnail image in the preview collection."""
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        return 0

    pcoll = get_preview_collection()

    if replace_existing:
        old_id = _active_preview_ids.get(view_name)
        if old_id and old_id in pcoll:
            try:
                del pcoll[old_id]
            except Exception:
                # Some Blender versions don't reliably support entry deletion.
                pass

    # Use a fresh id on replacement, because individual entry removal from
    # preview collections is not reliable across Blender versions.
    if replace_existing or view_name not in _active_preview_ids:
        preview_id = _next_preview_id(view_name)
    else:
        preview_id = _active_preview_ids[view_name]

    try:
        pcoll.load(preview_id, thumbnail_path, 'IMAGE')
        _active_preview_ids[view_name] = preview_id
        return pcoll[preview_id].icon_id
    except Exception as e:
        print(f"[ViewPilot] Could not load preview for {view_name}: {e}")
        return 0


def get_preview_icon_id(view_name):
    """Get the icon id for a view preview, or 0 if unavailable."""
    pcoll = get_preview_collection()
    preview_id = _active_preview_ids.get(view_name)
    if preview_id and preview_id in pcoll:
        return pcoll[preview_id].icon_id
    return 0


def remove_view_preview(view_name):
    """Forget active preview mapping for a deleted/renamed view."""
    if view_name in _active_preview_ids:
        del _active_preview_ids[view_name]
    invalidate_panel_gallery_cache()


def refresh_view_preview(view_name):
    """Refresh only one view preview after thumbnail regeneration."""
    debug_tools.inc("preview.refresh_one")

    image_name = f".VP_Thumb_{view_name}"
    img = bpy.data.images.get(image_name)
    if not img:
        debug_tools.inc("preview.refresh_one.no_image")
        return 0

    with debug_tools.timed("preview.refresh_one.total"):
        temp_path = _write_preview_temp_file(img, view_name)
        if not temp_path:
            debug_tools.inc("preview.refresh_one.write_failed")
            return 0

        icon_id = load_view_preview(view_name, temp_path, replace_existing=True)
        if icon_id:
            debug_tools.inc("preview.refresh_one.success")
            invalidate_panel_gallery_cache()
        else:
            debug_tools.inc("preview.refresh_one.load_failed")
        return icon_id


def reload_all_previews(context):
    """Reload all view previews from packed blender images."""
    from . import data_storage
    global _preview_serial

    debug_tools.inc("preview.reload_all")

    with debug_tools.timed("preview.reload_all.total"):
        pcoll = get_preview_collection()
        pcoll.clear()
        _active_preview_ids.clear()
        _preview_serial = 0

        loaded = 0
        for view_dict in data_storage.get_saved_views():
            view_name = view_dict.get("name", "View")
            image_name = f".VP_Thumb_{view_name}"
            img = bpy.data.images.get(image_name)
            if not img:
                continue

            temp_path = _write_preview_temp_file(img, view_name)
            if not temp_path:
                continue

            if load_view_preview(view_name, temp_path, replace_existing=True):
                loaded += 1

        invalidate_panel_gallery_cache()
        debug_tools.log(f"previews reloaded: {loaded}")


def invalidate_panel_gallery_cache():
    """Clear cached enum items for panel icon gallery."""
    if hasattr(get_panel_gallery_items, '_cached'):
        get_panel_gallery_items._cached = []
        get_panel_gallery_items._cached_count = 0
        get_panel_gallery_items._cached_signature = ()


def get_panel_gallery_items(self, context):
    """Generate enum items for panel gallery template_icon_view."""
    from . import data_storage

    saved_views = data_storage.get_saved_views()
    current_count = len(saved_views)
    current_signature = tuple(v.get("name", "") for v in saved_views)
    cached_count = getattr(get_panel_gallery_items, '_cached_count', 0)
    cached_signature = getattr(get_panel_gallery_items, '_cached_signature', ())

    # Cache hit only when both count and names match.
    if (hasattr(get_panel_gallery_items, '_cached') and
        len(get_panel_gallery_items._cached) > 0 and
        cached_count == current_count and
        cached_signature == current_signature):
        debug_tools.inc("enum.panel_gallery.cache_hit")
        return get_panel_gallery_items._cached

    debug_tools.inc("enum.panel_gallery.items_build")

    items = []
    for i, view_dict in enumerate(saved_views):
        view_name = view_dict.get("name", f"View {i+1}")
        icon_id = get_preview_icon_id(view_name)

        identifier = str(i)
        name = str(view_name)
        description = f"Navigate to {view_name}"
        items.append((identifier, name, description, icon_id, i))

    if not items:
        items.append(('NONE', "No Views", "No saved views", 0, 0))

    get_panel_gallery_items._cached = items
    get_panel_gallery_items._cached_count = current_count
    get_panel_gallery_items._cached_signature = current_signature
    return items


get_panel_gallery_items._cached = []
get_panel_gallery_items._cached_count = 0
get_panel_gallery_items._cached_signature = ()


class VIEWPILOT_OT_reload_previews(bpy.types.Operator):
    """Reload thumbnail previews for panel gallery"""
    bl_idname = "viewpilot.reload_previews"
    bl_label = "Reload Previews"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        from . import data_storage
        reload_all_previews(context)
        self.report({'INFO'}, f"Reloaded {len(data_storage.get_saved_views())} previews")
        return {'FINISHED'}


@bpy.app.handlers.persistent
def on_file_load(dummy):
    """Reload previews when a file is opened."""
    # Use timer to delay - context may not be fully ready immediately
    def delayed_reload():
        try:
            context = bpy.context
            if context and hasattr(context, 'scene') and context.scene:
                reload_all_previews(context)
        except:
            pass
        return None  # Don't repeat
    
    bpy.app.timers.register(delayed_reload, first_interval=0.5)


def register():
    """Initialize preview collection."""
    global preview_collections
    preview_collections["viewpilot_previews"] = bpy.utils.previews.new()
    bpy.utils.register_class(VIEWPILOT_OT_reload_previews)
    if on_file_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_file_load)


def unregister():
    """Clean up preview collection."""
    global preview_collections, _preview_serial
    
    # Remove handler
    if on_file_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_file_load)
    
    bpy.utils.unregister_class(VIEWPILOT_OT_reload_previews)
    
    for pcoll in preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    preview_collections.clear()
    _active_preview_ids.clear()
    _preview_serial = 0
