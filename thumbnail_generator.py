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
import glob
import os
import traceback

from . import utils
from .temp_paths import make_temp_png_path


THUMBNAIL_RENDERER_VERSION = "2026-02-11-write-still"


def _temp_thumbnail_path(image_name):
    """Get a deterministic temp path for OpenGL thumbnail output."""
    return make_temp_png_path("_vp_thumb_", image_name)


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
        orig_scene_view_settings_state = self._snapshot_rna_scalars(context.scene.view_settings)
        orig_scene_display_settings_state = self._snapshot_rna_scalars(context.scene.display_settings)
        orig_scene_curve_mapping_state = self._snapshot_curve_mapping(context.scene.view_settings)
        image_settings = context.scene.render.image_settings
        orig_image_settings_state = self._snapshot_rna_scalars(image_settings)
        orig_image_view_settings_state = None
        orig_image_curve_mapping_state = None
        if hasattr(image_settings, "view_settings"):
            orig_image_view_settings_state = self._snapshot_rna_scalars(image_settings.view_settings)
            orig_image_curve_mapping_state = self._snapshot_curve_mapping(image_settings.view_settings)
        orig_image_display_settings_state = None
        if hasattr(image_settings, "display_settings"):
            orig_image_display_settings_state = self._snapshot_rna_scalars(image_settings.display_settings)
        orig_scene_display_device = orig_scene_display_settings_state.get("display_device")
        orig_scene_view_transform = orig_scene_view_settings_state.get("view_transform")
        orig_scene_look = orig_scene_view_settings_state.get("look")
        orig_image_color_management = orig_image_settings_state.get("color_management")
        orig_image_display_device = (
            orig_image_display_settings_state.get("display_device")
            if orig_image_display_settings_state else None
        )
        orig_image_view_transform = (
            orig_image_view_settings_state.get("view_transform")
            if orig_image_view_settings_state else None
        )
        orig_image_look = (
            orig_image_view_settings_state.get("look")
            if orig_image_view_settings_state else None
        )
        # Avoid touching image_settings.linear_colorspace_settings.name here.
        # On some Blender builds this can emit RNA warnings when the current
        # value is transient/invalid, and we don't need it for thumbnail output.
        orig_use_multiview = getattr(context.scene.render, "use_multiview", None)
        orig_views_format = getattr(context.scene.render, "views_format", None)

        temp_filepath = _temp_thumbnail_path(image_name)
        temp_base = os.path.splitext(temp_filepath)[0]
        output_filepath = temp_filepath

        # Overlay state must always be restored, even when rendering fails.
        overlay = space.overlay
        orig_show_overlays = overlay.show_overlays
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
        
        try:
            # Configure render settings
            context.scene.render.resolution_x = self.size
            context.scene.render.resolution_y = self.size
            context.scene.render.resolution_percentage = 100
            context.scene.render.filepath = temp_base
            image_settings = context.scene.render.image_settings

            # Force output context suitable for thumbnail files.
            if hasattr(context.scene.render, "use_multiview"):
                try:
                    context.scene.render.use_multiview = False
                except Exception:
                    pass

            if hasattr(context.scene.render, "views_format"):
                views_ids = self._enum_ids(context.scene.render, "views_format")
                if "INDIVIDUAL" in views_ids:
                    try:
                        context.scene.render.views_format = "INDIVIDUAL"
                    except Exception:
                        pass

            if hasattr(image_settings, "media_type"):
                media_ids = self._enum_ids(image_settings, "media_type")
                if "IMAGE" in media_ids:
                    try:
                        image_settings.media_type = "IMAGE"
                    except Exception:
                        pass

            # Avoid output-level color-management override affecting thumbnail
            # tonemapping during OpenGL file write. We want scene-driven output.
            color_mgmt_ids = self._enum_ids(image_settings, "color_management")
            if "FOLLOW_SCENE" in color_mgmt_ids:
                try:
                    image_settings.color_management = "FOLLOW_SCENE"
                except Exception:
                    pass

            format_ids = self._enum_ids(image_settings, "file_format")
            if "PNG" not in format_ids:
                raise TypeError(f"PNG unavailable in file_format enum: {sorted(format_ids)}")
            image_settings.file_format = "PNG"

            color_ids = self._enum_ids(image_settings, "color_mode")
            if "RGBA" in color_ids:
                image_settings.color_mode = "RGBA"
            elif "RGB" in color_ids:
                image_settings.color_mode = "RGB"
            else:
                raise TypeError(f"Neither RGBA nor RGB available in color_mode enum: {sorted(color_ids)}")

            output_filepath = bpy.path.ensure_ext(temp_base, context.scene.render.file_extension)
            
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
            
            # Render directly to a temp file.
            with context.temp_override(area=area, region=region, space_data=space):
                bpy.ops.render.opengl(write_still=True, view_context=True)

            if not os.path.exists(output_filepath):
                if os.path.exists(temp_filepath):
                    output_filepath = temp_filepath
                else:
                    matches = glob.glob(f"{temp_base}.*")
                    if matches:
                        output_filepath = matches[0]

            if not os.path.exists(output_filepath):
                print(f"[ViewPilot] OpenGL thumbnail output missing: {output_filepath}")
                return None

            # Load rendered image and pack into blend.
            if self._load_from_file(output_filepath, image_name):
                return image_name
            return None
            
        except Exception as e:
            print(f"[ViewPilot] OpenGL render error: {e}")
            traceback.print_exc()
            return None
            
        finally:
            # Restore all overlay settings
            try:
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
            except Exception:
                pass

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
            self._restore_rna_scalars(
                context.scene.display_settings,
                orig_scene_display_settings_state,
                debug_prefix="restore scene.display_settings",
            )
            self._restore_rna_scalars(
                context.scene.view_settings,
                orig_scene_view_settings_state,
                debug_prefix="restore scene.view_settings",
                priority=("view_transform", "look", "exposure", "gamma"),
            )
            self._restore_curve_mapping(
                context.scene.view_settings,
                orig_scene_curve_mapping_state,
                debug_prefix="restore scene.view_settings.curve_mapping",
            )
            # Explicit pass for color-management chain (handles enum edge cases).
            self._set_enum_value(
                context.scene.display_settings,
                "display_device",
                orig_scene_display_device,
                "restore scene.display_settings.display_device",
            )
            self._set_enum_value(
                context.scene.view_settings,
                "view_transform",
                orig_scene_view_transform,
                "restore scene.view_settings.view_transform",
            )
            self._set_enum_value(
                context.scene.view_settings,
                "look",
                orig_scene_look,
                "restore scene.view_settings.look",
            )
            try:
                context.scene.render.resolution_x = orig_res_x
                context.scene.render.resolution_y = orig_res_y
                context.scene.render.resolution_percentage = orig_res_percent
                context.scene.render.filepath = orig_filepath
            except Exception:
                pass
            if orig_use_multiview is not None and hasattr(context.scene.render, "use_multiview"):
                try:
                    context.scene.render.use_multiview = orig_use_multiview
                except Exception:
                    pass
            if orig_views_format is not None and hasattr(context.scene.render, "views_format"):
                views_ids = self._enum_ids(context.scene.render, "views_format")
                if orig_views_format in views_ids:
                    try:
                        context.scene.render.views_format = orig_views_format
                    except Exception:
                        pass
            image_settings = context.scene.render.image_settings
            self._restore_rna_scalars(
                image_settings,
                orig_image_settings_state,
                debug_prefix="restore image_settings",
                priority=("media_type", "file_format", "color_mode", "color_depth"),
            )
            if orig_image_display_settings_state and hasattr(image_settings, "display_settings"):
                self._restore_rna_scalars(
                    image_settings.display_settings,
                    orig_image_display_settings_state,
                    debug_prefix="restore image_settings.display_settings",
                )
            if orig_image_view_settings_state and hasattr(image_settings, "view_settings"):
                self._restore_rna_scalars(
                    image_settings.view_settings,
                    orig_image_view_settings_state,
                    debug_prefix="restore image_settings.view_settings",
                    priority=("view_transform", "look", "exposure", "gamma"),
                )
                self._restore_curve_mapping(
                    image_settings.view_settings,
                    orig_image_curve_mapping_state,
                    debug_prefix="restore image_settings.view_settings.curve_mapping",
                )
            # Explicit pass for override color-management chain.
            self._set_enum_value(
                image_settings,
                "color_management",
                orig_image_color_management,
                "restore image_settings.color_management",
            )
            if hasattr(image_settings, "display_settings"):
                self._set_enum_value(
                    image_settings.display_settings,
                    "display_device",
                    orig_image_display_device,
                    "restore image_settings.display_settings.display_device",
                )
            if hasattr(image_settings, "view_settings"):
                self._set_enum_value(
                    image_settings.view_settings,
                    "view_transform",
                    orig_image_view_transform,
                    "restore image_settings.view_settings.view_transform",
                )
                self._set_enum_value(
                    image_settings.view_settings,
                    "look",
                    orig_image_look,
                    "restore image_settings.view_settings.look",
                )
            
            # Cleanup temp file
            try:
                if os.path.exists(output_filepath):
                    os.remove(output_filepath)
                if os.path.exists(temp_filepath) and temp_filepath != output_filepath:
                    os.remove(temp_filepath)
            except Exception:
                pass

    def _enum_ids(self, rna_owner, prop_name):
        """Return enum identifiers for an RNA property, or empty set on failure."""
        try:
            return {
                item.identifier
                for item in rna_owner.bl_rna.properties[prop_name].enum_items
            }
        except Exception:
            return set()

    def _snapshot_rna_scalars(self, rna_owner):
        """Snapshot writable scalar RNA properties (bool/int/float/string/enum)."""
        state = {}
        try:
            for prop in rna_owner.bl_rna.properties:
                prop_name = prop.identifier
                if prop_name == "rna_type" or prop.is_readonly:
                    continue
                if prop.type not in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
                    continue
                try:
                    state[prop_name] = getattr(rna_owner, prop_name)
                except Exception:
                    pass
        except Exception:
            pass
        return state

    def _try_set_rna_scalar(self, rna_owner, prop_name, value):
        """Try to set one scalar RNA property. Return True on success."""
        try:
            prop = rna_owner.bl_rna.properties[prop_name]
        except Exception:
            return False

        if prop.is_readonly:
            return False

        # For dynamic enum sets, direct assignment is more reliable than
        # pre-checking enum_items; assignment can itself unlock dependent enums.
        try:
            setattr(rna_owner, prop_name, value)
            return True
        except Exception:
            return False

    def _set_enum_value(self, rna_owner, prop_name, value, debug_prefix):
        """Set an enum/string property directly with focused debug on failure."""
        if value is None:
            return
        try:
            setattr(rna_owner, prop_name, value)
            return
        except Exception:
            return

    def _restore_rna_scalars(self, rna_owner, state, debug_prefix="", priority=()):
        """Restore scalar RNA state with dependency-aware ordering."""
        if not state:
            return

        pending = dict(state)

        # First pass for known dependency-sensitive settings.
        for prop_name in priority:
            if prop_name not in pending:
                continue
            if self._try_set_rna_scalar(rna_owner, prop_name, pending[prop_name]):
                del pending[prop_name]

        # Multi-pass restore lets earlier assignments unlock enum options.
        for _ in range(4):
            progressed = False
            for prop_name in list(pending.keys()):
                if self._try_set_rna_scalar(rna_owner, prop_name, pending[prop_name]):
                    del pending[prop_name]
                    progressed = True
            if not progressed:
                break

        if pending:
            return

    def _snapshot_curve_mapping(self, view_settings):
        """Snapshot color-management curve mapping points for later restoration."""
        try:
            if not view_settings or not hasattr(view_settings, "curve_mapping"):
                return None
            cm = view_settings.curve_mapping
            curves_data = []
            for curve in cm.curves:
                points = []
                for point in curve.points:
                    try:
                        handle_type = getattr(point, "handle_type", None)
                    except Exception:
                        handle_type = None
                    points.append(
                        (float(point.location[0]), float(point.location[1]), handle_type)
                    )
                curves_data.append(points)
            return curves_data
        except Exception:
            return None

    def _restore_curve_mapping(self, view_settings, snapshot, debug_prefix=""):
        """Restore color-management curve mapping from snapshot."""
        if not snapshot:
            return
        try:
            if not view_settings or not hasattr(view_settings, "curve_mapping"):
                return
            cm = view_settings.curve_mapping
            curves = cm.curves
            if len(snapshot) != len(curves):
                return

            for curve_idx, points_data in enumerate(snapshot):
                curve = curves[curve_idx]
                # Keep endpoints and resize interior points to match saved count.
                while len(curve.points) > len(points_data) and len(curve.points) > 2:
                    curve.points.remove(curve.points[-2])
                while len(curve.points) < len(points_data):
                    curve.points.new(0.5, 0.5)

                for point_idx, (x, y, handle_type) in enumerate(points_data):
                    point = curve.points[point_idx]
                    point.location = (x, y)
                    if handle_type:
                        try:
                            point.handle_type = handle_type
                        except Exception:
                            pass

            try:
                cm.update()
            except Exception:
                pass
        except Exception:
            pass
    
    def _find_view3d_context(self, context):
        """Find a valid VIEW_3D area, space, and WINDOW region."""
        try:
            preferred_area = None
            try:
                from .modal_gallery import VIEW3D_OT_thumbnail_gallery
                preferred_area = VIEW3D_OT_thumbnail_gallery._context_area
            except Exception:
                preferred_area = None

            return utils.find_view3d_override_context(context, preferred_area=preferred_area)
        except Exception as e:
            print(f"[ViewPilot] Error finding 3D view context: {e}")
            return None, None, None
    
    def _load_from_file(self, filepath, image_name):
        """Load the rendered PNG and prepare it for display."""
        try:
            if not filepath or not os.path.exists(filepath):
                print(f"[ViewPilot] Thumbnail file missing: {filepath}")
                return False

            img = bpy.data.images.get(image_name)
            if img:
                # Rebuild image datablock from file to avoid stale source path
                # issues when re-packing existing thumbnails.
                bpy.data.images.remove(img)

            img = bpy.data.images.load(filepath, check_existing=False)
            img.name = image_name
            img.filepath = filepath
            img.filepath_raw = filepath
            
            # Mark as Non-Color to prevent Blender from re-interpreting the data.
            # The PNG was rendered with Standard transform (sRGB output), so color
            # data is already correct. If we left this as 'sRGB', Blender would
            # apply an additional linearization on access, making colors darker.
            # The galleries then apply their own display transform via save_render().
            img.colorspace_settings.name = 'Non-Color'
            img.use_fake_user = True
            
            # Pack so it persists with .blend file.
            try:
                img.pack()
            except Exception as e_pack:
                print(f"[ViewPilot] Thumbnail pack failed ({image_name}): {e_pack}")
                return False

            return True
        except Exception as e:
            print(f"[ViewPilot] Error loading thumbnail from file: {e}")
            return False


# Module-level instance
_renderer = None


def get_renderer(size=256):
    """Get or create the thumbnail renderer."""
    global _renderer
    if _renderer is None or _renderer.size != size:
        _renderer = ThumbnailRenderer(size)
    return _renderer


def generate_thumbnail(context, saved_view, name_suffix=None, refresh_preview=True):
    """Generate a thumbnail for a saved view."""
    if name_suffix is None:
        name_suffix = saved_view.name
    
    image_name = f".VP_Thumb_{name_suffix}"
    renderer = get_renderer()
    result = renderer.render_from_view_data(context, saved_view, image_name)
    
    # Refresh panel icon preview for single-thumbnail operations.
    if result and refresh_preview:
        try:
            from .preview_manager import refresh_view_preview
            refresh_view_preview(name_suffix)
        except Exception:
            pass  # Panel gallery refresh is optional
    
    return result


def delete_thumbnail(view_name):
    """Delete the thumbnail image for a saved view."""
    image_name = f".VP_Thumb_{view_name}"
    img = bpy.data.images.get(image_name)
    if img:
        bpy.data.images.remove(img)
    
    # Remove preview mapping and invalidate panel gallery cache.
    try:
        from .preview_manager import remove_view_preview
        remove_view_preview(view_name)
    except Exception:
        pass
