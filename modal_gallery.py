"""
Modal Gallery for ViewPilot Saved Views Thumbnails.

Displays a filmstrip overlay of saved view thumbnails at the bottom of the 3D viewport.
Click a thumbnail to navigate to that view. Right click on them for relevant functions,
like delete, rename, update or "Remember" toggles.
"""

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, Quaternion
from .temp_paths import make_temp_png_path
from . import debug_tools, utils

# Module-level backup of draw handler - survives class reload
_backup_draw_handler = None


class VIEW3D_OT_thumbnail_gallery(bpy.types.Operator):
    """Show saved views as a thumbnail filmstrip overlay"""
    bl_idname = "view3d.thumbnail_gallery"
    bl_label = "Viewport Gallery"
    bl_options = {'REGISTER'}
    
    # Gallery settings (defaults, overridden by preferences)
    THUMB_SIZE_MAX_DEFAULT = 100
    THUMB_SIZE_MIN = 64 # If screen is full even with this, scroll indicators with counters are shown
    THUMB_PADDING = 8 # Padding between thumbnails
    STRIP_MARGIN = 0  # Bottom margin (when gallery at bottom)
    TOP_MARGIN = 0   # Top margin (when gallery at top)
    
    @classmethod
    def _get_thumb_size_max(cls):
        """Get thumbnail max size from preferences, with fallback."""
        try:
            from .preferences import get_preferences
            return get_preferences().thumbnail_size_max
        except Exception:
            return cls.THUMB_SIZE_MAX_DEFAULT
    
    _draw_handler = None
    _textures = {}
    _is_active = False  # Class-level flag to prevent multiple instances
    _instance = None  # Reference to active instance for external refresh
    _needs_refresh = False  # Flag set by external code to trigger refresh
    _context_menu_index = -1  # Index of thumbnail that was right-clicked
    _primary_area = None  # The 3D view area where gallery displays (only one)
    _primary_region = None  # The WINDOW region of the primary area (for coordinate conversion)
    _context_area = None  # The last in-focus 3D view for create/update context
    
    def _get_mouse_region_coords(self, event):
        """Convert global mouse coordinates to primary region local coordinates.
        
        This ensures consistent hit detection regardless of which area/region
        the modal event originates from.
        """
        primary_region = VIEW3D_OT_thumbnail_gallery._primary_region
        if not primary_region:
            # Fallback to event's region coords
            return event.mouse_region_x, event.mouse_region_y
        
        # Convert global mouse to primary region's local coords
        mx = event.mouse_x - primary_region.x
        my = event.mouse_y - primary_region.y
        return mx, my
    
    @classmethod
    def poll(cls, context):
        # Allow enabling from any context (TopBar/Properties) as long as we have a 3D view
        _, space, region = utils.find_view3d_context(context)
        return bool(space and region)
    
    def invoke(self, context, event):
        # If already active, toggle OFF
        if VIEW3D_OT_thumbnail_gallery._is_active:
            if VIEW3D_OT_thumbnail_gallery._instance:
                # Toggle OFF: cleanup and close
                VIEW3D_OT_thumbnail_gallery._instance._cleanup(context)
            return {'CANCELLED'}
        
        # If not in a VIEW_3D area, re-invoke with proper context
        if not context.area or context.area.type != 'VIEW_3D':
            preferred_area = (
                VIEW3D_OT_thumbnail_gallery._context_area
                or VIEW3D_OT_thumbnail_gallery._primary_area
            )
            area, _, region = utils.find_view3d_override_context(
                context, preferred_area=preferred_area
            )
            window = utils.find_window_for_area(context, area)
            if area and region and window:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.view3d.thumbnail_gallery('INVOKE_DEFAULT')
                return {'CANCELLED'}  # This invoke is done, the re-invoked one takes over
            # No 3D view found.
            return {'CANCELLED'}
        
        VIEW3D_OT_thumbnail_gallery._is_active = True
        VIEW3D_OT_thumbnail_gallery._instance = self
        VIEW3D_OT_thumbnail_gallery._primary_area = context.area  # Track which area shows gallery
        # Store the WINDOW region for coordinate conversion
        for r in context.area.regions:
            if r.type == 'WINDOW':
                VIEW3D_OT_thumbnail_gallery._primary_region = r
                break
        VIEW3D_OT_thumbnail_gallery._context_area = context.area  # Initial context for operations
        
        # Initialize state
        self._hover_index = -1
        self._plus_hover = False
        self._scroll_offset = 0
        self._thumb_size = self._get_thumb_size_max()
        self._plus_btn_rect = None
        self._refresh_btn_rect = None
        self._reorder_btn_rect = None
        self._close_btn_rect = None
        self._refresh_hover = False
        self._reorder_hover = False
        self._close_hover = False
        self._flip_to_top = False  # User preference to flip gallery to top
        self._preview_index = -1  # Index of thumbnail being previewed with MMB
        self._layout_cache = None
        self._layout_cache_key = None
        self._geom_cache = {}
        self._text_dim_cache = {}
        self._display_image_names = set()
        self._shader_uniform = gpu.shader.from_builtin('UNIFORM_COLOR')
        self._shader_image = gpu.shader.from_builtin('IMAGE')
        
        # Load textures for all thumbnails
        self._load_textures(context)
        
        # Add draw handler - Pass NO args, let draw function use bpy.context
        # This ensures we always get the fresh draw-time context
        self._draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_gallery, (), 'WINDOW', 'POST_PIXEL'
        )
        
        # Also save to module-level backup (survives class reload)
        global _backup_draw_handler
        _backup_draw_handler = self._draw_handler
        
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}
    
    @classmethod
    def request_refresh(cls):
        """Request texture refresh - called by save/update/delete operators."""
        if cls._is_active and cls._instance:
            # Set flag to trigger refresh in the modal loop (thread-safe, correct context)
            cls._needs_refresh = True
            if cls._primary_area:
                cls._primary_area.tag_redraw()
            return

    def _invalidate_layout_cache(self, clear_text_cache=False):
        """Invalidate cached gallery layout and geometry."""
        self._layout_cache = None
        self._layout_cache_key = None
        self._geom_cache.clear()
        if clear_text_cache:
            self._text_dim_cache.clear()

    def _clear_gpu_textures(self):
        """Release GPU texture objects held by the gallery instance."""
        for tex in list(self._textures.values()):
            free_fn = getattr(tex, "free", None)
            if callable(free_fn):
                try:
                    free_fn()
                except Exception:
                    pass
        self._textures.clear()

    def _clear_display_images(self):
        """Remove temporary display images created for Blender 4.x preview path."""
        for name in list(self._display_image_names):
            img = bpy.data.images.get(name)
            if img:
                try:
                    bpy.data.images.remove(img)
                except Exception:
                    pass
        self._display_image_names.clear()

    def _batch_key(self, kind, x, y, width, height):
        return (kind, int(round(x)), int(round(y)), int(round(width)), int(round(height)))

    def _get_rect_batch(self, kind, x, y, width, height):
        """Return cached GPU batch for common rectangle primitives."""
        key = self._batch_key(kind, x, y, width, height)
        batch = self._geom_cache.get(key)
        if batch is not None:
            return batch

        if kind == 'TRIS':
            verts = (
                (x, y), (x + width, y),
                (x + width, y + height), (x, y + height),
            )
            batch = batch_for_shader(
                self._shader_uniform, 'TRIS', {"pos": verts},
                indices=((0, 1, 2), (2, 3, 0))
            )
        elif kind == 'LINE':
            verts = (
                (x, y), (x + width, y),
                (x + width, y + height), (x, y + height), (x, y),
            )
            batch = batch_for_shader(self._shader_uniform, 'LINE_STRIP', {"pos": verts})
        elif kind == 'IMAGE':
            verts = (
                (x, y), (x + width, y),
                (x + width, y + height), (x, y + height),
            )
            uvs = ((0, 0), (1, 0), (1, 1), (0, 1))
            batch = batch_for_shader(
                self._shader_image, 'TRIS',
                {"pos": verts, "texCoord": uvs},
                indices=((0, 1, 2), (2, 3, 0))
            )
        else:
            return None

        self._geom_cache[key] = batch
        return batch

    def _get_dashed_border_batch(self, x, y, width, height):
        """Return cached batch for dashed borders."""
        import math

        key = self._batch_key('DASHED', x, y, width, height)
        batch = self._geom_cache.get(key)
        if batch is not None:
            return batch

        vertices = []
        dash_len = 10
        gap_len = 6

        def add_dashed_line(x1, y1, x2, y2):
            dx = x2 - x1
            dy = y2 - y1
            dist = math.sqrt(dx * dx + dy * dy)
            if dist == 0:
                return

            steps = int(dist / (dash_len + gap_len))
            ux = dx / dist
            uy = dy / dist

            for i in range(steps + 1):
                start_dist = i * (dash_len + gap_len)
                if start_dist >= dist:
                    break
                end_dist = min(dist, start_dist + dash_len)
                vertices.append((x1 + ux * start_dist, y1 + uy * start_dist))
                vertices.append((x1 + ux * end_dist, y1 + uy * end_dist))

        add_dashed_line(x, y + height, x + width, y + height)      # Top
        add_dashed_line(x, y, x + width, y)                        # Bottom
        add_dashed_line(x, y, x, y + height)                       # Left
        add_dashed_line(x + width, y, x + width, y + height)       # Right

        batch = batch_for_shader(self._shader_uniform, 'LINES', {"pos": vertices})
        self._geom_cache[key] = batch
        return batch

    def _get_text_dimensions(self, font_id, font_size, text):
        """Return cached BLF text dimensions."""
        key = (font_id, int(font_size), text)
        dims = self._text_dim_cache.get(key)
        if dims is not None:
            return dims
        blf.size(font_id, int(font_size))
        dims = blf.dimensions(font_id, text)
        self._text_dim_cache[key] = dims
        return dims
    
    def modal(self, context, event):
        # Check if we should stop (toggled off externally or by addon reload)
        if not VIEW3D_OT_thumbnail_gallery._is_active:
            self._cleanup(context)  # Ensure draw handler is removed
            return {'CANCELLED'}
        
        # Check if primary area is still valid, promote if needed
        if not self._is_primary_area_valid():
            self._promote_new_primary_area(context)
            return {'CANCELLED'}  # End this modal, new one started in promoted area
        
        # Track last in-focus 3D view using GLOBAL mouse position
        # When over primary area (gallery), clear context_area so "+" uses gallery's view
        # When over other 3D views, track them as context_area for "+" button
        mouse_x, mouse_y = event.mouse_x, event.mouse_y
        primary_area = VIEW3D_OT_thumbnail_gallery._primary_area
        
        # Check if mouse is over primary area - clear context_area
        if primary_area:
            if (primary_area.x <= mouse_x < primary_area.x + primary_area.width and
                primary_area.y <= mouse_y < primary_area.y + primary_area.height):
                VIEW3D_OT_thumbnail_gallery._context_area = None
            else:
                # Check if mouse is over any other 3D view
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'VIEW_3D' and area != primary_area:
                            if (area.x <= mouse_x < area.x + area.width and
                                area.y <= mouse_y < area.y + area.height):
                                VIEW3D_OT_thumbnail_gallery._context_area = area
                                break

        # Track hover state on mouse movement
        if event.type == 'MOUSEMOVE':
            mx, my = self._get_mouse_region_coords(event)
            
            # If in preview mode (MMB held), update preview on thumbnail hover
            if self._preview_index >= 0:
                new_thumb = self._get_clicked_thumbnail(context, event)
                if new_thumb is not None and new_thumb in self._textures:
                    if new_thumb != self._preview_index:
                        self._preview_index = new_thumb
                        context.area.tag_redraw()
                # Keep current preview if dragging over non-thumbnail area
                return {'RUNNING_MODAL'}
            
            # Check thumbnail hover
            new_hover = self._get_clicked_thumbnail(context, event)
            new_hover_idx = new_hover if new_hover is not None else -1
            if new_hover_idx != self._hover_index:
                self._hover_index = new_hover_idx
                context.area.tag_redraw()
            
            # Check plus button hover
            if self._plus_btn_rect:
                rx, ry, rw, rh = self._plus_btn_rect
                is_hover_plus = (rx <= mx <= rx + rw and ry <= my <= ry + rh)
                if is_hover_plus != self._plus_hover:
                    self._plus_hover = is_hover_plus
                    context.area.tag_redraw()
            
            # Check refresh button hover
            if self._refresh_btn_rect:
                rx, ry, rw, rh = self._refresh_btn_rect
                is_hover = (rx <= mx <= rx + rw and ry <= my <= ry + rh)
                if is_hover != self._refresh_hover:
                    self._refresh_hover = is_hover
                    context.area.tag_redraw()
            
            # Check reorder button hover
            if self._reorder_btn_rect:
                rx, ry, rw, rh = self._reorder_btn_rect
                is_hover = (rx <= mx <= rx + rw and ry <= my <= ry + rh)
                if is_hover != self._reorder_hover:
                    self._reorder_hover = is_hover
                    context.area.tag_redraw()
            
            # Check close button hover
            if self._close_btn_rect:
                rx, ry, rw, rh = self._close_btn_rect
                is_hover = (rx <= mx <= rx + rw and ry <= my <= ry + rh)
                if is_hover != self._close_hover:
                    self._close_hover = is_hover
                    context.area.tag_redraw()
                    
            return {'PASS_THROUGH'}
        
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            mx, my = self._get_mouse_region_coords(event)
            
            # Check Refresh Button
            if self._refresh_btn_rect:
                rx, ry, rw, rh = self._refresh_btn_rect
                if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                    # Refresh all thumbnails
                    self._regenerate_all_thumbnails(context)
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            
            # Check Reorder Button
            if self._reorder_btn_rect:
                rx, ry, rw, rh = self._reorder_btn_rect
                if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                    # Only open reorder if we have at least 2 views
                    from . import data_storage
                    if len(data_storage.get_saved_views()) >= 2:
                        bpy.ops.view3d.reorder_views('INVOKE_DEFAULT')
                    else:
                        self.report({'INFO'}, "Not enough views to reorder")
                    return {'RUNNING_MODAL'}
            
            # Check Close Button
            if self._close_btn_rect:
                rx, ry, rw, rh = self._close_btn_rect
                if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                    # Close gallery
                    self._cleanup(context)
                    return {'CANCELLED'}
            
            # Check Plus Button (Add)
            if self._plus_btn_rect:
                rx, ry, rw, rh = self._plus_btn_rect
                if rx <= mx <= rx + rw and ry <= my <= ry + rh:
                    # Add new view - use context_area if set (tracks the non-gallery 3D view)
                    # Fall back to primary_area (gallery's view) if context_area is None
                    ctx_area = VIEW3D_OT_thumbnail_gallery._context_area
                    if not ctx_area:
                        ctx_area = VIEW3D_OT_thumbnail_gallery._primary_area
                    
                    area, _, region = utils.find_view3d_override_context(
                        context, preferred_area=ctx_area
                    )
                    window = utils.find_window_for_area(context, area)
                    if area and region and window:
                        with bpy.context.temp_override(window=window, area=area, region=region):
                            try:
                                bpy.ops.view3d.save_current_view()
                            except RuntimeError as error:
                                # Save operator may intentionally cancel (e.g. storage invalid).
                                # Keep gallery modal loop alive without dumping traceback noise.
                                print(f"[ViewPilot] Save from gallery cancelled: {error}")
                    return {'RUNNING_MODAL'}
            
            # Check Thumbnail Click
            clicked_index = self._get_clicked_thumbnail(context, event)
            if clicked_index is not None:
                # Navigate to that view
                context.scene.viewpilot.saved_views_enum = str(clicked_index)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}
                
            # Click outside gallery - pass through
            return {'PASS_THROUGH'}
        
        elif event.type == 'RIGHTMOUSE' and event.value == 'PRESS':
            # Check if right-clicking on a thumbnail
            clicked_index = self._get_clicked_thumbnail(context, event)
            if clicked_index is not None:
                # Store index for context menu operators
                VIEW3D_OT_thumbnail_gallery._context_menu_index = clicked_index
                # Invoke context menu
                bpy.ops.wm.call_menu(name="VIEW3D_MT_gallery_context")
                return {'RUNNING_MODAL'}
            # Right-click outside thumbnails - pass through
            return {'PASS_THROUGH'}
        
        elif event.type == 'ESC' and event.value == 'PRESS':
            # Only close if mouse is over the gallery area
            if self._is_mouse_over_gallery(context, event):
                self._cleanup(context)
                return {'CANCELLED'}
            return {'PASS_THROUGH'}
        
        # Middle mouse button for enlarged preview
        elif event.type == 'MIDDLEMOUSE':
            if event.value == 'PRESS':
                clicked_index = self._get_clicked_thumbnail(context, event)
                if clicked_index is not None and clicked_index in self._textures:
                    self._preview_index = clicked_index
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            elif event.value == 'RELEASE':
                if self._preview_index >= 0:
                    self._preview_index = -1
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        
        # Mouse wheel scrolling (only when scrolling is needed)
        elif event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            if self._is_mouse_over_gallery(context, event):
                from . import data_storage
                num_views = len(data_storage.get_saved_views())
                visible_count = self._get_visible_count(context)
                max_offset = max(0, num_views - visible_count)
                
                if max_offset > 0:  # Only scroll if needed
                    if event.type == 'WHEELUPMOUSE':
                        self._scroll_offset = max(0, self._scroll_offset - 1)
                    else:
                        self._scroll_offset = min(max_offset, self._scroll_offset + 1)
                    self._invalidate_layout_cache()
                    context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        
        # Check for refresh request from external operators
        if VIEW3D_OT_thumbnail_gallery._needs_refresh:
            VIEW3D_OT_thumbnail_gallery._needs_refresh = False
            self._load_textures(context)
            context.area.tag_redraw()
        
        return {'PASS_THROUGH'}
    
    def _is_primary_area_valid(self):
        """Check if the primary area still exists in any window."""
        primary = VIEW3D_OT_thumbnail_gallery._primary_area
        if not primary:
            return False
        if primary.type != 'VIEW_3D':
            return False
        return utils.find_window_for_area(bpy.context, primary) is not None
    
    def _promote_new_primary_area(self, context):
        """Promote the next available 3D view to primary and restart modal."""
        preferred_area = VIEW3D_OT_thumbnail_gallery._context_area
        area, _, region = utils.find_view3d_override_context(
            context, preferred_area=preferred_area
        )
        window = utils.find_window_for_area(context, area)
        if area and region and window:
            VIEW3D_OT_thumbnail_gallery._primary_area = area
            VIEW3D_OT_thumbnail_gallery._primary_region = region
            # Restart modal in new context to fix event handling
            VIEW3D_OT_thumbnail_gallery._is_active = False
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.view3d.thumbnail_gallery('INVOKE_DEFAULT')
            return
        # No 3D views left, cleanup
        self._cleanup(context)
    
    def _cleanup(self, context):
        global _backup_draw_handler
        VIEW3D_OT_thumbnail_gallery._is_active = False
        VIEW3D_OT_thumbnail_gallery._instance = None
        VIEW3D_OT_thumbnail_gallery._needs_refresh = False
        VIEW3D_OT_thumbnail_gallery._primary_area = None
        VIEW3D_OT_thumbnail_gallery._primary_region = None
        VIEW3D_OT_thumbnail_gallery._context_area = None
        if self._draw_handler:
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handler, 'WINDOW')
            self._draw_handler = None
        _backup_draw_handler = None  # Clear module-level backup
        self._clear_gpu_textures()
        self._clear_display_images()
        self._invalidate_layout_cache(clear_text_cache=True)
        self._shader_uniform = None
        self._shader_image = None
        
        # Redraw all 3D views.
        wm = getattr(context, "window_manager", None) or getattr(bpy.context, "window_manager", None)
        if wm:
            for window in wm.windows:
                screen = window.screen
                if not screen:
                    continue
                for area in screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
    
    def _regenerate_all_thumbnails(self, context):
        """Regenerate thumbnails for all saved views by navigating to each and capturing."""
        try:
            from .thumbnail_generator import generate_thumbnail
            from .state_controller import get_controller, UpdateSource, LockPriority
            from .preview_manager import reload_all_previews
            from . import data_storage
            from types import SimpleNamespace
            
            views = data_storage.get_saved_views()
            if not views:
                print("[ViewPilot] No saved views to regenerate")
                return
            
            # Track which views were updated for batched save
            updated_any = False
            
            # Get the actual viewport state (not UI properties which have callbacks)
            space = context.space_data
            region = space.region_3d if space else None
            if not region:
                print("[ViewPilot] No region_3d found")
                return
            
            # Store actual viewport state
            original_location = region.view_location.copy()
            original_rotation = region.view_rotation.copy()
            original_distance = region.view_distance
            original_perspective = region.view_perspective
            original_is_perspective = region.is_perspective
            original_lens = space.lens
            original_index = context.scene.saved_views_index
            original_world = context.scene.world  # Store World to prevent it from changing
            original_scene = context.window.scene  # Store original scene for restoration
            original_view_layer = context.window.view_layer  # Store original view layer
            
            # Begin a critical update transaction to block other updates
            controller = get_controller()
            if not controller.begin_update(UpdateSource.VIEW_RESTORE, LockPriority.CRITICAL):
                print("[ViewPilot] Could not acquire lock for thumbnail regeneration")
                return
            
            # Start a long grace period to prevent history recording during thumbnail regen
            # This ensures programmatic view changes don't bloat the history
            controller.start_grace_period(60.0, UpdateSource.VIEW_RESTORE)
            
            try:
                for i, view_dict in enumerate(views):
                    # Navigate to this view (skip enum load to avoid callbacks)
                    controller.skip_enum_load = True
                    context.scene.viewpilot.saved_views_enum = str(i)
                    controller.skip_enum_load = False
                    
                    # Switch to the view's scene if different
                    # Try UUID first, fall back to name
                    target_scene = None
                    scene_uuid = view_dict.get("composition_scene_uuid", "")
                    composition_scene = view_dict.get("composition_scene", "")
                    
                    if scene_uuid:
                        target_scene = data_storage.find_scene_by_uuid(scene_uuid)
                    if not target_scene and composition_scene and composition_scene in bpy.data.scenes:
                        target_scene = bpy.data.scenes[composition_scene]
                    
                    if target_scene and context.window.scene != target_scene:
                        context.window.scene = target_scene
                        # Need to get new region reference after scene switch
                        space = context.space_data
                        region = space.region_3d if space else None
                        if not region:
                            print(f"[ViewPilot] Lost region_3d after scene switch for view {i}")
                            continue
                    
                    # Switch to the view's view layer if different
                    # Try UUID first, fall back to name
                    target_vl = None
                    vl_uuid = view_dict.get("composition_view_layer_uuid", "")
                    composition_view_layer = view_dict.get("composition_view_layer", "")
                    current_scene = context.window.scene
                    
                    if vl_uuid:
                        target_vl = data_storage.find_view_layer_by_uuid(vl_uuid, current_scene)
                    if not target_vl and composition_view_layer:
                        if composition_view_layer in [vl.name for vl in current_scene.view_layers]:
                            target_vl = current_scene.view_layers[composition_view_layer]
                    
                    if target_vl and context.window.view_layer != target_vl:
                        context.window.view_layer = target_vl
                    
                    # Apply view state directly
                    rotation = view_dict.get("rotation", [1.0, 0.0, 0.0, 0.0])
                    region.view_location = Vector(view_dict.get("location", [0, 0, 0]))
                    region.view_rotation = Quaternion((rotation[0], rotation[1], rotation[2], rotation[3]))
                    region.view_distance = view_dict.get("distance", 10.0)
                    if view_dict.get("is_perspective", True):
                        region.view_perspective = 'PERSP'
                    else:
                        region.view_perspective = 'ORTHO'
                    space.lens = view_dict.get("lens", 50.0)
                    
                    # Create SimpleNamespace for thumbnail generator
                    temp_view = SimpleNamespace(**view_dict)
                    temp_view.location = tuple(view_dict.get("location", [0, 0, 0]))
                    temp_view.rotation = tuple(rotation)
                    
                    # Generate thumbnail for current viewport state
                    image_name = generate_thumbnail(context, temp_view, refresh_preview=False)
                    if image_name:
                        # Update in-memory only (no disk I/O yet)
                        view_dict["thumbnail_image"] = image_name
                        updated_any = True
                
                # Batch save: single JSON write + single sync to all scenes
                if updated_any:
                    data = data_storage.load_data()
                    data["saved_views"] = views
                    data_storage.save_data(data)
                    data_storage.sync_to_all_scenes()
                    reload_all_previews(context)
                
            finally:
                # Always restore state, even on exceptions
                # Get fresh references in case they changed during loop
                space = context.space_data
                region = space.region_3d if space else None
                
                # Restore original scene first (may affect region reference)
                try:
                    if context.window.scene != original_scene:
                        context.window.scene = original_scene
                        # Refresh region reference after scene switch
                        space = context.space_data
                        region = space.region_3d if space else None
                except Exception as restore_err:
                    print(f"[ViewPilot] Error restoring scene: {restore_err}")
                
                # Restore viewport state
                if region:
                    try:
                        region.view_location = original_location
                        region.view_rotation = original_rotation
                        region.view_distance = original_distance
                        if original_perspective == 'CAMERA':
                            region.view_perspective = 'CAMERA'
                        elif original_is_perspective:
                            region.view_perspective = 'PERSP'
                        else:
                            region.view_perspective = 'ORTHO'
                    except Exception as restore_err:
                        print(f"[ViewPilot] Error restoring viewport state: {restore_err}")
                
                if space:
                    try:
                        space.lens = original_lens
                    except Exception as restore_err:
                        print(f"[ViewPilot] Error restoring lens: {restore_err}")
                
                # Restore original view layer
                try:
                    if original_view_layer and original_view_layer.name in [vl.name for vl in context.window.scene.view_layers]:
                        if context.window.view_layer != original_view_layer:
                            context.window.view_layer = original_view_layer
                except Exception as restore_err:
                    print(f"[ViewPilot] Error restoring view layer: {restore_err}")
                
                # Restore saved views index and world
                try:
                    context.scene.saved_views_index = original_index
                    context.scene.world = original_world
                except Exception as restore_err:
                    print(f"[ViewPilot] Error restoring index/world: {restore_err}")
                
                # Reset enum property to match the index
                try:
                    controller.skip_enum_load = True
                    context.scene.viewpilot.saved_views_enum = str(original_index)
                except Exception:
                    pass
                finally:
                    controller.skip_enum_load = False
                
                # Clear the grace period so history recording resumes immediately
                controller.start_grace_period(0.0)
                
                # Always release the lock
                controller.end_update()
            
            # Sync UI properties to restored state (after lock released)
            context.scene.viewpilot.reinitialize_from_context(context)
            
            # Reload textures after regeneration
            self._load_textures(context)
            print(f"[ViewPilot] Regenerated {len(views)} thumbnails")
        except Exception as e:
            import traceback
            print(f"[ViewPilot] Error regenerating thumbnails: {e}")
            traceback.print_exc()
    
    def _load_textures(self, context):
        """Load GPU textures for all saved view thumbnails.
        
        Uses version-based approach:
        - Blender 5.0+: Direct gpu.texture.from_image() works correctly with Non-Color
        - Blender 4.x: Use save_render() workaround to fix washed-out colors
        """
        from . import data_storage
        self._clear_gpu_textures()
        self._invalidate_layout_cache()
        
        # Check Blender version - 5.0+ handles Non-Color correctly in GPU textures
        use_direct_method = bpy.app.version >= (5, 0, 0)
        
        # Clean up previously generated display images before rebuilding textures.
        self._clear_display_images()
        
        views = data_storage.get_saved_views()
        for i, view_dict in enumerate(views):
            thumb_name = view_dict.get("thumbnail_image", "")
            if thumb_name:
                img = bpy.data.images.get(thumb_name)
                if img:
                    try:
                        if use_direct_method:
                            # Blender 5.0+: Direct texture creation works correctly
                            texture = gpu.texture.from_image(img)
                            self._textures[i] = texture
                        else:
                            # Blender 4.x: Use save_render() to apply display transform
                            # This fixes washed-out colors from Non-Color images
                            import os

                            temp_path = make_temp_png_path("vp_gallery_", thumb_name)
                            img.save_render(temp_path)
                            
                            # Load the color-corrected image
                            display_img_name = f".VP_Display_{i}"
                            display_img = bpy.data.images.get(display_img_name)
                            if display_img:
                                display_img.filepath = temp_path
                                display_img.reload()
                            else:
                                display_img = bpy.data.images.load(temp_path, check_existing=False)
                                display_img.name = display_img_name
                            self._display_image_names.add(display_img_name)
                            
                            texture = gpu.texture.from_image(display_img)
                            self._textures[i] = texture
                            
                            # Clean up temp file
                            try:
                                os.remove(temp_path)
                            except Exception:
                                pass
                            
                    except Exception as e:
                        print(f"[ViewPilot] Failed to load texture for {view_dict.get('name', 'View')}: {e}")
                else:
                    print(f"[ViewPilot] Image not found: {thumb_name}")
    
    def _calculate_thumb_size(self, context, num_views):
        """Calculate optimal thumbnail size to fit all views + buttons, respecting min/max."""
        # We only have 1 button (+ for Add)
        total_items = num_views + 1
        
        region = VIEW3D_OT_thumbnail_gallery._primary_region or context.region
        if region is None:
            return self._get_thumb_size_max()
            
        available_width = region.width - self.STRIP_MARGIN * 2
        
        try:
            size = int((available_width - self.THUMB_PADDING * (total_items + 1)) / total_items)
        except ZeroDivisionError:
            size = self._get_thumb_size_max()
            
        return max(self.THUMB_SIZE_MIN, min(self._get_thumb_size_max(), size))
    
    def _get_visible_count(self, context):
        """Calculate how many thumbnails fit in the viewport minus buttons."""
        region = VIEW3D_OT_thumbnail_gallery._primary_region or context.region
        if region is None:
            return 0
            
        # Reserve space for 1 button
        button_space = 1 * (self._thumb_size + self.THUMB_PADDING)
        available_for_thumbs = region.width - self.STRIP_MARGIN * 2 - button_space
        
        # Ensure we don't have negative space
        if available_for_thumbs <= 0:
            return 0
            
        return max(1, int(available_for_thumbs / (self._thumb_size + self.THUMB_PADDING)))
    

    def _calculate_layout(self, context):
        """Calculate common layout parameters to ensure consistency."""
        from . import data_storage
        
        # Use primary region for consistency (fallback to context.region for draw-time)
        region = VIEW3D_OT_thumbnail_gallery._primary_region or context.region
        if region is None:
            return None

        num_views = len(data_storage.get_saved_views())
        thumb_size_max = self._get_thumb_size_max()

        # Detect header position and calculate Y offset
        header_height = 0
        header_at_bottom = True  # Default assumption
        area = VIEW3D_OT_thumbnail_gallery._primary_area or context.area
        if area:
            for ar in area.regions:
                if ar.type == 'HEADER':
                    header_height = ar.height
                    # Check alignment property - 'BOTTOM' means header is at bottom
                    header_at_bottom = (ar.alignment == 'BOTTOM')
                    break

        layout_key = (
            region.width,
            region.height,
            num_views,
            self._scroll_offset,
            self._flip_to_top,
            header_height,
            header_at_bottom,
            thumb_size_max,
        )
        if self._layout_cache_key == layout_key and self._layout_cache is not None:
            debug_tools.inc("modal.layout.cache_hit")
            return self._layout_cache

        debug_tools.inc("modal.layout.recompute")

        # Calculate adaptive thumbnail size
        thumb_size = self._calculate_thumb_size(context, num_views)

        # Calculate visible range
        visible_count = self._get_visible_count(context)
        needs_scroll = num_views > visible_count

        # Clamp scroll offset
        max_offset = max(0, num_views - visible_count)
        scroll_offset = min(self._scroll_offset, max_offset)
        self._scroll_offset = scroll_offset  # Ensure persisted

        start_idx = scroll_offset if needs_scroll else 0
        end_idx = min(num_views, start_idx + visible_count)
        visible_views = end_idx - start_idx

        thumb_spacing = thumb_size + self.THUMB_PADDING
        total_content_width = (visible_views * thumb_spacing) + thumb_spacing

        start_x = (region.width - total_content_width + self.THUMB_PADDING) / 2

        # Calculate Y position based on gallery position and header alignment
        if self._flip_to_top:
            # Gallery at top - offset down if header is also at top
            if header_at_bottom:
                start_y = region.height - thumb_size - self.TOP_MARGIN - self.THUMB_PADDING * 2
            else:
                # Header at top, offset down by header height + margin
                start_y = region.height - thumb_size - self.TOP_MARGIN - self.THUMB_PADDING * 2 - header_height
        else:
            # Gallery at bottom (default) - offset up if header is also at bottom
            if header_at_bottom:
                start_y = self.STRIP_MARGIN + header_height
            else:
                # Header at top, no offset needed
                start_y = self.STRIP_MARGIN

        layout = {
            'thumb_size': thumb_size,
            'start_idx': start_idx,
            'end_idx': end_idx,
            'start_x': start_x,
            'start_y': start_y,
            'visible_views': visible_views,
            'thumb_spacing': thumb_spacing,
        }
        self._layout_cache_key = layout_key
        self._layout_cache = layout
        # Geometry depends on layout coordinates, so drop stale batches.
        self._geom_cache.clear()
        return layout
    
    def _draw_icon_shape(self, x, y, size, shape='PLUS', color=(0.6, 0.6, 0.6, 1.0), size_multiplier=0.5):
        """Draw an icon using Unicode text symbols."""
        center_x = x + size / 2
        center_y = y + size / 2
        
        # Use Unicode symbols for all icons - clean anti-aliased rendering
        font_id = 0
        font_size = int(size * size_multiplier)
        blf.size(font_id, font_size)
        blf.color(font_id, *color)
        
        if shape == 'PLUS':
            glyph = "＋"  # Full-width plus sign
        elif shape == 'REFRESH':
            glyph = "↻"
        elif shape == 'REORDER':
            glyph = "☰"  # Hamburger menu / reorder icon
        elif shape == 'CLOSE':
            glyph = "⏷"
        else:
            return  # Unknown shape
        
        # Center the glyph
        text_w, text_h = self._get_text_dimensions(font_id, font_size, glyph)
        glyph_x = center_x - text_w / 2
        glyph_y = center_y - text_h / 2
        
        blf.enable(font_id, blf.SHADOW)
        blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 0.5)
        blf.position(font_id, glyph_x, glyph_y, 0)
        blf.draw(font_id, glyph)
        blf.disable(font_id, blf.SHADOW)

    def _draw_dashed_border(self, x, y, width, height, color=(0.6, 0.6, 0.6, 0.8)):
        """Draw dashed border for the Add button."""
        shader = self._shader_uniform
        batch = self._get_dashed_border_batch(x, y, width, height)
        if batch is None:
            return

        gpu.state.line_width_set(2.0) # Thicker border
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        gpu.state.blend_set('NONE')

    def _draw_gallery(self):
        """Draw the thumbnail filmstrip with + button."""
        # Early bail if gallery is no longer active (prevents stale draw handler issues)
        if not VIEW3D_OT_thumbnail_gallery._is_active:
            return
        
        try:
            debug_tools.inc("modal.draw.calls")
            context = bpy.context
            # Only draw in the primary area (prevents drawing in all 3D views)
            if context.area != VIEW3D_OT_thumbnail_gallery._primary_area:
                return
            # Only draw in the primary WINDOW region. This avoids duplicate
            # draws in multi-region viewports (e.g. quad view splits).
            if context.region != VIEW3D_OT_thumbnail_gallery._primary_region:
                return
            # Safety check - only draw in 3D views
            if not context.area or context.area.type != 'VIEW_3D':
                return
            # Don't draw in camera view - keep it clean for composition
            space = context.space_data
            if space and hasattr(space, 'region_3d') and space.region_3d:
                if space.region_3d.view_perspective == 'CAMERA':
                    return
            with debug_tools.timed("modal.draw.total"):
                layout = self._calculate_layout(context)
                if not layout:
                    return

                # Unpack layout
                thumb_size = layout['thumb_size']
                start_idx = layout['start_idx']
                end_idx = layout['end_idx']
                start_x = layout['start_x']
                start_y = layout['start_y']
                visible_views = layout['visible_views']
                thumb_spacing = layout['thumb_spacing']

                # Update class state
                self._thumb_size = thumb_size

                # --- DRAW THUMBNAILS (CENTER) ---
                thumbs_start_x = start_x
                from . import data_storage
                current_idx = context.scene.saved_views_index
                num_views = len(data_storage.get_saved_views())

                # Track hidden view range for scroll indicators
                first_thumb_pos = None
                last_thumb_pos = None

                for draw_pos, i in enumerate(range(start_idx, end_idx)):
                    x = thumbs_start_x + draw_pos * thumb_spacing
                    y = start_y + self.THUMB_PADDING

                    if draw_pos == 0:
                        first_thumb_pos = (x, y)
                    last_thumb_pos = (x, y)

                    # Draw content first
                    if i in self._textures:
                        self._draw_texture(self._textures[i], x, y, self._thumb_size, self._thumb_size)
                    else:
                        self._draw_placeholder(x, y, self._thumb_size, self._thumb_size, i + 1)

                    self._draw_border(x, y, self._thumb_size, self._thumb_size)

                    # Draw highlight border on top (at exact thumbnail position)
                    if i == current_idx:
                        self._draw_selection_highlight(x, y, self._thumb_size, self._thumb_size)
                    elif i == self._hover_index:
                        self._draw_hover_highlight(x, y, self._thumb_size, self._thumb_size)

                    if i == self._hover_index:
                        self._draw_view_name(context, x, y, self._thumb_size, i)

                # --- DRAW PLUS BUTTON (RIGHT) ---
                plus_x = thumbs_start_x + (visible_views * thumb_spacing)
                plus_y = start_y + self.THUMB_PADDING

                self._plus_btn_rect = (plus_x, plus_y, self._thumb_size, self._thumb_size)

                # Transparent background for + button
                self._draw_placeholder(plus_x, plus_y, self._thumb_size, self._thumb_size, -1, color=(0.1, 0.1, 0.1, 0.0))

                # Dashed border - match refresh/close button colors
                border_color = (1.0, 1.0, 1.0, 1.0) if self._plus_hover else (0.5, 0.5, 0.5, 0.8)
                self._draw_dashed_border(plus_x, plus_y, self._thumb_size, self._thumb_size, color=border_color)

                # Icon color - match refresh/close button colors
                icon_color = (1.0, 1.0, 1.0, 1.0) if self._plus_hover else (0.5, 0.5, 0.5, 0.8)
                self._draw_icon_shape(plus_x, plus_y, self._thumb_size, 'PLUS', color=icon_color)

                # --- DRAW ACTION PANEL (FAR RIGHT) ---
                # Half-width panel with refresh (top), reorder (middle), and close (bottom) buttons
                action_panel_width = int(self._thumb_size * 0.5)
                action_btn_height = int(self._thumb_size / 3)  # 3 buttons
                action_panel_x = plus_x + self._thumb_size + self.THUMB_PADDING

                # Refresh All button (top third)
                refresh_x = action_panel_x
                refresh_y = start_y + self.THUMB_PADDING + action_btn_height * 2
                self._refresh_btn_rect = (refresh_x, refresh_y, action_panel_width, action_btn_height)

                refresh_color = (1.0, 1.0, 1.0, 1.0) if self._refresh_hover else (0.5, 0.5, 0.5, 0.8)
                self._draw_icon_shape(refresh_x, refresh_y, min(action_panel_width, action_btn_height), 'REFRESH', color=refresh_color, size_multiplier=0.7)

                # Reorder button (middle third)
                reorder_x = action_panel_x
                reorder_y = start_y + self.THUMB_PADDING + action_btn_height
                self._reorder_btn_rect = (reorder_x, reorder_y, action_panel_width, action_btn_height)

                reorder_color = (1.0, 1.0, 1.0, 1.0) if self._reorder_hover else (0.5, 0.5, 0.5, 0.8)
                self._draw_icon_shape(reorder_x, reorder_y, min(action_panel_width, action_btn_height), 'REORDER', color=reorder_color, size_multiplier=0.7)

                # Close Gallery button (bottom third)
                close_x = action_panel_x
                close_y = start_y + self.THUMB_PADDING
                self._close_btn_rect = (close_x, close_y, action_panel_width, action_btn_height)

                close_color = (1.0, 1.0, 1.0, 1.0) if self._close_hover else (0.5, 0.5, 0.5, 0.8)
                self._draw_icon_shape(close_x, close_y, min(action_panel_width, action_btn_height), 'CLOSE', color=close_color, size_multiplier=0.8)

                # --- SCROLL INDICATORS ---
                hidden_left = start_idx
                hidden_right = num_views - end_idx

                if hidden_left > 0 and first_thumb_pos:
                    self._draw_scroll_indicator(first_thumb_pos[0], first_thumb_pos[1],
                                                self._thumb_size, self._thumb_size, hidden_left, 'LEFT')
                if hidden_right > 0 and last_thumb_pos:
                    self._draw_scroll_indicator(last_thumb_pos[0], last_thumb_pos[1],
                                                self._thumb_size, self._thumb_size, hidden_right, 'RIGHT')

                # --- ENLARGED PREVIEW (MMB) ---
                if self._preview_index >= 0 and self._preview_index in self._textures:
                    self._draw_enlarged_preview(context, self._preview_index)
                
        except ReferenceError:
            # Operator instance has been garbage collected
            return
    
    def _draw_scroll_indicator(self, x, y, width, height, count, side):
        """Draw +N overlay on edge thumbnail to indicate hidden views."""
        # Semi-transparent overlay on the appropriate half
        overlay_width = width // 2
        if side == 'LEFT':
            overlay_x = x
        else:
            overlay_x = x + width - overlay_width

        # Draw semi-transparent gray background
        shader = self._shader_uniform
        batch = self._get_rect_batch('TRIS', overlay_x, y, overlay_width, height)
        if batch is None:
            return
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float("color", (0.2, 0.2, 0.2, 0.5))
        batch.draw(shader)
        gpu.state.blend_set('NONE')
        
        # Draw number
        font_id = 0
        font_size = 14
        blf.size(font_id, font_size)
        text = f"+{count}"
        text_width, text_height = self._get_text_dimensions(font_id, font_size, text)
        
        text_x = overlay_x + (overlay_width - text_width) / 2
        text_y = y + (height - text_height) / 2
        
        blf.position(font_id, text_x, text_y, 0)
        blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
        blf.draw(font_id, text)
    
    def _draw_enlarged_preview(self, context, index):
        """Draw enlarged thumbnail preview above gallery with dark backdrop."""
        texture = self._textures.get(index)
        if not texture:
            return
        
        region = context.region
        if not region:
            return
        
        # Get preferences
        try:
            from .preferences import get_preferences
            prefs = get_preferences()
            # Clamp to minimum 0.2 (slider shows 0-1 for visual alignment with other sliders)
            size_factor = max(0.2, prefs.preview_size_factor)
            backdrop_opacity = prefs.preview_backdrop_opacity
        except Exception:
            size_factor = 0.5
            backdrop_opacity = 0.5
        
        # Calculate preview size based on preference and screen size
        preview_size = min(
            int(region.width * size_factor),
            int(region.height * size_factor)
        )
        
        # Get gallery layout to position preview above it
        layout = self._calculate_layout(context)
        if not layout:
            return
        
        gallery_top = layout['start_y'] + self.THUMB_PADDING + layout['thumb_size']
        # Add some padding above gallery for view name text (~20px)
        preview_bottom = gallery_top + 30
        
        # Center horizontally, position above gallery
        preview_x = (region.width - preview_size) / 2
        preview_y = preview_bottom
        
        # Draw dark backdrop (full screen)
        shader = self._shader_uniform
        batch = self._get_rect_batch('TRIS', 0, 0, region.width, region.height)
        if batch is None:
            return
        
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float("color", (0.0, 0.0, 0.0, backdrop_opacity))
        batch.draw(shader)
        
        # Draw enlarged thumbnail
        self._draw_texture(texture, preview_x, preview_y, preview_size, preview_size)
        
        # Draw border around preview
        self._draw_border(preview_x, preview_y, preview_size, preview_size)
        
        gpu.state.blend_set('NONE')
    
    def _draw_background(self, x, y, width, height):
        """Draw semi-transparent background rectangle."""
        shader = self._shader_uniform
        batch = self._get_rect_batch('TRIS', x, y, width, height)
        if batch is None:
            return
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float("color", (0.1, 0.1, 0.1, 0.85))
        batch.draw(shader)
        gpu.state.blend_set('NONE')
    
    def _draw_selection_highlight(self, x, y, width, height):
        """Draw highlight border for selected thumbnail using theme color."""
        # Get theme color for active object
        theme = bpy.context.preferences.themes[0].view_3d
        color = (*theme.object_active[:3], 1.0)

        batch = self._get_rect_batch('LINE', x, y, width, height)
        if batch is None:
            return
        shader = self._shader_uniform
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(4.0)  # Thicker border for selection
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')
    
    def _draw_texture(self, texture, x, y, width, height):
        """Draw a thumbnail texture."""
        shader = self._shader_image
        batch = self._get_rect_batch('IMAGE', x, y, width, height)
        if batch is None:
            return
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_sampler("image", texture)
        batch.draw(shader)
        gpu.state.blend_set('NONE')
    
    def _draw_placeholder(self, x, y, width, height, number, color=(0.3, 0.3, 0.3, 1.0)):
        """Draw placeholder for views without thumbnails."""
        shader = self._shader_uniform
        batch = self._get_rect_batch('TRIS', x, y, width, height)
        if batch is None:
            return
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        gpu.state.blend_set('NONE')
    
    def _draw_border(self, x, y, width, height):
        """Draw faint black border around thumbnail."""
        shader = self._shader_uniform
        batch = self._get_rect_batch('LINE', x, y, width, height)
        if batch is None:
            return
        # Disable depth test so all edges render uniformly
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(1.0)
        shader.bind()
        shader.uniform_float("color", (0.0, 0.0, 0.0, 0.4))
        batch.draw(shader)
        gpu.state.blend_set('NONE')
    
    def _get_clicked_thumbnail(self, context, event):
        """Return index of clicked thumbnail, or None if click was outside."""
        layout = self._calculate_layout(context)
        if not layout:
            return None
            
        start_idx = layout['start_idx']
        end_idx = layout['end_idx']
        start_x = layout['start_x']
        start_y = layout['start_y']
        thumb_spacing = layout['thumb_spacing']
        thumb_size = layout['thumb_size']
        
        thumbs_start_x = start_x
        
        mx, my = self._get_mouse_region_coords(event)
        
        for draw_pos, i in enumerate(range(start_idx, end_idx)):
            x = thumbs_start_x + draw_pos * thumb_spacing
            y = start_y + self.THUMB_PADDING
            
            if x <= mx <= x + thumb_size and y <= my <= y + thumb_size:
                return i
        
        return None
    
    def _is_mouse_over_gallery(self, context, event):
        """Check if mouse is over the gallery background area."""
        layout = self._calculate_layout(context)
        if not layout:
            return False
            
        thumb_size = layout['thumb_size']
        visible_views = layout['visible_views']
        start_x = layout['start_x']
        start_y = layout['start_y']
        thumb_spacing = layout['thumb_spacing']
        
        # Calculate full strip dimensions
        total_content_width = (visible_views * thumb_spacing) + thumb_spacing
        strip_height = thumb_size + self.THUMB_PADDING * 2
        
        # Add margin for easier detection/clipping avoidance
        rect_start_x = start_x - 10
        rect_start_y = start_y - 10
        rect_end_x = start_x + total_content_width + 10
        rect_end_y = start_y + strip_height + 10
        
        mx, my = self._get_mouse_region_coords(event)
        return rect_start_x <= mx <= rect_end_x and rect_start_y <= my <= rect_end_y
    
    def _draw_hover_highlight(self, x, y, width, height):
        """Draw hover highlight border using theme color."""
        # Get theme color for selected object
        theme = bpy.context.preferences.themes[0].view_3d
        color = (*theme.object_selected[:3], 0.8)

        batch = self._get_rect_batch('LINE', x, y, width, height)
        if batch is None:
            return
        shader = self._shader_uniform
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)  # Slightly thinner for hover
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')
    
    def _draw_view_name(self, context, x, y, thumb_size, view_index):
        """Draw view name centered inside hovered thumbnail, clipped if too long."""
        from . import data_storage
        views = data_storage.get_saved_views()
        if view_index < 0 or view_index >= len(views):
            return
        
        view_name = views[view_index].get("name", "View")
        
        font_id = 0
        font_size = 14  # Fixed size regardless of thumbnail
        padding = 6  # Padding around text for background
        
        blf.size(font_id, font_size)
        text_width, text_height = self._get_text_dimensions(font_id, font_size, view_name)
        
        # Center inside thumbnail
        text_x = x + (thumb_size - text_width) / 2
        text_y = y + thumb_size + text_height
        
        # Draw semi-transparent background
        bg_x = text_x - padding
        bg_y = text_y - padding
        bg_w = text_width + padding * 2
        bg_h = text_height + padding * 2
        
        gpu.state.blend_set('ALPHA')
        shader = self._shader_uniform
        batch = self._get_rect_batch('TRIS', bg_x, bg_y, bg_w, bg_h)
        if batch is None:
            gpu.state.blend_set('NONE')
            return
        shader.bind()
        shader.uniform_float("color", (0.0, 0.0, 0.0, 0.7))
        batch.draw(shader)
        gpu.state.blend_set('NONE')
        
        # Draw text
        blf.position(font_id, text_x, text_y, 0)
        blf.color(font_id, 1.0, 1.0, 1.0, 0.9)
        blf.draw(font_id, view_name)


def _reset_gallery_state():
    """Reset gallery class state - called on file load and addon reload."""
    global _backup_draw_handler
    
    # Try to remove draw handler from backup first (survives class reload)
    if _backup_draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_backup_draw_handler, 'WINDOW')
        except Exception:
            pass
        _backup_draw_handler = None
    
    # Clear draw handler if it exists on the class (from previous instance)
    try:
        if VIEW3D_OT_thumbnail_gallery._draw_handler:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(
                    VIEW3D_OT_thumbnail_gallery._draw_handler, 'WINDOW'
                )
            except Exception:
                pass
            VIEW3D_OT_thumbnail_gallery._draw_handler = None
    except Exception:
        pass
    
    # Reset all class-level state
    try:
        for tex in list(VIEW3D_OT_thumbnail_gallery._textures.values()):
            free_fn = getattr(tex, "free", None)
            if callable(free_fn):
                try:
                    free_fn()
                except Exception:
                    pass
        VIEW3D_OT_thumbnail_gallery._is_active = False
        VIEW3D_OT_thumbnail_gallery._instance = None
        VIEW3D_OT_thumbnail_gallery._needs_refresh = False
        VIEW3D_OT_thumbnail_gallery._primary_area = None
        VIEW3D_OT_thumbnail_gallery._context_area = None
        VIEW3D_OT_thumbnail_gallery._context_menu_index = -1
        VIEW3D_OT_thumbnail_gallery._textures.clear()
    except Exception:
        pass

    # Clean up any stale temp display images.
    try:
        for img in list(bpy.data.images):
            if img.name.startswith(".VP_Display_"):
                bpy.data.images.remove(img)
    except Exception:
        pass
    
    # Force redraw all 3D views to clear any stale gallery overlay
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _auto_start_gallery():
    """Timer callback to start the gallery."""
    # Don't toggle if already active (prevents race condition on fresh files)
    if VIEW3D_OT_thumbnail_gallery._is_active:
        return None  # Already open, nothing to do
    
    area, _, region = utils.find_view3d_override_context(bpy.context)
    window = utils.find_window_for_area(bpy.context, area)
    if area and region and window:
        try:
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.view3d.thumbnail_gallery('INVOKE_DEFAULT')
        except Exception as e:
            print(f"[ViewPilot] Failed to auto-start gallery: {e}")
    return None  # Unregister timer

@bpy.app.handlers.persistent
def _on_load_post(dummy):
    """Handler called after loading a .blend file."""
    _reset_gallery_state()
    # Check preference before auto-starting
    try:
        from .preferences import get_preferences
        if not get_preferences().start_gallery_on_load:
            return
    except Exception:
        return  # Don't auto-start if we can't read preference (safer default)
    # Auto-enable gallery after a short delay to ensure context is ready
    bpy.app.timers.register(_auto_start_gallery, first_interval=0.5)


# =============================================================================
# CONTEXT MENU FOR GALLERY THUMBNAILS
# =============================================================================

class VIEW3D_MT_gallery_context(bpy.types.Menu):
    """Context menu for gallery thumbnail right-click"""
    bl_label = ""  # Empty - we set title dynamically
    bl_idname = "VIEW3D_MT_gallery_context"
    
    def draw(self, context):
        layout = self.layout
        
        # Get the clicked index
        idx = VIEW3D_OT_thumbnail_gallery._context_menu_index
        if idx < 0 or idx >= len(context.scene.saved_views):
            layout.label(text="No view selected")
            return
        
        view = context.scene.saved_views[idx]
        
        # Header with view name only (no separator, acts as title)
        layout.label(text=view.name, icon='HIDE_OFF')
        
        # Update from current viewport
        op = layout.operator("view3d.update_saved_view", text="Update", icon='FILE_REFRESH')
        op.index = idx
        
        # Rename
        layout.operator_context = 'INVOKE_DEFAULT'
        op = layout.operator("view3d.rename_saved_view", text="Rename", icon='FONT_DATA')
        op.index = idx
        
        layout.separator()
        
        # Create camera from this view
        op = layout.operator("view3d.gallery_view_to_camera", text="Create Camera", icon='OUTLINER_OB_CAMERA')
        op.index = idx
        
        layout.separator()
        
        # =================================================================
        # Remember Toggles (View Styles)
        # =================================================================
        layout.label(text="Remember:", icon='BOOKMARKS')
        
        layout.prop(view, "remember_perspective", text="Perspective")
        layout.prop(view, "remember_shading", text="Shading")
        layout.prop(view, "remember_overlays", text="Overlays")
        layout.prop(view, "remember_composition", text="Composition")
        
        layout.separator()
        
        # Delete
        op = layout.operator("view3d.gallery_delete_view", text="Delete", icon='X')
        op.index = idx
        
        layout.separator()
        
        # Flip gallery position
        instance = VIEW3D_OT_thumbnail_gallery._instance
        if instance:
            flip_text = "Flip to Bottom" if instance._flip_to_top else "Flip to Top"
            flip_icon = 'TRIA_DOWN' if instance._flip_to_top else 'TRIA_UP'
            layout.operator("view3d.gallery_flip_position", text=flip_text, icon=flip_icon)


class VIEW3D_OT_gallery_close(bpy.types.Operator):
    """Close the thumbnail gallery"""
    bl_idname = "view3d.gallery_close"
    bl_label = "Close Gallery"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        if VIEW3D_OT_thumbnail_gallery._is_active:
            VIEW3D_OT_thumbnail_gallery._is_active = False
            if VIEW3D_OT_thumbnail_gallery._instance:
                VIEW3D_OT_thumbnail_gallery._instance._cleanup(context)
        return {'FINISHED'}


class VIEW3D_OT_gallery_flip_position(bpy.types.Operator):
    """Flip gallery between top and bottom of viewport"""
    bl_idname = "view3d.gallery_flip_position"
    bl_label = "Flip Gallery Position"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        instance = VIEW3D_OT_thumbnail_gallery._instance
        if instance:
            instance._flip_to_top = not instance._flip_to_top
            instance._invalidate_layout_cache()
            context.area.tag_redraw()
        return {'FINISHED'}


class VIEW3D_OT_gallery_load_view(bpy.types.Operator):
    """Navigate to this saved view"""
    bl_idname = "view3d.gallery_load_view"
    bl_label = "Go to View"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        from . import data_storage
        views = data_storage.get_saved_views()
        if 0 <= self.index < len(views):
            context.scene.viewpilot.saved_views_enum = str(self.index)
        return {'FINISHED'}




class VIEW3D_OT_gallery_delete_view(bpy.types.Operator):
    """Delete this saved view"""
    bl_idname = "view3d.gallery_delete_view"
    bl_label = "Delete View"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def invoke(self, context, event):
        # Show confirmation dialog
        return context.window_manager.invoke_confirm(self, event)
    
    def execute(self, context):
        # Delegate to the canonical delete operator so index remapping and enum
        # synchronization logic stay consistent across all delete entry points.
        result = bpy.ops.view3d.delete_saved_view(index=self.index)
        if 'FINISHED' in result:
            return {'FINISHED'}
        return {'CANCELLED'}


class VIEW3D_OT_gallery_view_to_camera(bpy.types.Operator):
    """Create a camera at this view's position"""
    bl_idname = "view3d.gallery_view_to_camera"
    bl_label = "Create Camera from View"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        from mathutils import Vector, Quaternion
        from .utils import create_camera_from_view_data
        from . import data_storage
        
        views = data_storage.get_saved_views()
        if not (0 <= self.index < len(views)):
            return {'CANCELLED'}
        
        view_dict = views[self.index]
        
        # Find the view's target scene (stored composition)
        stored_scene_id = view_dict.get('composition_scene_uuid', '')
        stored_scene_name = view_dict.get('composition_scene', '')
        target_scene = None
        
        if stored_scene_id:
            target_scene = data_storage.find_scene_by_identity(stored_scene_id)
        if not target_scene and stored_scene_name:
            target_scene = bpy.data.scenes.get(stored_scene_name)
        if not target_scene:
            target_scene = context.scene  # Fallback to current scene
        
        # Get preferences
        try:
            from .preferences import get_preferences
            prefs = get_preferences()
            passepartout = prefs.camera_passepartout
            show_passepartout = prefs.show_passepartout
            show_name = prefs.show_camera_name
            show_sensor = prefs.show_camera_sensor
            use_collection = prefs.use_camera_collection
            collection_name = prefs.camera_collection_name
            collection_color = prefs.camera_collection_color
            camera_name_prefix = prefs.camera_name_prefix
        except Exception:
            passepartout = 0.95
            show_passepartout = True
            show_name = True
            show_sensor = True
            use_collection = True
            collection_name = "ViewPilot"
            collection_color = 'COLOR_04'
            camera_name_prefix = "ViewCam"
        
        # Create camera name: "PrefixName [View Name]"
        cam_name = f"{camera_name_prefix} [{view_dict.get('name', 'View')}]"
        
        # Calculate camera position from saved view data
        rotation = view_dict.get("rotation", [1.0, 0.0, 0.0, 0.0])
        rot_quat = Quaternion((rotation[0], rotation[1], rotation[2], rotation[3]))
        view_z = Vector((0.0, 0.0, 1.0))
        location = view_dict.get("location", [0, 0, 0])
        distance = view_dict.get("distance", 10.0)
        eye_pos = Vector(location) + (rot_quat @ view_z) * distance
        
        # Create camera using centralized utility
        cam_obj = create_camera_from_view_data(
            context=context,
            name=cam_name,
            location=eye_pos,
            rotation=rot_quat,
            is_perspective=view_dict.get("is_perspective", True),
            lens=view_dict.get("lens", 50.0),
            distance=distance,
            clip_start=view_dict.get("clip_start", 0.01),
            clip_end=view_dict.get("clip_end", 1000.0),
            passepartout=passepartout,
            show_passepartout=show_passepartout,
            show_name=show_name,
            show_sensor=show_sensor,
            use_collection=use_collection,
            collection_name=collection_name,
            collection_color=collection_color,
            scene=target_scene
        )
        
        self.report({'INFO'}, f"Created camera: {view_dict.get('name', 'View')}")
        return {'FINISHED'}


def register():
    bpy.utils.register_class(VIEW3D_OT_thumbnail_gallery)
    bpy.utils.register_class(VIEW3D_MT_gallery_context)
    bpy.utils.register_class(VIEW3D_OT_gallery_close)
    bpy.utils.register_class(VIEW3D_OT_gallery_flip_position)
    bpy.utils.register_class(VIEW3D_OT_gallery_load_view)
    bpy.utils.register_class(VIEW3D_OT_gallery_delete_view)
    bpy.utils.register_class(VIEW3D_OT_gallery_view_to_camera)
    # Reset state on registration (in case of addon reload)
    _reset_gallery_state()
    # Add file load handler
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    # Check if we should auto-start gallery (respects preference on addon reload)
    try:
        from .preferences import get_preferences
        if get_preferences().start_gallery_on_load:
            bpy.app.timers.register(_auto_start_gallery, first_interval=0.5)
    except Exception:
        pass


def unregister():
    # Clean up any running gallery before unregistering
    _reset_gallery_state()
    # Remove file load handler
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    bpy.utils.unregister_class(VIEW3D_OT_gallery_view_to_camera)
    bpy.utils.unregister_class(VIEW3D_OT_gallery_delete_view)
    bpy.utils.unregister_class(VIEW3D_OT_gallery_load_view)
    bpy.utils.unregister_class(VIEW3D_OT_gallery_flip_position)
    bpy.utils.unregister_class(VIEW3D_OT_gallery_close)
    bpy.utils.unregister_class(VIEW3D_MT_gallery_context)
    bpy.utils.unregister_class(VIEW3D_OT_thumbnail_gallery)

