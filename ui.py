# ui.py
"""
UI components for ViewPilot panels - shortcut popup, header popover, and N-panel.
"""

import bpy
from .preferences import get_preferences
from . import utils
from .modal_gallery import VIEW3D_OT_thumbnail_gallery

# =============================================================================
# SHARED DRAW FUNCTIONS
# =============================================================================

def draw_viewpilot_controls(layout, context, location='popup'):
    """Draw the full set of ViewPilot controls.
    
    Args:
        layout: The layout to draw into
        context: The blender context
        location: 'popup', 'npanel', or 'header' - determines which visibility settings to use
    """
    props = context.scene.viewpilot
    
    # Detect mode for UI logic
    in_camera_mode = props.is_camera_mode
    
    # Get visibility preferences (shared across all panel locations)
    from . import preferences
    try:
        prefs = preferences.get_preferences()
        show_lens = prefs.section_show_lens if not in_camera_mode else prefs.section_show_lens_cam
        show_transform = prefs.section_show_transform if not in_camera_mode else prefs.section_show_transform_cam
        show_history = prefs.section_show_history if not in_camera_mode else prefs.section_show_history_cam
        show_saved_views = prefs.section_show_saved_views if not in_camera_mode else prefs.section_show_saved_views_cam
        show_viewport_display = prefs.section_show_overlays_cam if in_camera_mode else False
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
        show_lens = True
        show_transform = True
        show_history = True
        show_saved_views = True
        show_viewport_display = in_camera_mode

    # History
    if show_history:
        from . import utils as utils_module
        hist_idx = utils_module.view_history_index
        hist_len = len(utils_module.view_history)
        
        split = layout.split(align=True, factor=0.52)
        split.label(text=f"History {hist_idx + 1 if hist_idx != -1 else hist_len}/{hist_len}", icon='SCREEN_BACK')
        split.operator("view3d.view_history_back", text="", icon='FRAME_PREV')
        split.operator("view3d.view_history_forward", text="", icon='FRAME_NEXT')
        
        layout.separator()

    # Lens & Clipping
    if show_lens:
        col_lens = layout.column(align=True)
        # Header row: Lens label (left), camera controls (center), settings icon (right)
        row_header = col_lens.row(align=False)
        
        # Left: Lens label
        row_header.label(text="Lens", icon='CAMERA_DATA')
        
        # Center: Camera controls or Viewport label
        if in_camera_mode:
            cam = context.scene.camera
            if cam:
                sub_cam = row_header.row(align=True)
                sub_cam.alignment = 'CENTER'
                # Keep Camera Active toggle - when enabled, camera stays selected
                is_mode_active = props.keep_camera_active
                icon = 'RESTRICT_SELECT_OFF' if is_mode_active else 'RESTRICT_SELECT_ON'
                sub_cam.operator("view3d.toggle_camera_selection", text="", icon=icon, depress=is_mode_active)
                # Camera dropdown (for switching between cameras)
                sub_cam.prop(props, "camera_enum", text="")
                # Exit camera view button
                sub_cam.operator("view3d.exit_camera_view", text="", icon='X')
            else:
                # Fallback: should not happen in camera mode, but just in case
                row_header.label(text="No Camera", icon='ERROR')
        else:
            row_header.label(text="Viewport Cam", icon='VIEW3D')
            
        
        # Main lens area: vertical ortho button on left, controls on right
        row_main = col_lens.row(align=True)
        
        # Left column: big vertical ortho/persp toggle
        col_ortho = row_main.column(align=True)
        col_ortho.scale_x = 1.2
        if in_camera_mode:
            col_ortho.scale_y = 4.0  # Match height of focal row (2.0) + clip and shift rows (1.0)
        else:
            col_ortho.scale_y = 3.0  # Match height of ortho scale  row + clip row (1.0)
        persp_icon = 'VIEW_PERSPECTIVE' if props.is_perspective else 'VIEW_ORTHO'
        col_ortho.prop(props, "is_perspective", text="", icon=persp_icon, toggle=True)
        
        # Right column: focal length and clipping
        col_controls = row_main.column(align=True)
        
        # Focal Length / FOV
        if props.is_perspective:
            # Perspective
            split_focal = col_controls.split(factor=0.85, align=True)
            split_focal.scale_y = 2.0
            
            if props.use_fov:
                split_focal.prop(props, "field_of_view", text="Field of View")
            else:
                split_focal.prop(props, "focal_length", text="Focal Length")
            
            # Unit toggle
            text = "deg" if props.use_fov else "mm"
            split_focal.prop(props, "use_fov", text=text, toggle=True)
        else:
            # Ortho
            row_focal = col_controls.row(align=True)
            row_focal.scale_y = 2.0
            row_focal.prop(props, "focal_length", text="Ortho Scale")
            
        # Clipping
        row_clip = col_controls.row(align=True)
        row_clip.prop(props, "clip_start")
        # Shift X (only in camera mode with valid camera)
        if in_camera_mode and context.scene.camera:
            row_clip.prop(context.scene.camera.data, "shift_x", text="Shift X")
        else:
            row_clip.prop(props, "clip_end")
            
        # Camera Shift row 2 (only in camera mode)
        if in_camera_mode and context.scene.camera:
            row_clip = col_controls.row(align=True)
            row_clip.prop(props, "clip_end")
            row_clip.prop(context.scene.camera.data, "shift_y", text="Shift Y")
            
        # Zoom Slider - hide in camera ortho mode (ortho_scale handles it there)
        show_zoom = props.is_perspective
        if show_zoom:
            row = layout.row(align=True)
            zoom_label = "Zoom (Dolly)" if props.is_perspective else "Zoom (Scale)"
            row.prop(props, "zoom_level", text=zoom_label, icon='ZOOM_ALL')
            row.scale_y = 1.4
        
        layout.separator()
        
    # Transform
    if show_transform:
        split_main = layout.split(factor=0.5)
        
        # --- Location Section ---
        col_loc_outer = split_main.column(align=True)
        col_loc_outer.label(text="Location", icon='EMPTY_ARROWS')
        
        # Row containing [Toggle | Sliders]
        row_loc = col_loc_outer.row(align=True)
        
        # Left: Vertical Toggle Button (Screen Space)
        col_btn_loc = row_loc.column(align=True)
        col_btn_loc.scale_x = 1.2
        col_btn_loc.scale_y = 3 # Taller to match 3 rows of inputs
        icon_loc = 'ORIENTATION_VIEW' if props.use_screen_space else 'ORIENTATION_GLOBAL'
        col_btn_loc.prop(props, "use_screen_space", text="", icon=icon_loc, toggle=True)
        
        # Right: Sliders
        col_inputs_loc = row_loc.column(align=True)
        
        if props.use_screen_space:
            # Screen Space Inputs
            row = col_inputs_loc.row(align=True)
            row.prop(props, "screen_x", text="U")
            row.prop(props, "reset_screen_x", text="", icon='PANEL_CLOSE')
                
            row = col_inputs_loc.row(align=True)
            row.prop(props, "screen_z", text="V")
            row.prop(props, "reset_screen_z", text="", icon='PANEL_CLOSE')
            
            row = col_inputs_loc.row(align=True)
            row.prop(props, "screen_rotation", text="Roll")
            row.prop(props, "reset_screen_rotation", text="", icon='PANEL_CLOSE')
        else:
            # World Space Inputs
            row = col_inputs_loc.row(align=True)
            row.prop(props, "loc_x")
            row.prop(props, "reset_loc_x", text="", icon='PANEL_CLOSE')
            row = col_inputs_loc.row(align=True)
            row.prop(props, "loc_y")
            row.prop(props, "reset_loc_y", text="", icon='PANEL_CLOSE')
            row = col_inputs_loc.row(align=True)
            row.prop(props, "loc_z")
            row.prop(props, "reset_loc_z", text="", icon='PANEL_CLOSE')
        
        # Back Up to Wall button (under location section)
        col_inputs_loc.separator()
        row_backup = col_loc_outer.row(align=False)
        row_backup.operator("view3d.dolly_to_obstacle", text="Back Up to Wall", icon='TRACKING_BACKWARDS_SINGLE')
        
        # --- Rotation Section ---
        col_rot_outer = split_main.column(align=True)
        col_rot_outer.label(text="Rotation", icon='ORIENTATION_GIMBAL')
        
        # Row containing [Toggle | Sliders]
        row_rot = col_rot_outer.row(align=True)
        
        # Left: Vertical Toggle Button (Orbit)
        col_btn_rot = row_rot.column(align=True)
        col_btn_rot.scale_x = 1.2
        col_btn_rot.scale_y = 3 # Taller to match 3 rows
        
        if in_camera_mode:
            col_btn_rot.enabled = False
        
        col_btn_rot.prop(props, "orbit_around_selection", text="", icon='PIVOT_BOUNDBOX', toggle=True)
        
        # Right: Sliders
        col_inputs_rot = row_rot.column(align=True)
        
        if props.orbit_around_selection and not in_camera_mode:
            # Orbit Mode Inputs
            row = col_inputs_rot.row(align=True)
            row.prop(props, "orbit_pitch", text="Pitch")
            row.prop(props, "reset_orbit_pitch", text="", icon='PANEL_CLOSE')
            row = col_inputs_rot.row(align=True)
            row.prop(props, "orbit_yaw", text="Yaw")
            row.prop(props, "reset_orbit_yaw", text="", icon='PANEL_CLOSE')
            row = col_inputs_rot.row(align=True)
            row.prop(props, "screen_rotation", text="Roll")
            row.prop(props, "reset_screen_rotation", text="", icon='PANEL_CLOSE')
        else:
            # Standard Rotation Inputs
            row = col_inputs_rot.row(align=True)
            row.prop(props, "rot_x")
            row.prop(props, "reset_rot_x", text="", icon='PANEL_CLOSE')
            row = col_inputs_rot.row(align=True)
            row.prop(props, "rot_y")
            row.prop(props, "reset_rot_y", text="", icon='PANEL_CLOSE')
            row = col_inputs_rot.row(align=True)
            row.prop(props, "rot_z")
            row.prop(props, "reset_rot_z", text="", icon='PANEL_CLOSE')
    
    if show_history or show_saved_views:
        layout.separator()
            
    # Saved Views (includes Create Camera and Create View buttons)
    if show_saved_views:
        row = layout.row(align=True)
        row.label(text="Views", icon='HIDE_OFF')

        views_ui = layout.column(align=True)        
        split = views_ui.split(align=True, factor=0.76)
        split.scale_y = 3.0
        split.operator("view3d.save_current_view", text="+ Create View", icon='BOOKMARKS')
        split.operator("view3d.create_camera_from_view", text="", icon='OUTLINER_OB_CAMERA')
        
        # Use data_storage for view count
        from . import data_storage
        saved_views = data_storage.get_saved_views()
        has_views = len(saved_views) > 0
        
        # Check ghost state for update button
        current_idx = context.scene.saved_views_index
        ghost_idx = props.last_active_view_index
        has_ghost = current_idx == -1 and ghost_idx >= 0 and ghost_idx < len(saved_views)
        # Enable update if we have a selection OR ghost (view styles can change even if camera hasn't moved)
        can_update = current_idx >= 0 or has_ghost
        # Rename/Delete only when actually on a view (not ghost)
        can_modify = current_idx >= 0
        
        col_saved = views_ui.column(align=True)
        row_views = col_saved.row(align=True)
        
        # Prev/Next buttons - disabled if no views
        sub = row_views.row(align=True)
        sub.enabled = has_views
        sub.operator("view3d.prev_saved_view", text="", icon='BACK')
        sub.operator("view3d.next_saved_view", text="", icon='FORWARD')
        
        # Dropdown - always visible, disabled if no views
        sub = row_views.row(align=True)
        sub.enabled = has_views
        sub.prop(props, "saved_views_enum", text="")
        
        # Update button with ghost logic
        sub = row_views.row(align=True)
        sub.enabled = can_update
        sub.operator("view3d.update_saved_view", text="", icon='FILE_REFRESH')
        
        # Rename button - only when on a view
        sub = row_views.row(align=True)
        sub.enabled = can_modify
        sub.operator_context = 'INVOKE_DEFAULT'
        sub.operator("view3d.rename_saved_view", text="", icon='FONT_DATA')
        
        # Delete button - only when on a view
        sub = row_views.row(align=True)
        sub.enabled = can_modify
        sub.operator("view3d.delete_saved_view", text="", icon='X')
        
        # Reorder button
        sub = row_views.row(align=True)
        sub.enabled = has_views
        sub.operator("view3d.reorder_views", text="", icon='SORTSIZE')
        
        # Gallery button
        sub = row_views.row(align=True)
        sub.operator("view3d.thumbnail_gallery", text="", icon='RENDERLAYERS', depress=VIEW3D_OT_thumbnail_gallery._is_active)
        
        # Thumbnail Gallery with info panel
        if len(context.scene.saved_views) > 0:
            props = context.scene.viewpilot
            
            # Two-column layout: gallery on left, info on right
            # Use split to give gallery more room (65%) vs info column
            split = layout.split(factor=0.34)
            
            # Left column: thumbnail gallery with labels
            col_gallery = split.column()
            col_gallery.template_icon_view(props, "panel_gallery_enum", show_labels=True, scale=5.0)
            
            # Right column: selected view's Remember states
            col_remember = split.column()
            
            # Get selected view
            selected_idx = context.scene.saved_views_index
            ghost_idx = props.last_active_view_index
            
            # Determine which view to show (selected or ghost)
            display_idx = selected_idx if selected_idx >= 0 else ghost_idx
            is_ghost = selected_idx < 0 and ghost_idx >= 0
            
            if 0 <= display_idx < len(context.scene.saved_views):
                view = context.scene.saved_views[display_idx]
                
                # Remember states header
                col_remember.label(text="Remember:")
                col_remember.enabled = not is_ghost  # Disable in ghost mode
                
                col_states = col_remember.column(align=True)
                col_states.scale_y = 0.9
                col_states.enabled = not is_ghost  # Disable in ghost mode
                
                # Toggleable remember states
                col_states.prop(view, "remember_perspective", text="Perspective", toggle=True)
                col_states.prop(view, "remember_shading", text="Shading", toggle=True)
                col_states.prop(view, "remember_overlays", text="Overlays", toggle=True)
                col_states.prop(view, "remember_composition", text="Composition", toggle=True)
            else:
                col_remember.label(text="No View Selected")
                
            # Show composition info (Scene : ViewLayer) - use dynamic lookup if possible
            scene_view_info = layout.row()
            scene_view_info.enabled = not is_ghost  # Disable in ghost mode
            scene_view_info.separator()
            
            # Use display_idx to show ghost view's composition when in ghost mode
            if 0 <= display_idx < len(context.scene.saved_views):
                # Get view data from JSON storage (has UUID)
                from . import data_storage
                view_dict = data_storage.get_saved_view(display_idx)
                
                if view_dict:
                    stored_scene_id = view_dict.get('composition_scene_uuid', '')
                    stored_vl_id = view_dict.get('composition_view_layer_uuid', '')
                    stored_scene_name = view_dict.get('composition_scene', '')
                    stored_vl_name = view_dict.get('composition_view_layer', '')
                    
                    # Look up current scene by identity
                    found_scene = data_storage.find_scene_by_identity(stored_scene_id) if stored_scene_id else None
                    scene_name = found_scene.name if found_scene else (stored_scene_name or "—")
                    
                    # Look up current view layer by identity
                    target_scene = found_scene if found_scene else bpy.data.scenes.get(stored_scene_name)
                    found_vl = data_storage.find_view_layer_by_identity(stored_vl_id, target_scene) if stored_vl_id and target_scene else None
                    vl_name = found_vl.name if found_vl else (stored_vl_name or "—")
                    
                    scene_view_info.label(text=f"{scene_name} : {vl_name}", icon='SCENE_DATA')
            else:
                scene_view_info.label(text="—", icon='SCENE_DATA')
      
    # Overlays (Camera Mode Only)
    if show_viewport_display and in_camera_mode and context.scene.camera:
        cam_data = context.scene.camera.data
        layout.separator()
        col_display = layout.column(align=False)
        col_display.label(text="Overlays", icon='OVERLAY')
        col_display.separator()
        
        # Passepartout row
        row_passepartout = col_display.split(factor=0.33, align=True)
        row_passepartout.scale_y = 1.2
        row_passepartout.prop(cam_data, "show_passepartout", text="Passepartout", toggle=False)
        row_passepartout.prop(cam_data, "passepartout_alpha", text="")
        
        col_display.separator()
        
        # Composition Guides row
        col_guides = col_display.column(align=True)
        col_guides.scale_y = 1.2
        row_guides = col_guides.row(align=True)
        row_guides.prop(cam_data, "show_composition_thirds", text="Thirds", icon='MESH_GRID', toggle=True)
        row_guides.prop(cam_data, "show_composition_center", text="Center", icon='ADD', toggle=True)
        row_guides.prop(cam_data, "show_composition_center_diagonal", text="Diagonal", icon='X', toggle=True)
        row_guides = col_guides.row(align=True)
        row_guides.prop(cam_data, "composition_guide_color", text="")

# =============================================================================
# HEADER POPOVER PANEL
# =============================================================================

class VIEW3D_PT_viewpilot(bpy.types.Panel):
    """Popover panel for ViewPilot controls in 3D viewport header."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "ViewPilot"
    bl_ui_units_x = 14  # Width of the popover
    
    def draw(self, context):
        layout = self.layout
        layout.label(text="ViewPilot")
        draw_viewpilot_controls(self.layout, context, location='header')

# =============================================================================
# N-PANEL
# =============================================================================

class VIEW3D_PT_viewpilot_npanel(bpy.types.Panel):
    """N-Panel for ViewPilot controls."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "ViewPilot"  # Tab name in N-panel
    bl_label = "ViewPilot"
    
    @classmethod
    def poll(cls, context):
        """Only show if enabled in preferences."""
        try:
            return get_preferences().show_n_panel
        except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
            return False
    
    def draw(self, context):
        draw_viewpilot_controls(self.layout, context, location='npanel')

# =============================================================================
# HEADER DRAW FUNCTION
# =============================================================================

def draw_header_button(self, context):
    """Draw the ViewPilot popover button in the 3D Viewport header."""
    if context.space_data.type != 'VIEW_3D':
        return
    
    try:
        prefs = get_preferences()
        if not prefs.show_header_button:
            return
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
        pass
    
    self.layout.popover(
        panel="VIEW3D_PT_viewpilot",
        text="",
        icon='VIEW_CAMERA'
    )

# =============================================================================
# TOPBAR DRAW FUNCTION (near Scene/View Layer)
# =============================================================================

def draw_topbar_saved_views(self, context):
    """Draw saved views dropdown and save button in the TOPBAR."""
    # Guard: Only draw once - TOPBAR draw is called for multiple regions
    # We only want to draw in the RIGHT region (where Scene/ViewLayer are)
    if hasattr(context, 'region') and context.region:
        if context.region.alignment != 'RIGHT':
            return
    
    try:
        prefs = get_preferences()
        if not prefs.show_topbar_saved_views:
            return
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
        pass
    
    # Ensure at least one resolvable 3D view exists for these actions.
    _, space, region = utils.find_view3d_context(context)
    if not space or not region:
        return  # No 3D viewport found
    
    layout = self.layout
    props = context.scene.viewpilot
    
    # Use data_storage for view count
    from . import data_storage
    saved_views = data_storage.get_saved_views()
    has_views = len(saved_views) > 0
    current_idx = context.scene.saved_views_index
    ghost_idx = props.last_active_view_index
    
    # Update button should be enabled when:
    # 1. We have a direct selection (current_idx >= 0) - means we're on a view (could be modified)
    # 2. OR we're in ghost mode (current_idx == -1 but ghost_idx >= 0) - modified from a view
    # For now, enable if ghost mode OR direct selection (user modifies while on view)
    has_ghost = current_idx == -1 and ghost_idx >= 0 and ghost_idx < len(saved_views)
    can_update = has_ghost  # Only enable if we've modified away from a saved view
    can_modify = current_idx >= 0  # Rename/Delete only when on a view (not ghost)
    
    row = layout.row(align=True)
    row.separator()
    
    # Add button (always enabled if 3D view exists) - leftmost
    row_add = row.row(align=True)
    row_add.operator("view3d.save_current_view", text="", icon='ADD')
    row_add.scale_x = 2
    
    # Prev/Next buttons
    sub = row.row(align=True)
    sub.enabled = has_views
    sub.operator("view3d.prev_saved_view", text="", icon='BACK')
    sub.operator("view3d.next_saved_view", text="", icon='FORWARD')
    
    # Dropdown - always show (but greyed if no views), with minimum width
    sub = row.row(align=True)
    sub.enabled = has_views
    sub.ui_units_x = 6  # Fixed width for the dropdown
    sub.prop(props, "saved_views_enum", text="", icon='NONE')
    
    # Update button - enabled when modified from a saved view (ghost mode)
    sub = row.row(align=True)
    sub.enabled = can_update
    sub.operator("view3d.update_saved_view", text="", icon='FILE_REFRESH')
    
    # Rename button - only when on a view (not ghost)
    sub = row.row(align=True)
    sub.enabled = can_modify
    sub.operator_context = 'INVOKE_DEFAULT'
    sub.operator("view3d.rename_saved_view", text="", icon='FONT_DATA')
    
    # Delete button - only when on a view (not ghost)
    sub = row.row(align=True)
    sub.enabled = can_modify
    sub.operator("view3d.delete_saved_view", text="", icon='X')
    
    # Reorder button
    sub = row.row(align=True)
    sub.enabled = has_views
    sub.scale_x = 1
    sub.operator("view3d.reorder_views", text="", icon='SORTSIZE')
    
    # Gallery button - Always enabled so we can create first view from gallery
    sub = row.row(align=True)
    sub.scale_x = 1.4
    sub.operator("view3d.thumbnail_gallery", text="", icon='RENDERLAYERS', depress=VIEW3D_OT_thumbnail_gallery._is_active)

# =============================================================================
# REGISTRATION
# =============================================================================

classes = [
    VIEW3D_PT_viewpilot,
    VIEW3D_PT_viewpilot_npanel,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.VIEW3D_HT_header.append(draw_header_button)
    bpy.types.TOPBAR_HT_upper_bar.prepend(draw_topbar_saved_views)


def unregister():
    bpy.types.TOPBAR_HT_upper_bar.remove(draw_topbar_saved_views)
    bpy.types.VIEW3D_HT_header.remove(draw_header_button)
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
