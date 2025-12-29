"""
Preview Collection Manager for ViewPilot Panel Gallery.

Manages GPU preview icons for displaying saved view thumbnails in template_icon_view.

Why use temp files
--------------------------------
bpy.utils.previews (preview collections) can ONLY load images from file paths.
There is no API to load directly from bpy.data.images. Therefore we have to:

1. Get the Blender internal image (.VP_Thumb_*)
2. Save it to a temp file with save_render()
3. Load that file into pcoll with pcoll.load(path)

pcoll.load() appears to either load lazily or keep a reference to the filepath, 
so deleting the temp files too quickly results in blank icons. They are small PNGs 
in the system temp folder and get overwritten on each refresh - the OS will eventually 
clean them up.
"""

import bpy
import bpy.utils.previews
import os

# Global storage for preview collections
preview_collections = {}


def get_preview_collection():
    """Get the ViewPilot preview collection, creating if needed."""
    global preview_collections
    
    if "viewpilot_previews" not in preview_collections:
        preview_collections["viewpilot_previews"] = bpy.utils.previews.new()
    
    return preview_collections["viewpilot_previews"]


def load_view_preview(view_name, thumbnail_path):
    """Load a thumbnail image into the preview collection."""
    pcoll = get_preview_collection()
    
    # Use view name as identifier (spaces replaced for safety)
    preview_id = f"vp_{view_name}"
    
    # Skip if preview already exists (can't remove individual items from pcoll)
    # The image will be updated next time the collection is cleared
    if preview_id in pcoll:
        return pcoll[preview_id].icon_id
    
    # Only load if file exists
    if os.path.exists(thumbnail_path):
        try:
            pcoll.load(preview_id, thumbnail_path, 'IMAGE')
            return pcoll[preview_id].icon_id
        except Exception as e:
            print(f"[ViewPilot] Could not load preview for {view_name}: {e}")
            return 0
    
    return 0


def get_preview_icon_id(view_name):
    """Get the icon_id for a view's preview, or 0 if not loaded."""
    pcoll = get_preview_collection()
    preview_id = f"vp_{view_name}"
    
    if preview_id in pcoll:
        return pcoll[preview_id].icon_id
    return 0


def refresh_view_preview(view_name):
    """Refresh a single view's preview in the panel gallery. Call after thumbnail generation."""
    import tempfile
    
    # Find the Blender image
    image_name = f".VP_Thumb_{view_name}"
    img = bpy.data.images.get(image_name)
    
    if img:
        try:
            # Sanitize filename
            safe_name = view_name.replace(" ", "_").replace(".", "_")
            temp_path = os.path.join(tempfile.gettempdir(), f"vp_preview_{safe_name}.png")
            img.save_render(temp_path)
            load_view_preview(view_name, temp_path)
            # Don't delete temp file - pcoll may need it to persist
            # Invalidate cache so enum regenerates with new icon
            invalidate_panel_gallery_cache()
        except:
            pass


def reload_all_previews(context):
    """Reload all view previews from Blender's internal images."""
    import tempfile
    from . import data_storage
    
    pcoll = get_preview_collection()
    pcoll.clear()
    
    saved_views = data_storage.get_saved_views()
    for view_dict in saved_views:
        view_name = view_dict.get("name", "View")
        # Match the thumbnail naming convention from thumbnail_generator
        image_name = f".VP_Thumb_{view_name}"
        img = bpy.data.images.get(image_name)
        
        if img:
            # Save to temp file, then load into preview collection
            # (preview collections can only load from files)
            try:
                # Sanitize filename - replace spaces and special chars
                safe_name = view_name.replace(" ", "_").replace(".", "_")
                temp_path = os.path.join(tempfile.gettempdir(), f"vp_preview_{safe_name}.png")
                img.save_render(temp_path)
                load_view_preview(view_name, temp_path)
                # Don't delete temp file - pcoll may need it to persist
            except Exception as e:
                pass  # Silently skip failed previews
    
    # Invalidate the enum items cache so it regenerates with new icon_ids
    invalidate_panel_gallery_cache()


def invalidate_panel_gallery_cache():
    """Clear the panel gallery enum cache. Call when views are added/deleted/renamed."""
    if hasattr(get_panel_gallery_items, '_cached'):
        get_panel_gallery_items._cached = []
        get_panel_gallery_items._cached_count = 0


def get_panel_gallery_items(self, context):
    """Generate enum items for panel gallery template_icon_view."""
    from . import data_storage
    
    pcoll = get_preview_collection()
    
    # Check if we can return cached items (views haven't changed)
    saved_views = data_storage.get_saved_views()
    current_count = len(saved_views)
    cached_count = getattr(get_panel_gallery_items, '_cached_count', 0)
    
    # If we have cached items and count matches, return cache
    if (hasattr(get_panel_gallery_items, '_cached') and 
        len(get_panel_gallery_items._cached) > 0 and
        cached_count == current_count):
        return get_panel_gallery_items._cached
    
    # Generate new items
    items = []
    
    for i, view_dict in enumerate(saved_views):
        view_name = view_dict.get("name", f"View {i+1}")
        preview_id = f"vp_{view_name}"
        icon_id = pcoll[preview_id].icon_id if preview_id in pcoll else 0
        
        # CRITICAL: Create persistent string copies to prevent garbage collection
        # Blender's C UI holds references to these strings, so they must not be deallocated
        identifier = str(i)
        name = str(view_name)  # Explicit copy
        description = f"Navigate to {view_name}"
        
        # Format: (identifier, name, description, icon_id, index)
        items.append((identifier, name, description, icon_id, i))
    
    # Ensure at least one item (Blender requirement)
    if not items:
        items.append(('NONE', "No Views", "No saved views", 0, 0))
    
    # CRITICAL: Cache items AND count to prevent Python garbage collection
    # This must happen BEFORE returning
    get_panel_gallery_items._cached = items
    get_panel_gallery_items._cached_count = current_count
    return items

get_panel_gallery_items._cached = []
get_panel_gallery_items._cached_count = 0


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
    bpy.app.handlers.load_post.append(on_file_load)


def unregister():
    """Clean up preview collection."""
    global preview_collections
    
    # Remove handler
    if on_file_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_file_load)
    
    bpy.utils.unregister_class(VIEWPILOT_OT_reload_previews)
    
    for pcoll in preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    preview_collections.clear()
