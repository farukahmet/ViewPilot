"""Debug helpers for ViewPilot.

This module is intentionally small and self-contained so it can be removed easily
after refactors are complete.
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from typing import DefaultDict, Iterator

import bpy


_counters: DefaultDict[str, int] = defaultdict(int)
_timing_total_ms: DefaultDict[str, float] = defaultdict(float)
_timing_count: DefaultDict[str, int] = defaultdict(int)


def _get_prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return None


def enabled() -> bool:
    prefs = _get_prefs()
    return bool(getattr(prefs, "debug_enabled", False))


def inc(name: str, amount: int = 1) -> None:
    if not enabled():
        return
    _counters[name] += amount


@contextmanager
def timed(name: str) -> Iterator[None]:
    if not enabled():
        yield
        return

    start = time.perf_counter()
    try:
        yield
    finally:
        dt_ms = (time.perf_counter() - start) * 1000.0
        _timing_total_ms[name] += dt_ms
        _timing_count[name] += 1


def log(message: str) -> None:
    if not enabled():
        return
    print(f"[ViewPilot][Debug] {message}")


def reset_stats() -> None:
    _counters.clear()
    _timing_total_ms.clear()
    _timing_count.clear()


def dump_stats(force: bool = False) -> None:
    if not force and not enabled():
        return

    print("\n[ViewPilot][Debug] ===== Stats =====")

    if _counters:
        print("[ViewPilot][Debug] Counters:")
        for k in sorted(_counters.keys()):
            print(f"  - {k}: {_counters[k]}")

    if _timing_count:
        print("[ViewPilot][Debug] Timings (avg ms, calls):")
        for k in sorted(_timing_count.keys()):
            calls = _timing_count[k]
            total = _timing_total_ms[k]
            avg = (total / calls) if calls else 0.0
            print(f"  - {k}: {avg:.3f} ms avg ({calls} calls)")

    if not _counters and not _timing_count:
        print("[ViewPilot][Debug] (no data yet)")


class VIEWPILOT_OT_debug_print_stats(bpy.types.Operator):
    bl_idname = "viewpilot.debug_print_stats"
    bl_label = "Print Debug Stats"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        dump_stats(force=True)
        self.report({'INFO'}, "Printed ViewPilot debug stats to console")
        return {'FINISHED'}


class VIEWPILOT_OT_debug_reset_stats(bpy.types.Operator):
    bl_idname = "viewpilot.debug_reset_stats"
    bl_label = "Reset Debug Stats"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        reset_stats()
        self.report({'INFO'}, "Reset ViewPilot debug stats")
        return {'FINISHED'}


_classes = [
    VIEWPILOT_OT_debug_print_stats,
    VIEWPILOT_OT_debug_reset_stats,
]


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
