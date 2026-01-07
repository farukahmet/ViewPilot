[Documentation WIP]

# ViewPilot

**Control, capture, and recall exactly what you see in your viewport.**

ViewPilot gives you precise control over your 3D viewport and lets you save views as navigable bookmarks with thumbnail previews.

---

## Main features
- Allow camera manipulation without having to select them. 
	- Minor: Global space. Local space. Turntable. Back Up to Wall. 
- Create cameras: that replicate what your see exactly, down to the sensor framing
- History: Trace your steps back
- Views: Remember what you were looking at, where you were looking at it from, how you were looking at it
	- Emphasize: Scenes + ViewLayers!
- Panels: Customize how and where you wanna access ViewPilot. Customize the panels themselves!

---
## Features

### 🎯 Viewport Control
- **Transform Controls** — Manipulate location, rotation, and zoom with precise numeric input
- **Screen-Space Transforms** — Shift X/Y for dolly-style camera moves
- **Orbit Mode** — Turntable-style rotation around selection
- **Perspective Toggle** — Quick switch between perspective and orthographic
- **Lens Controls** — Adjust focal length, field of view, clip start/end

### 📸 Saved Views
- **Save unlimited views** — Capture viewport position, rotation, zoom, and lens settings
- **Thumbnail Gallery** — Visual filmstrip overlay showing all saved views
- **One-click navigation** — Jump to any saved view instantly
- **Ghost indicators** — See when your current view differs from a saved view ( *View Name* )

### 🎬 "Remember" System
Each saved view can selectively remember:
- **Perspective** — Camera position and orientation
- **Shading** — Viewport shading mode and settings
- **Overlays** — Overlay visibility states
- **Composition** — Active Scene and View Layer

### 📷 Camera Creation
- **Create camera from view** — Instantly create a scene camera matching your current viewport
- **Auto-naming** — Configurable camera naming with prefixes
- **Camera collection** — Optionally organize cameras in a dedicated collection

### 🕰️ View History
- **Automatic history tracking** — Navigate back and forward through viewport changes
- **Configurable history size** — Control how many states to remember

---

## Access Points

ViewPilot is available in **four locations** (all configurable):

| Location           | Access                             | Best For                        |
| ------------------ | ---------------------------------- | ------------------------------- |
| **Popup**          | `Shift+Z` (customizable)           | Quick access anywhere           |
| **Header Popover** | Click ViewPilot button             | Persistent access while working |
| **N-Panel**        | View tab → ViewPilot               | Full panel integration          |
| **Topbar**         | Next to Scene/View Layer dropdowns | Saved views quick access        |

---

## Thumbnail Gallery

The filmstrip overlay provides:
- **Visual preview** of all saved views
- **Click to navigate** — left-click any thumbnail
- **Right-click menu** — rename, delete, update, toggle "Remember" options
- **Action buttons** — Refresh all, Reorder views, Close gallery
- **Auto-start option** — Gallery can open automatically on file load

---

## Customization

ViewPilot is highly customizable through addon preferences:

### UI Visibility
- Enable/disable each access point independently
- Show/hide individual panel sections (History, Lens, Transform, etc.)
- Configure popup width

### Defaults for New Views
- Set which "Remember" toggles are enabled by default
- Choose default lens unit (Field of View vs Focal Length)

### Camera Creation
- Custom camera name prefix
- Use dedicated camera collection (on/off)
- Passepartout opacity
- Show/hide passepartout

### History
- Maximum history size
- Settle delay before recording

### Gallery
- Start gallery automatically on file load
- Maximum thumbnail size

---

## Tips

- **Create cameras efficiently** — Use ViewPilot to compose your shot, then instantly create a matching camera
- **Organize presentations** — Save key views for client presentations or animation planning
- **Fast iteration** — Jump between saved views to compare compositions
- **Non-destructive** — Saved views don't modify your scene; they're just viewport bookmarks

---

## Requirements

- Blender 4.0+

---

## Installation

1. Download the addon
2. In Blender: Edit → Preferences → Add-ons → Install...
3. Select the downloaded file
4. Enable "ViewPilot" in the addon list

---

## Shortcuts

Default shortcuts (customizable in Blender Keymap settings):

| Action                   | Shortcut           |
| ------------------------ | ------------------ |
| Open ViewPilot Popup     | `Shift+Z`          |
| Previous/Next Saved View | `Alt+Left/Right`   |
| Save Current View        | `Ctrl+Alt+Down`    |
| Create Camera from View  | `Ctrl+Alt+Up`      |
| History Back/Forward     | (Unset by default) |

---

## License

[Add your license here]

---

## Credits

Created by **Faruk Ahmet**
