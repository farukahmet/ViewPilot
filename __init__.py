bl_info = {
    "name"          : "ViewPilot",
    "description"   : "Control, capture, and recall *exactly* what you see in your viewport.",
    "author"        : "Faruk Ahmet",
    "version"       : (1, 0, 0),
    "blender"       : (4, 2, 0),
    "category"      : "3D View",
    "location"      : "N-Panel > ViewPilot",
}

import bpy
import importlib
from . import utils
from . import preferences
from . import debug_tools
from . import operators
from . import ui
from . import properties
from . import temp_paths
from . import thumbnail_generator
from . import modal_gallery
from . import preview_manager
from . import data_storage

from .utils import reset_history_handler, start_monitor
from .preferences import get_preferences


class VIEW3D_OT_viewport_controls(bpy.types.Operator):
    bl_idname = "view3d.viewport_controls"
    bl_label = "ViewPilot"
    bl_options = {'REGISTER', 'UNDO'} 
    
    def invoke(self, context, event):
        # Initialize scene properties from current context
        context.scene.viewpilot.reinitialize_from_context(context)
        
        try:
            popup_width = get_preferences().popup_width
        except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
            popup_width = 300
            
        return context.window_manager.invoke_props_dialog(self, width=popup_width)
    
    def draw(self, context):
        ui.draw_viewpilot_controls(self.layout, context)
        
    def execute(self, context):
        return {'FINISHED'}

# ============================================================================
# KEYMAPS
# ============================================================================

addon_keymaps = []


def register():
    # Reload modules to pick up changes without restarting Blender.
    # Order matters:
    # - thumbnail_generator must come BEFORE operators, because operators imports
    #   generate_thumbnail/delete_thumbnail directly.
    # - modal_gallery must come BEFORE operators (operators imports from it).
    importlib.reload(utils)
    importlib.reload(preferences)
    importlib.reload(debug_tools)
    importlib.reload(data_storage)  # Before properties!
    importlib.reload(temp_paths)
    importlib.reload(thumbnail_generator)  # Before operators!
    importlib.reload(modal_gallery)  # Before operators!
    importlib.reload(operators)
    importlib.reload(ui)
    importlib.reload(properties)
    importlib.reload(preview_manager)
    try:
        print(
            "[ViewPilot] Thumbnail module loaded:",
            getattr(thumbnail_generator, "__file__", "<unknown>"),
            "version=",
            getattr(thumbnail_generator, "THUMBNAIL_RENDERER_VERSION", "<unknown>"),
        )
    except (RuntimeError, ReferenceError, AttributeError, TypeError, ValueError):
        pass

    # Register Preferences
    preferences.register()
    debug_tools.register()
    properties.register()
    operators.register()
    ui.register()
    modal_gallery.register()
    preview_manager.register()
    
    # Data storage initialization is deferred using a timer because
    # bpy.data.texts is not available during addon registration
    def _deferred_init():
        try:
            data_storage.ensure_data_initialized()
            migrated = data_storage.migrate_from_scene_storage()
            if migrated > 0:
                print(f"[ViewPilot] Migrated {migrated} views from scene storage to JSON")
            
            # Initialize UUIDs for all scenes and view layers
            data_storage.initialize_all_uuids()
            
            # Sync JSON to PropertyGroup for UIList compatibility
            if hasattr(bpy.context, 'scene') and bpy.context.scene:
                synced = data_storage.sync_to_all_scenes()
                if synced > 0:
                    print(f"[ViewPilot] Synced {synced} views to PropertyGroup")
        except (RuntimeError, ReferenceError, AttributeError, TypeError, ValueError, OSError) as e:
            print(f"[ViewPilot] Deferred init failed: {e}")
        return None  # Don't repeat
    
    bpy.app.timers.register(_deferred_init, first_interval=0.5)
    
    # Register Layout
    bpy.utils.register_class(VIEW3D_OT_viewport_controls)
    
    # Register Scene Properties
    bpy.types.Scene.viewpilot = bpy.props.PointerProperty(type=properties.ViewPilotProperties)
    
    # Scene properties for saved views
    bpy.types.Scene.saved_views = bpy.props.CollectionProperty(type=properties.SavedViewItem)
    bpy.types.Scene.saved_views_index = bpy.props.IntProperty(name="Active Saved View", default=-1)
    bpy.types.Scene.saved_views_next_number = bpy.props.IntProperty(
        name="Next View Number",
        description="Counter for auto-generating unique view names",
        default=1 
    )
    
    # Auto-start monitor
    bpy.app.timers.register(start_monitor, first_interval=0.5)
    
    # Register Load Handler (with duplicate guard)
    if reset_history_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(reset_history_handler)
    
    # Register depsgraph handler for collection name sync on scene rename
    if utils.viewpilot_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(utils.viewpilot_depsgraph_handler)
    
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
        
        # Open Popup (Grless) - ENABLED by default
        kmi = km.keymap_items.new(VIEW3D_OT_viewport_controls.bl_idname, type='GRLESS', value='CLICK', shift=False, ctrl=False)
        addon_keymaps.append((km, kmi))
        
        # History Back (Ctrl+Left) - disabled by default
        kmi = km.keymap_items.new(operators.VIEW3D_OT_view_history_back.bl_idname, type='LEFT_BRACKET', value='PRESS', ctrl=True)
        kmi.active = False
        addon_keymaps.append((km, kmi))
        
        # History Forward (Ctrl+Right) - disabled by default
        kmi = km.keymap_items.new(operators.VIEW3D_OT_view_history_forward.bl_idname, type='RIGHT_BRACKET', value='PRESS', ctrl=True)
        kmi.active = False
        addon_keymaps.append((km, kmi))
        
        # Previous Saved View (Alt+Left) - disabled by default
        kmi = km.keymap_items.new(operators.VIEW3D_OT_prev_saved_view.bl_idname, type='LEFT_BRACKET', value='PRESS', alt=True)
        kmi.active = False
        addon_keymaps.append((km, kmi))
        
        # Next Saved View (Alt+Right) - disabled by default
        kmi = km.keymap_items.new(operators.VIEW3D_OT_next_saved_view.bl_idname, type='RIGHT_BRACKET', value='PRESS', alt=True)
        kmi.active = False
        addon_keymaps.append((km, kmi))
        
def unregister():
    # Remove keymaps first
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
    
    # Remove load handler
    if reset_history_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(reset_history_handler)
    
    # Remove depsgraph handler
    if utils.viewpilot_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(utils.viewpilot_depsgraph_handler)
    
    # Remove Scene properties
    if hasattr(bpy.types.Scene, "viewpilot"):
        del bpy.types.Scene.viewpilot
    del bpy.types.Scene.saved_views_next_number
    del bpy.types.Scene.saved_views_index
    del bpy.types.Scene.saved_views
    
    # Unregister main popup operator
    bpy.utils.unregister_class(VIEW3D_OT_viewport_controls)
    
    # Unregister gallery
    modal_gallery.unregister()
    preview_manager.unregister()
    
    # Unregister UI module (header button, N-panel)
    ui.unregister()
    
    # Unregister operators from operators module
    operators.unregister()
    
    # Unregister preferences module 
    from . import preferences as pref_module
    from . import debug_tools as debug_tools_module
    debug_tools_module.unregister()
    pref_module.unregister()

    # Unregister properties module
    from . import properties as prop_module
    prop_module.unregister()

if __name__ == "__main__":
    register()
