# Phones and tablets

The shared show and the sandbox work on desktops, tablets and phones. The interface rearranges its layout by device class; switching class
or rotating the screen only rearranges the layout without rebuilding the viewport or the realtime connection, and the selected entity, camera
and panel state are kept.

## Device classes

| Class | Rule (CSS pixels, re-evaluated on rotation) | Layout | Input |
|---|---|---|---|
| Phone | Width < 768, or short side < 600 (phone in landscape) | One full-screen window with a bottom tab bar (a vertical tab bar on the left in landscape) | Touch |
| Tablet | 768 ≤ width < 1280 and short side ≥ 600 | Commander layout in landscape; viewport on top and a drawer below in portrait | Touch; desktop shortcuts with an external keyboard |
| Desktop | Width ≥ 1280 | Floating layout | Mouse and keyboard; touch enhancements on touch screens |
| Too narrow | Width < 360 | A prompt to rotate the device or enlarge the window | — |

## Tablet: landscape commander

The 3D viewport is the main area, with dockable side panels: the left panel lists the fleet, targets and nests, and the right panel shows
details of the selected entity (sensors and perception readings). Docked panels take width from the viewport; floating panels overlay it,
and dragging a panel header switches between the two. The bottom task bar shows task state and progress, quick commands for the selected
drone (go to, hover, return home, watch), rate, timeline and the alert count. Two virtual sticks appear during manual control.

In portrait the viewport takes about the top 60%, with a snapping drawer below (snap points at 25%, 50% and 90%) holding four pages: fleet,
tasks, targets and details.

## Phone: five tabs

The bottom tab bar holds **Viewport, Fleet, Tasks, Targets and Alerts**. Only one window is shown at a time, and each window keeps its scroll
position and selection; rendering drops to 10 fps while the viewport window is not shown.

Sandbox operators on a phone use **quick commands**: (1) select drones (from the list or by tapping in the viewport); (2) choose a command:
go to (tap a ground point in the viewport), hover (loiter for fixed-wing), return home, or watch (choose a target to create a point watch
task, with station and keep-out computed by the tasking service); (3) confirm the command to be issued. Each command takes at most 4 taps.
Phones offer no virtual sticks, and the command button is hidden for read-only visitors.

## Touch gestures

| Gesture | Action |
|---|---|
| One-finger drag | Rotate the view (pan in top-down mode) |
| Pinch | Zoom, anchored at the midpoint of the fingers |
| Two-finger drag | Pan |
| Two-finger rotate | Yaw rotation |
| Long press, 500 ms | Context menu: fly here, place a target here (sandbox), measure distance, copy coordinates |
| Tap | Select an entity, or clear the selection on empty ground |
| Double tap | Focus on the entity or ground point |

On touch devices every interactive control has a hit area of at least 44 × 44 CSS pixels. Page zoom stays available.

## Quality

Mobile devices choose quality by device class:

| Item | Phone | Tablet |
|---|---|---|
| Target frame rate | 30 fps | 60 fps, 30 fps when degraded |
| Drawing pixel cap | 1.0 MP | 2.0 MP |
| Point budget | 0.5 million points | 1 million points |
| Field view points | ≤ 2048 | ≤ 4096 |
| Fleet state subscription | 5 Hz, 30 Hz for the selected drone | 10 Hz, 30 Hz for the selected drone |

The rendering code is designed for the minimum limits of the WebGL2 specification (for example, it does not depend on floating-point render
targets), so it also runs on mobile GPUs.

## Full screen and home-screen launch

- The "Full screen" button in the top bar uses the browser's full-screen API (Android, desktop and iPadOS); on iPhones without element full
  screen the button is hidden and a hint suggests "Add to Home Screen".
- The site provides a web app manifest: added to the home screen, it launches full screen and starts at the synthetic demo city. The site
  registers no service worker, which avoids conflicts between caching and version updates.
- The tablet commander can lock landscape orientation in full screen or when launched from the home screen; rotating during manual control
  does not interrupt the lease.
- The top bar, tab bar and sticks are inset by the device's safe areas.
