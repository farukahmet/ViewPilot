"""
Thumbnail Generation for ViewPilot Saved Views.

Uses OpenGL method (bpy.ops.render.opengl) for:
- Better color accuracy (Standard transform + Non-Color), as I
  couldn't get the same colors as the viewport with GPUOffScreen 
  (they come out darker due to, I think, linear colorspace it 
  provides them in)
- Faster performance for most scenes (benchmarked 2-4x faster)
  compared to GPUOffScreen; except for very large, unoptimized 
  files I tend to create, ironically. But that's a me problem.

Note: Cycles RENDERED mode cannot be captured - falls back to SOLID. 
Would be too expensive anyways.
"""

import bpy
import os
import tempfile
import traceback


class ThumbnailRenderer:
    """
    Renders thumbnails using bpy.ops.render.opengl.
    Captures the current viewport directly.
    """
    
    def __init__(self, size=256):
        self.size = size
    
    def render_from_view_data(self, context, saved_view, image_name):
        """
        Render the viewport with saved view's shading applied.
        saved_view is used to apply shading settings for accurate thumbnails.
        """
        area, space, region = self._find_view3d_context(context)
        if not all([area, space, region]):
            print("[ViewPilot] No valid 3D View found for thumbnail rendering")
            return None
        
        # Store original shading settings
        shading = space.shading
        orig_shading_type = shading.type
        orig_shading_light = shading.light
        orig_shading_color_type = shading.color_type
        orig_shading_single_color = shading.single_color[:]
        orig_shading_background_type = shading.background_type
        orig_shading_background_color = shading.background_color[:]
        # Only read studio_light when not in WIREFRAME mode (WIREFRAME has no valid studio_light)
        orig_shading_studio_light = shading.studio_light if orig_shading_type != 'WIREFRAME' else ''
        orig_shading_studiolight_rotate_z = shading.studiolight_rotate_z
        orig_shading_studiolight_intensity = shading.studiolight_intensity
        orig_shading_studiolight_background_alpha = shading.studiolight_background_alpha
        orig_shading_studiolight_background_blur = shading.studiolight_background_blur
        orig_shading_show_cavity = shading.show_cavity
        orig_shading_show_object_outline = shading.show_object_outline
        orig_shading_show_xray = shading.show_xray
        orig_shading_show_shadows = shading.show_shadows
        orig_shading_use_scene_lights = shading.use_scene_lights
        orig_shading_use_scene_world = shading.use_scene_world
        orig_world = context.scene.world
        
        # Apply saved view's shading settings (if saved_view has them)
        if hasattr(saved_view, 'shading_type') and saved_view.shading_type:
            try:
                # Always apply shading type (including WIREFRAME)
                # Don't apply RENDERED mode (can't capture with Cycles)
                if saved_view.shading_type != 'RENDERED':
                    shading.type = saved_view.shading_type
                
                # These properties may not be valid in all shading modes (e.g., WIREFRAME)
                # Wrap in individual try/except to apply what we can
                try:
                    shading.light = saved_view.shading_light
                except TypeError:
                    pass
                try:
                    shading.color_type = saved_view.shading_color_type
                except TypeError:
                    pass
                try:
                    shading.single_color = saved_view.shading_single_color[:]
                except (TypeError, AttributeError):
                    pass
                try:
                    shading.background_type = saved_view.shading_background_type
                except TypeError:
                    pass
                if hasattr(saved_view, 'shading_background_color'):
                    try:
                        shading.background_color = saved_view.shading_background_color[:]
                    except (TypeError, AttributeError):
                        pass
                if saved_view.shading_studio_light:
                    try:
                        shading.studio_light = saved_view.shading_studio_light
                    except TypeError:
                        pass
                try:
                    shading.studiolight_rotate_z = saved_view.shading_studiolight_rotate_z
                    shading.studiolight_intensity = saved_view.shading_studiolight_intensity
                    shading.studiolight_background_alpha = saved_view.shading_studiolight_background_alpha
                    shading.studiolight_background_blur = saved_view.shading_studiolight_background_blur
                except (TypeError, AttributeError):
                    pass
                shading.show_cavity = saved_view.shading_show_cavity
                shading.show_object_outline = saved_view.shading_show_object_outline
                shading.show_xray = saved_view.shading_show_xray
                shading.show_shadows = saved_view.shading_show_shadows
                shading.use_scene_lights = saved_view.shading_use_scene_lights
                shading.use_scene_world = saved_view.shading_use_scene_world
                # Apply saved World datablock if stored and exists
                if hasattr(saved_view, 'shading_selected_world') and saved_view.shading_selected_world:
                    if saved_view.shading_selected_world in bpy.data.worlds:
                        context.scene.world = bpy.data.worlds[saved_view.shading_selected_world]
            except Exception as e:
                print(f"[ViewPilot] Could not apply saved shading: {e}")
        
        # Detect Cycles RENDERED mode (can't capture - use SOLID fallback)
        is_cycles_rendered = (
            shading.type == 'RENDERED' and 
            context.scene.render.engine == 'CYCLES'
        )
        if is_cycles_rendered:
            shading.type = 'SOLID'
        
        # Store original settings
        orig_res_x = context.scene.render.resolution_x
        orig_res_y = context.scene.render.resolution_y
        orig_res_percent = context.scene.render.resolution_percentage
        orig_filepath = context.scene.render.filepath
        orig_format = context.scene.render.image_settings.file_format
        orig_view_transform = context.scene.view_settings.view_transform
        orig_look = context.scene.view_settings.look
        
        temp_filepath = os.path.join(tempfile.gettempdir(), f"_vp_thumb_{image_name}.png")
        
        try:
            # Configure render settings
            context.scene.render.resolution_x = self.size
            context.scene.render.resolution_y = self.size
            context.scene.render.resolution_percentage = 100
            context.scene.render.filepath = temp_filepath
            context.scene.render.image_settings.file_format = 'PNG'
            
            # COLOR MANAGEMENT FOR THUMBNAILS
            # ================================
            # The goal: capture the viewport as close how the user see it as possible, and display
            # it correctly in the galleries regardless of the user's color settings.
            #
            # Problem: If I capture with the user's view transform (e.g., Filmic, AgX),
            # the galleries will apply ANOTHER transform when displaying, resulting in
            # double-transformed, messed up colors.
            #
            # Solution:
            # 1. CAPTURE: Use 'Standard' view transform (linear-to-sRGB, no tone mapping).
            #    This produces a clean sRGB PNG that looks like the viewport.
            #
            # 2. STORAGE: Load the PNG and mark as 'Non-Color' (see _load_from_file).
            #    This tells Blender "don't interpret this data, just store it raw".
            #    Without this, loading as sRGB makes images appear slightly darker 
            #    than viewport.
            #
            # 3. DISPLAY: The galleries call save_render() which applies the display
            #    transform (Standard by default), then create GPU textures from that.
            #    This ensures correct colors regardless of user's Render Properties.
            #    Though by "correct" I mean Standard, not user's chosen view transform.
            #    but that still looks closer to the viewport than other options.
            context.scene.view_settings.view_transform = 'Standard'
            context.scene.view_settings.look = 'None'
            
            # Handle overlays for thumbnails:
            # - If saved view has overlays OFF, keep them off (no wireframes/grid/axes)
            # - If saved view has overlays ON, show floor/axes/wireframes but hide other overlays
            overlay = space.overlay
            orig_show_overlays = overlay.show_overlays
            
            # Save settings we'll change and restore
            orig_show_floor = overlay.show_floor
            orig_show_axis_x = overlay.show_axis_x
            orig_show_axis_y = overlay.show_axis_y
            orig_show_axis_z = overlay.show_axis_z
            orig_show_wireframes = overlay.show_wireframes
            orig_wireframe_threshold = overlay.wireframe_threshold
            orig_wireframe_opacity = overlay.wireframe_opacity
            orig_show_cursor = overlay.show_cursor
            orig_show_object_origins = overlay.show_object_origins
            orig_show_extras = overlay.show_extras
            orig_show_bones = overlay.show_bones
            orig_show_text = overlay.show_text
            orig_show_annotation = overlay.show_annotation
            orig_show_outline_selected = overlay.show_outline_selected
            orig_show_relationship_lines = overlay.show_relationship_lines
            
            # Check if saved view wants overlays at all
            saved_overlays_on = getattr(saved_view, 'overlays_show_overlays', True)
            
            if saved_overlays_on:
                # Overlays enabled - show floor/axes/wireframes from saved view, hide other overlays
                overlay.show_overlays = True
                
                # Apply saved view's floor/axes/wireframe settings
                if hasattr(saved_view, 'overlays_show_floor'):
                    overlay.show_floor = saved_view.overlays_show_floor
                    overlay.show_axis_x = saved_view.overlays_show_axis_x
                    overlay.show_axis_y = saved_view.overlays_show_axis_y
                    overlay.show_axis_z = saved_view.overlays_show_axis_z
                    overlay.show_wireframes = saved_view.overlays_show_wireframes
                    overlay.wireframe_threshold = saved_view.overlays_wireframe_threshold
                    overlay.wireframe_opacity = saved_view.overlays_wireframe_opacity
                
                # Disable non-preserved overlays
                overlay.show_cursor = False
                overlay.show_object_origins = False
                overlay.show_extras = False
                overlay.show_bones = False
                overlay.show_text = False
                overlay.show_annotation = False
                overlay.show_outline_selected = False
                overlay.show_relationship_lines = False
            else:
                # Overlays disabled in saved view - keep them off
                overlay.show_overlays = False
            
            # Force viewport update before capture (needed for wireframe and HDRI backgrounds)
            area.tag_redraw()
            bpy.context.view_layer.update()
            
            # Render the current viewport
            with context.temp_override(area=area, region=region, space_data=space):
                bpy.ops.render.opengl(write_still=True, view_context=True)
            
            # Restore all overlay settings
            overlay.show_overlays = orig_show_overlays
            overlay.show_floor = orig_show_floor
            overlay.show_axis_x = orig_show_axis_x
            overlay.show_axis_y = orig_show_axis_y
            overlay.show_axis_z = orig_show_axis_z
            overlay.show_wireframes = orig_show_wireframes
            overlay.wireframe_threshold = orig_wireframe_threshold
            overlay.wireframe_opacity = orig_wireframe_opacity
            overlay.show_cursor = orig_show_cursor
            overlay.show_object_origins = orig_show_object_origins
            overlay.show_extras = orig_show_extras
            overlay.show_bones = orig_show_bones
            overlay.show_text = orig_show_text
            overlay.show_annotation = orig_show_annotation
            overlay.show_outline_selected = orig_show_outline_selected
            overlay.show_relationship_lines = orig_show_relationship_lines
            
            # Load rendered image
            self._load_from_file(temp_filepath, image_name)
            return image_name
            
        except Exception as e:
            print(f"[ViewPilot] OpenGL render error: {e}")
            traceback.print_exc()
            return None
            
        finally:
            # Restore all shading settings
            # Restore type first, then other properties (some may not be valid in all modes)
            shading.type = orig_shading_type
            try:
                shading.light = orig_shading_light
            except TypeError:
                pass
            try:
                shading.color_type = orig_shading_color_type
            except TypeError:
                pass
            try:
                shading.single_color = orig_shading_single_color
            except TypeError:
                pass
            try:
                shading.background_type = orig_shading_background_type
            except TypeError:
                pass
            try:
                shading.background_color = orig_shading_background_color
            except TypeError:
                pass
            # Guard against empty studio_light (invalid in WIREFRAME mode)
            if orig_shading_studio_light:
                try:
                    shading.studio_light = orig_shading_studio_light
                except TypeError:
                    pass  # Skip if enum value is not valid for current shading type
            try:
                shading.studiolight_rotate_z = orig_shading_studiolight_rotate_z
                shading.studiolight_intensity = orig_shading_studiolight_intensity
                shading.studiolight_background_alpha = orig_shading_studiolight_background_alpha
                shading.studiolight_background_blur = orig_shading_studiolight_background_blur
            except TypeError:
                pass
            shading.show_cavity = orig_shading_show_cavity
            shading.show_object_outline = orig_shading_show_object_outline
            shading.show_xray = orig_shading_show_xray
            shading.show_shadows = orig_shading_show_shadows
            shading.use_scene_lights = orig_shading_use_scene_lights
            shading.use_scene_world = orig_shading_use_scene_world
            context.scene.world = orig_world
            
            # Restore render settings
            context.scene.view_settings.view_transform = orig_view_transform
            context.scene.view_settings.look = orig_look
            context.scene.render.resolution_x = orig_res_x
            context.scene.render.resolution_y = orig_res_y
            context.scene.render.resolution_percentage = orig_res_percent
            context.scene.render.filepath = orig_filepath
            context.scene.render.image_settings.file_format = orig_format
            
            # Cleanup temp file
            try:
                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
            except:
                pass
    
    def _find_view3d_context(self, context):
        """Find a valid VIEW_3D area, space, and WINDOW region.
        
        Priority: 1) context.area if it's a VIEW_3D,
                  2) Gallery's _context_area (for topbar/non-3D contexts),
                  3) First VIEW_3D on screen.
        """
        try:
            if context is None:
                return None, None, None
            
            # Priority 1: context.area if it's already a VIEW_3D
            if context.area and context.area.type == 'VIEW_3D':
                area = context.area
                space = context.space_data
                for reg in area.regions:
                    if reg.type == 'WINDOW':
                        return area, space, reg
            
            # Priority 2: Gallery's tracked context_area (for topbar, etc.)
            try:
                from .modal_gallery import VIEW3D_OT_thumbnail_gallery
                gallery_area = VIEW3D_OT_thumbnail_gallery._context_area
                if gallery_area and gallery_area.type == 'VIEW_3D':
                    # Verify it's still valid
                    for window in bpy.context.window_manager.windows:
                        for area in window.screen.areas:
                            if area == gallery_area:
                                space = area.spaces.active
                                for reg in area.regions:
                                    if reg.type == 'WINDOW':
                                        return area, space, reg
            except:
                pass
            
            # Priority 3: Fall back to first VIEW_3D on screen
            if context.screen:
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        space = area.spaces.active
                        for reg in area.regions:
                            if reg.type == 'WINDOW':
                                return area, space, reg
            return None, None, None
        except Exception as e:
            print(f"[ViewPilot] Error finding 3D view context: {e}")
            return None, None, None
    
    def _load_from_file(self, filepath, image_name):
        """Load the rendered PNG and prepare it for display."""
        try:
            img = bpy.data.images.get(image_name)
            if img:
                img.filepath = filepath
                img.reload()
            else:
                img = bpy.data.images.load(filepath, check_existing=False)
                img.name = image_name
            
            # Mark as Non-Color to prevent Blender from re-interpreting the data.
            # The PNG was rendered with Standard transform (sRGB output), so color
            # data is already correct. If we left this as 'sRGB', Blender would
            # apply an additional linearization on access, making colors darker.
            # The galleries then apply their own display transform via save_render().
            img.colorspace_settings.name = 'Non-Color'
            img.use_fake_user = True
            
            # Pack so it persists with .blend file
            if not img.packed_file:
                img.pack()
                
        except Exception as e:
            print(f"[ViewPilot] Error loading thumbnail from file: {e}")


# Module-level instance
_renderer = None


def get_renderer(size=256):
    """Get or create the thumbnail renderer."""
    global _renderer
    if _renderer is None or _renderer.size != size:
        _renderer = ThumbnailRenderer(size)
    return _renderer


def generate_thumbnail(context, saved_view, name_suffix=None):
    """Generate a thumbnail for a saved view."""
    if name_suffix is None:
        name_suffix = saved_view.name
    
    image_name = f".VP_Thumb_{name_suffix}"
    renderer = get_renderer()
    result = renderer.render_from_view_data(context, saved_view, image_name)
    
    # Refresh the panel gallery preview after generating thumbnail
    try:
        from .preview_manager import refresh_view_preview
        refresh_view_preview(name_suffix)
    except:
        pass  # Panel gallery refresh is optional
    
    return result


def delete_thumbnail(view_name):
    """Delete the thumbnail image for a saved view."""
    image_name = f".VP_Thumb_{view_name}"
    img = bpy.data.images.get(image_name)
    if img:
        bpy.data.images.remove(img)
    
    # Invalidate panel gallery cache
    try:
        from .preview_manager import invalidate_panel_gallery_cache
        invalidate_panel_gallery_cache()
    except:
        pass
