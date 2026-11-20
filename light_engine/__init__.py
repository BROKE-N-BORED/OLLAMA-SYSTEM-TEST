"""The Light Engine, vendored into OLLAMA SYSTEM TEST (v26).

VENDORED - copied from Reblend v0.2-pkl (Joshua Lofland's own
code; Reblend is his 3D app), the light_engine package of the
tree at ~/workspace/reblend-v02-pkl-build/reblend/light_engine.
OST's Surface tab renders its live decision surface with this
engine when OpenGL is available; OST does not depend on the
Reblend repo at runtime - this copy is the whole dependency.

What was taken: the import closure of light_engine.scene and
light_engine.viewport, nothing else -
  animation.py, geometry.py, meshfile.py, particles.py,
  scene.py, shaders.py, style.py, textures.py, viewport.py
(plus this __init__.py). NOT taken: window.py, dial.py,
wheel.py, inspector_data.py, packs.py, pack_api.py, updater.py,
video.py, meshio.py, meshio_glb.py, meshio_obj.py - the app
shell, the pack system, the updater and the mesh file exporters
are Reblend-the-app's business, not the renderer's.

The copied files are byte-identical to the v0.2-pkl source.
OST-side adaptations live in ollama_system_test.py, in the
SurfaceViewport subclass (navy clear colour, no floor grid,
no modal shader-error dialog, gizmo suppressed, projection
picking of the surface's orbs) - the engine files themselves
are not edited, so a future Reblend version can be re-vendored
by copying the same nine files over these.
"""
# The version of the Reblend tree this copy was vendored from.
__version__ = "0.2-pkl"
