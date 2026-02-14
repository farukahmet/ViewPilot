"""Preview manager for ViewPilot panel icon gallery.

Panel icons prefer Image datablock previews (stable across runtime changes).
The file-backed preview collection path remains as a fallback.
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
_last_saved_views_signature = ()
_undo_refresh_queued = False
_icon_retry_queued = False
_is_registered = False


def _next_preview_id(view_name):
    """Generate a unique preview id for incremental replacement."""
    global _preview_serial
    _preview_serial += 1
    return f"vp_{sanitize_token(view_name)}_{_preview_serial}"


def _compute_saved_views_signature():
    """Build a cheap signature so undo/redo refresh only runs when needed."""
    from . import data_storage

    try:
        views = data_storage.get_saved_views()
    except Exception:
        return ()

    signature = []
    for view in views:
        signature.append(
            (
                view.get("name", ""),
                view.get("thumbnail_image", ""),
            )
        )
    return tuple(signature)


def _preview_cache_out_of_sync(signature):
    """Return True when preview mappings don't match current saved views."""
    expected_names = [entry[0] for entry in signature]
    if len(_active_preview_ids) != len(expected_names):
        return True

    try:
        pcoll = get_preview_collection()
    except Exception:
        return True

    for view_name in expected_names:
        preview_id = _active_preview_ids.get(view_name)
        if not preview_id:
            return True
        if preview_id not in pcoll:
            return True
    return False


def _mark_saved_views_signature():
    """Update the cached signature for change detection."""
    global _last_saved_views_signature
    _last_saved_views_signature = _compute_saved_views_signature()


def _request_gallery_refresh():
    """Request modal gallery texture reload if it is active."""
    try:
        from .modal_gallery import VIEW3D_OT_thumbnail_gallery

        if VIEW3D_OT_thumbnail_gallery._is_active:
            VIEW3D_OT_thumbnail_gallery.request_refresh()
    except Exception:
        pass


def _tag_view3d_redraw():
    """Request redraw in all 3D view areas so panel icon updates become visible."""
    try:
        wm = bpy.context.window_manager
        for window in wm.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _queue_panel_icon_retry():
    """Retry panel icon refresh once when preview icon ids are still pending."""
    global _icon_retry_queued
    if _icon_retry_queued:
        return

    _icon_retry_queued = True

    def _retry():
        global _icon_retry_queued
        _icon_retry_queued = False
        invalidate_panel_gallery_cache()
        _request_gallery_refresh()
        return None

    bpy.app.timers.register(_retry, first_interval=0.15)


def _queue_undo_refresh(reason):
    """Queue a one-shot refresh after undo/redo changes saved views."""
    global _undo_refresh_queued
    if _undo_refresh_queued:
        return

    _undo_refresh_queued = True

    def _delayed_refresh():
        global _undo_refresh_queued
        _undo_refresh_queued = False
        try:
            context = bpy.context
            if context and hasattr(context, 'scene') and context.scene:
                reload_all_previews(context)
                _request_gallery_refresh()
                debug_tools.log(f"undo/redo preview refresh applied ({reason})")
        except Exception as e:
            debug_tools.log(f"undo/redo preview refresh failed ({reason}): {e}")
        finally:
            _mark_saved_views_signature()
        return None

    bpy.app.timers.register(_delayed_refresh, first_interval=0.05)


def _write_preview_temp_file(image, view_name):
    """Export a packed blender image to temp file for preview loading."""
    temp_path = make_temp_png_path("vp_preview_", view_name)
    
    # Primary path: color-managed render write (matches gallery display expectations).
    try:
        image.save_render(temp_path)
        if os.path.exists(temp_path):
            return temp_path
    except Exception as e:
        debug_tools.log(f"preview temp export failed for '{view_name}': {e}")
    
    # Fallback path: direct image save. This avoids cases where save_render()
    # emits warnings and returns without producing a file.
    orig_filepath_raw = getattr(image, "filepath_raw", "")
    orig_file_format = getattr(image, "file_format", "PNG")
    try:
        image.filepath_raw = temp_path
        image.file_format = 'PNG'
        image.save()
        if os.path.exists(temp_path):
            return temp_path
    except Exception as e:
        debug_tools.log(f"preview temp direct save failed for '{view_name}': {e}")
    finally:
        try:
            image.filepath_raw = orig_filepath_raw
            image.file_format = orig_file_format
        except Exception:
            pass
    
    return None


def get_preview_collection():
    """Get the ViewPilot preview collection, creating if needed."""
    if "viewpilot_previews" not in preview_collections:
        preview_collections["viewpilot_previews"] = bpy.utils.previews.new()
    return preview_collections["viewpilot_previews"]


def load_view_preview(view_name, thumbnail_path, replace_existing=True):
    """Load or replace a thumbnail image in the preview collection."""
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        debug_tools.log(f"preview load skipped for '{view_name}': missing file '{thumbnail_path}'")
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
        icon_id = pcoll[preview_id].icon_id
        if not icon_id:
            _queue_panel_icon_retry()
        return icon_id
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


def get_view_icon_id_fast(view_name, thumbnail_image=""):
    """Fast icon lookup for UI lists without triggering preview refresh work."""
    icon_id = _get_image_preview_icon_id(thumbnail_image)
    if icon_id:
        return icon_id
    return get_preview_icon_id(view_name)


def remove_view_preview(view_name):
    """Forget active preview mapping for a deleted/renamed view."""
    if view_name in _active_preview_ids:
        del _active_preview_ids[view_name]
    invalidate_panel_gallery_cache()


def _resolve_thumbnail_image_name(view_name):
    """Resolve thumbnail image datablock name for a saved view."""
    from . import data_storage

    direct_name = f".VP_Thumb_{view_name}"
    if bpy.data.images.get(direct_name):
        return direct_name

    try:
        for view_dict in data_storage.get_saved_views():
            if view_dict.get("name", "") != view_name:
                continue
            thumb_name = view_dict.get("thumbnail_image", "")
            if thumb_name and bpy.data.images.get(thumb_name):
                return thumb_name
    except Exception:
        pass

    return direct_name


def _get_image_preview_icon_id(image_name):
    """Return icon_id from an Image datablock preview, or 0 if unavailable."""
    if not image_name:
        return 0
    img = bpy.data.images.get(image_name)
    if not img:
        return 0
    try:
        # Ensure preview is generated for this image datablock.
        img.preview_ensure()
        preview = getattr(img, "preview", None)
        if preview:
            icon_id = getattr(preview, "icon_id", 0) or 0
            return icon_id
    except Exception:
        pass
    return 0


def refresh_view_preview(view_name):
    """Refresh only one view preview after thumbnail regeneration."""
    debug_tools.inc("preview.refresh_one")

    image_name = _resolve_thumbnail_image_name(view_name)
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
        # Always invalidate cache after a load attempt so enum rebuild can
        # pick up asynchronous icon_id updates.
        invalidate_panel_gallery_cache()

        if icon_id:
            debug_tools.inc("preview.refresh_one.success")
        else:
            debug_tools.inc("preview.refresh_one.load_failed")
            _queue_panel_icon_retry()
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
            image_name = view_dict.get("thumbnail_image", "") or f".VP_Thumb_{view_name}"
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
    """Invalidate panel icon view state."""
    _tag_view3d_redraw()


def get_panel_gallery_items(self, context):
    """Generate enum items for panel gallery template_icon_view."""
    from . import data_storage

    saved_views = data_storage.get_saved_views()
    # Always rebuild items for panel icon view. Custom preview icon ids may
    # transition from 0->valid asynchronously, and cache hits can freeze the
    # panel in a permanent "loading" state.
    debug_tools.inc("enum.panel_gallery.items_build")

    items = []
    has_pending_icons = False
    for i, view_dict in enumerate(saved_views):
        view_name = view_dict.get("name", f"View {i+1}")
        thumb_name = view_dict.get("thumbnail_image", "")
        has_thumb = bool(thumb_name)

        # Preferred path: use the image datablock preview icon directly.
        icon_id = _get_image_preview_icon_id(thumb_name)

        # Fallback path: custom preview collection.
        if not icon_id:
            icon_id = get_preview_icon_id(view_name)
        
        # Lazy self-heal: if preview mapping is missing, try rebuilding once
        # from the packed thumbnail image while we build enum items.
        if has_thumb and not icon_id:
            icon_id = refresh_view_preview(view_name)
            if not icon_id:
                has_pending_icons = True

        identifier = str(i)
        name = str(view_name)
        description = f"Navigate to {view_name}"
        items.append((identifier, name, description, icon_id, i))

    if not items:
        items.append(('NONE', "No Views", "No saved views", 0, 0))

    if has_pending_icons:
        _queue_panel_icon_retry()
    return items


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
                _request_gallery_refresh()
        except Exception:
            pass
        finally:
            _mark_saved_views_signature()
        return None  # Don't repeat
    
    bpy.app.timers.register(delayed_reload, first_interval=0.5)


@bpy.app.handlers.persistent
def on_undo_post(dummy):
    """Refresh previews after undo only when saved views actually changed."""
    current_signature = _compute_saved_views_signature()
    if (current_signature == _last_saved_views_signature and
        not _preview_cache_out_of_sync(current_signature)):
        return
    _queue_undo_refresh("undo")


@bpy.app.handlers.persistent
def on_redo_post(dummy):
    """Refresh previews after redo only when saved views actually changed."""
    current_signature = _compute_saved_views_signature()
    if (current_signature == _last_saved_views_signature and
        not _preview_cache_out_of_sync(current_signature)):
        return
    _queue_undo_refresh("redo")


def register():
    """Initialize preview collection."""
    global preview_collections, _is_registered

    if _is_registered:
        return

    if "viewpilot_previews" in preview_collections:
        try:
            bpy.utils.previews.remove(preview_collections["viewpilot_previews"])
        except Exception:
            pass
        preview_collections.pop("viewpilot_previews", None)

    preview_collections["viewpilot_previews"] = bpy.utils.previews.new()

    try:
        bpy.utils.register_class(VIEWPILOT_OT_reload_previews)
    except Exception:
        # Already registered from a non-ideal reload path.
        pass

    if on_file_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_file_load)
    if on_undo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(on_undo_post)
    if on_redo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(on_redo_post)
    _mark_saved_views_signature()
    _is_registered = True


def unregister():
    """Clean up preview collection."""
    global preview_collections, _preview_serial, _last_saved_views_signature, _undo_refresh_queued, _icon_retry_queued, _is_registered
    
    # Remove handler
    if on_file_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_file_load)
    if on_undo_post in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(on_undo_post)
    if on_redo_post in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(on_redo_post)
    
    try:
        bpy.utils.unregister_class(VIEWPILOT_OT_reload_previews)
    except Exception:
        # Class may already be unregistered in non-ideal reload paths.
        pass
    
    for pcoll in preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    preview_collections.clear()
    _active_preview_ids.clear()
    _preview_serial = 0
    _last_saved_views_signature = ()
    _undo_refresh_queued = False
    _icon_retry_queued = False
    _is_registered = False
