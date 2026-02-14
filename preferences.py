"""
Preferences and Property Groups for ViewPilot.
"""

import bpy

# ============================================================================
# PREFERENCE UPDATE CALLBACKS
# ============================================================================

def _iter_viewpilot_camera_collections():
    """Yield (scene, collection) pairs for all ViewPilot-tagged camera collections."""
    seen = set()

    def _walk(scene, parent):
        for child in parent.children:
            if child.get("is_viewport_cameras_collection"):
                ptr = child.as_pointer()
                if ptr not in seen:
                    seen.add(ptr)
                    yield (scene, child)
            yield from _walk(scene, child)

    for scene in bpy.data.scenes:
        yield from _walk(scene, scene.collection)


def update_collection_name(self, context):
    """Update existing viewport cameras collection name when preference changes."""
    for scene, coll in _iter_viewpilot_camera_collections():
        coll["viewpilot_base_name"] = self.camera_collection_name
        coll.name = f"{self.camera_collection_name} [{scene.name}]"


def update_collection_color(self, context):
    """Update existing viewport cameras collection color when preference changes."""
    for _scene, coll in _iter_viewpilot_camera_collections():
        coll.color_tag = self.camera_collection_color


# ============================================================================
# ADDON PREFERENCES
# ============================================================================

class ViewportCameraControlsPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    
    # UI Location Visibility
    show_popup_menu: bpy.props.BoolProperty(
        name="Shortcut Popup",
        description="Enable the shortcut popup menu. You can set the shortcut in the section below",
        default=True
    )
    
    show_header_button: bpy.props.BoolProperty(
        name="Header",
        description="Show ViewPilot button in the 3D Viewport header (top right)",
        default=False
    )
    
    show_n_panel: bpy.props.BoolProperty(
        name="N-Panel",
        description="Show ViewPilot panel in the N-Panel sidebar",
        default=True
    )
    
    show_topbar_saved_views: bpy.props.BoolProperty(
        name="Topbar (Saved Views)",
        description="Show saved views dropdown in the top bar (near Scenes/View Layers)",
        default=True
    )
    
    # Main Settings
    popup_width: bpy.props.IntProperty(
        name="Popup Width",
        description="Width of the Camera Controls popup",
        default=300,
        min=200,
        max=600
    )
    
    # Shared Panel Section Visibility (used by all panels: popup, N-panel, header)
    # Each section has a toggle for viewport mode and camera mode
    section_show_history: bpy.props.BoolProperty(name="History", default=True)
    section_show_history_cam: bpy.props.BoolProperty(name="", description="Show in camera view", default=False)
    section_show_lens: bpy.props.BoolProperty(name="Lens", default=True)
    section_show_lens_cam: bpy.props.BoolProperty(name="", description="Show in camera view", default=True)
    section_show_transform: bpy.props.BoolProperty(name="Transform", default=True)
    section_show_transform_cam: bpy.props.BoolProperty(name="", description="Show in camera view", default=True)
    section_show_saved_views: bpy.props.BoolProperty(name="Views", default=True)
    section_show_saved_views_cam: bpy.props.BoolProperty(name="", description="Show in camera view", default=False)
    section_show_overlays: bpy.props.BoolProperty(name="Camera Overlays", description="Only relevant in camera view", default=False)
    section_show_overlays_cam: bpy.props.BoolProperty(name="", description="Show in camera view", default=True)
    
    # Advanced Settings
    history_max_size: bpy.props.IntProperty(
        name="History Buffer Size",
        description="Maximum number of view states to remember",
        default=20,
        min=5,
        max=100
    )

    debug_enabled: bpy.props.BoolProperty(
        name="Debug Mode",
        description="Enable ViewPilot debug counters and timings (prints to console)",
        default=False
    )
    
    settle_delay: bpy.props.FloatProperty(
        name="Settle Delay",
        description="Time to wait after movement stops before saving a history state (seconds)",
        default=0.3,
        min=0.1,
        max=2.0,
        precision=2
    )
    
    start_gallery_on_load: bpy.props.BoolProperty(
        name="Start Gallery on Load",
        description="Automatically open the thumbnail gallery when a file is loaded or addon is enabled",
        default=True
    )
    
    # Camera Creation Settings
    make_camera_active: bpy.props.BoolProperty(
        name="Activate on Creation",
        description="Set the new camera as active and switch to camera view",
        default=True
    )
    
    show_camera_sensor: bpy.props.BoolProperty(
        name="Show Sensor",
        description="Display the camera sensor boundary in the viewport",
        default=True
    )
    
    # =========================================================================
    # View Styles Defaults (for new saved views)
    # =========================================================================
    default_remember_perspective: bpy.props.BoolProperty(
        name="Perspective",
        description="Views remember viewport camera position by default",
        default=True
    )
    default_remember_shading: bpy.props.BoolProperty(
        name="Shading",
        description="Views remember shading mode by default. Wireframe/Solid/Eevee",
        default=True
    )
    default_remember_overlays: bpy.props.BoolProperty(
        name="Overlays",
        description="Views remember overlay settings by default",
        default=True
    )
    default_remember_composition: bpy.props.BoolProperty(
        name="Composition",
        description="Views remember scene + view layer by default",
        default=True
    )
    
    use_camera_collection: bpy.props.BoolProperty(
        name="Put in Dedicated Collection",
        description="Place created cameras in a dedicated collection",
        default=True
    )
    
    camera_collection_name: bpy.props.StringProperty(
        name="Collection Name",
        description="Name for the dedicated camera collection",
        default="ViewPilot",
        update=update_collection_name
    )

    camera_collection_color: bpy.props.EnumProperty(
        name="Collection Color",
        description="Color tag for the camera collection",
        items=[
            ('NONE', "None", "", 'OUTLINER_COLLECTION', 0),
            ('COLOR_01', "Red", "", 'COLLECTION_COLOR_01', 1),
            ('COLOR_02', "Orange", "", 'COLLECTION_COLOR_02', 2),
            ('COLOR_03', "Yellow", "", 'COLLECTION_COLOR_03', 3),
            ('COLOR_04', "Green", "", 'COLLECTION_COLOR_04', 4),
            ('COLOR_05', "Blue", "", 'COLLECTION_COLOR_05', 5),
            ('COLOR_06', "Violet", "", 'COLLECTION_COLOR_06', 6),
            ('COLOR_07', "Pink", "", 'COLLECTION_COLOR_07', 7),
            ('COLOR_08', "Brown", "", 'COLLECTION_COLOR_08', 8),
        ],
        default='COLOR_04',  # Green
        update=update_collection_color
    )
    
    show_camera_name: bpy.props.BoolProperty(
        name="Show Camera Name",
        description="Display the camera's name in the viewport",
        default=True
    )
    
    camera_name_prefix: bpy.props.StringProperty(
        name="Camera Name",
        description="Name prefix for created cameras",
        default="PilotCam"
    )

    show_passepartout: bpy.props.BoolProperty(
        name="Show Passepartout",
        description="Display the camera's passepartout in the viewport",
        default=True
    )

    camera_passepartout: bpy.props.FloatProperty(
        name="Camera Passepartout",
        description="Opacity of the darkened area outside the camera frame (0 = transparent, 1 = opaque)",
        default=0.95,
        min=0.0,
        max=1.0,
        precision=2,
        subtype='FACTOR'
    )
    
    # Views Settings
    thumbnail_size_max: bpy.props.IntProperty(
        name="Thumbnail Size",
        description="Maximum size of gallery thumbnails in pixels",
        default=100,
        min=50,
        max=256
    )
    
    preview_backdrop_opacity: bpy.props.FloatProperty(
        name="Preview Backdrop",
        description="Opacity of dark backdrop when previewing thumbnail with MMB (0 = transparent, 1 = opaque)",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR'
    )
    
    preview_size_factor: bpy.props.FloatProperty(
        name="Preview Size",
        description="Size of enlarged preview as fraction of viewport (0 = 20%, 1 = 100%)",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype='FACTOR'
    )
    
    # Lens Settings
    default_lens_unit: bpy.props.EnumProperty(
        name="",
        description="Default unit for displaying lens/zoom values",
        items=[
            ('FOV', "Field of View (deg)", "Field of View in degrees"),
            ('MM', "Focal Length (mm)", "Focal length in millimeters"),
        ],
        default='FOV'
    )
    
    def draw(self, context):
        layout = self.layout
        
        # ///// TWO MAIN COLUMNS /////
        split = layout.split(factor=0.5)
        
        # ===== LEFT COLUMN =====
        col_left = split.column()
        
        # --- Show Panel In ---
        box = col_left.box()
        row = box.row()
        row.label(text="SHOW PANEL IN", icon='RESTRICT_VIEW_OFF')
        
        col = box.column()
        col.prop(self, "show_topbar_saved_views", toggle=True)
        row = box.row(align=True)
        row.scale_y = 2
        row.prop(self, "show_popup_menu", toggle=True)
        row.prop(self, "show_header_button", toggle=True)
        row.prop(self, "show_n_panel", toggle=True)
    
        # Popup Width
        row = box.row()
        split_row = row.split(factor=0.5)
        split_row.alignment = 'RIGHT'
        split_row.label(text="Popup Width:")
        split_row.prop(self, "popup_width", text="")
            
        # --- Panel Sections (shared across all panels) ---
        box = col_left.box()
        row = box.row()
        row.label(text="PANEL SECTIONS", icon='WINDOW')
        
        # Two-column layout: Section name | Camera icon toggle
        col = box.column(align=True)
        col.scale_y = 1.4
        
        sub = col.split(factor=0.8, align=True)
        sub.prop(self, "section_show_history", toggle=True)
        sub.prop(self, "section_show_history_cam", toggle=True, icon='CAMERA_DATA')
        
        sub = col.split(factor=0.8, align=True)
        sub.prop(self, "section_show_lens", toggle=True)
        sub.prop(self, "section_show_lens_cam", toggle=True, icon='CAMERA_DATA')
        
        sub = col.split(factor=0.8, align=True)
        sub.prop(self, "section_show_transform", toggle=True)
        sub.prop(self, "section_show_transform_cam", toggle=True, icon='CAMERA_DATA')
        
        sub = col.split(factor=0.8, align=True)
        sub.prop(self, "section_show_saved_views", toggle=True)
        sub.prop(self, "section_show_saved_views_cam", toggle=True, icon='CAMERA_DATA')
        
        sub = col.split(factor=0.8, align=True)
        sub_disabled = sub.row(align=True)
        sub_disabled.enabled = False
        sub_disabled.prop(self, "section_show_overlays", toggle=True)
        sub.prop(self, "section_show_overlays_cam", toggle=True, icon='CAMERA_DATA')

        # --- Keyboard Shortcuts ---
        box = col_left.box()
        row = box.row()
        row.label(text="KEYBOARD SHORTCUTS", icon='EVENT_A')
        col = box.column(align=True)
        col.scale_y = 1.1
        
        wm = context.window_manager
        kc = wm.keyconfigs.user
        if kc:
            km = kc.keymaps.get("3D View")
            if km:
                shortcut_order = [
                        ("view3d.viewport_controls", "Open Popup"),
                        ("view3d.view_history_back", "History Back"),
                        ("view3d.view_history_forward", "History Forward"),
                        ("view3d.prev_saved_view", "Previous View"),
                        ("view3d.next_saved_view", "Next View"),
                    ]
                for idname, label in shortcut_order:
                    for kmi in km.keymap_items:
                        if kmi.idname == idname:
                            row = col.row(align=True)
                            row.prop(kmi, "active", text="", emboss=False)
                            row.label(text=label)
                            row.prop(kmi, "type", text="", full_event=True)
                            break

        # --- Debug ---
        box = col_left.box()
        row = box.row()
        row.label(text="DEBUG", icon='INFO')

        col = box.column(align=True)
        col.prop(self, "debug_enabled")

        row = col.row(align=True)
        row.operator("viewpilot.debug_print_stats", text="Print Stats")
        row.operator("viewpilot.debug_reset_stats", text="Reset Stats")
        
        # ===== RIGHT COLUMN =====
        col_right = split.column()
        
        # --- DEFAULTS (main section with subsections) ---
        box = col_right.box()
        row = box.row()
        row.label(text="DEFAULTS", icon='PREFERENCES')
        
        # --- History Subsection ---
        sub_box = box.box()
        row = sub_box.row()
        row.label(text="History", icon='SCREEN_BACK')
        
        col = sub_box.column(align=True)
        row = col.row(align=True)
        split_row = row.split(factor=0.5)
        split_row.label(text="Buffer Size:")
        split_row.prop(self, "history_max_size", text="")
            
        row = col.row(align=True)
        split_row = row.split(factor=0.5)
        split_row.label(text="Settle Delay:")
        split_row.prop(self, "settle_delay", text="")
        
        # --- Lens Subsection ---
        sub_box = box.box()
        row = sub_box.row()
        row.label(text="Lens", icon='CAMERA_DATA')
        
        row = sub_box.row(align=True)
        split_row = row.split(factor=0.5)
        split_row.label(text="Lens Unit:")
        split_row.prop(self, "default_lens_unit", expand=False)
        
        # --- Views Subsection ---
        sub_box = box.box()
        row = sub_box.row()
        row.label(text="View Gallery", icon='BOOKMARKS')
        
        row = sub_box.row()
        row.prop(self, "start_gallery_on_load")

        col = sub_box.column(align=True)            
        row = col.row(align=True)
        split_row = row.split(factor=0.5)
        split_row.label(text="Thumbnail Size:")
        split_row.prop(self, "thumbnail_size_max", text="")
        
        row = col.row(align=True)
        split_row = row.split(factor=0.5)
        split_row.label(text="MMB Big Preview Size:")
        split_row.prop(self, "preview_size_factor", text="")
        
        row = col.row(align=True)
        split_row = row.split(factor=0.5)
        split_row.label(text="MMB Big Preview Backdrop:")
        split_row.prop(self, "preview_backdrop_opacity", text="")
        
        # --- View Styles Subsection ---
        sub_box = box.box()
        row = sub_box.row()
        row.label(text="View Styles—Remember", icon='PRESET')
        
        row = sub_box.row(align=True)
        row.prop(self, "default_remember_perspective", toggle=True)
        row.prop(self, "default_remember_shading", toggle=True)
        row.prop(self, "default_remember_overlays", toggle=True)
        row.prop(self, "default_remember_composition", toggle=True)
    
        # --- Camera Subsection ---
        sub_box = box.box()
        row = sub_box.row()
        row.label(text="Camera from View", icon='OUTLINER_OB_CAMERA')
        
        row = sub_box.row(align=True)
        row.prop(self, "make_camera_active")
            
        # Collection with name field and color
        row = sub_box.row()
        col = row.column(align=True)
        split_row = col.split(factor=0.5)
        split_row.prop(self, "use_camera_collection", text="Put in Collection:")
        sub = split_row.row(align=True)
        sub.active = self.use_camera_collection
        sub.prop(self, "camera_collection_name", text="")
        sub.prop(self, "camera_collection_color", text="", icon_only=True)
        
        # Camera name with field
        row = col.row()
        col = row.column(align=True)
        split_row = col.split(factor=0.5)
        split_row.prop(self, "show_camera_name", text="Show Camera Name:")
        split_row.prop(self, "camera_name_prefix", text="")

        # Passepartout            
        row = col.row()
        col = row.column(align=True)
        split_row = col.split(factor=0.5)
        split_row.prop(self, "show_passepartout", text="Passepartout:")
        split_row.prop(self, "camera_passepartout", text="")

        # Camera sensor
        col.prop(self, "show_camera_sensor")

# ============================================================================
# HELPER FUNCTION
# ============================================================================

def get_preferences():
    """Get addon preferences."""
    return bpy.context.preferences.addons[__package__].preferences


# ============================================================================
# REGISTRATION
# ============================================================================

classes = [
    ViewportCameraControlsPreferences,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
