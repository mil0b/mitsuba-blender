import os

if "bpy" in locals():
    import importlib
    if "export_context" in locals():
        importlib.reload(export_context)
    if "materials" in locals():
        importlib.reload(materials)
    if "geometry" in locals():
        importlib.reload(geometry)
    if "lights" in locals():
        importlib.reload(lights)
    if "camera" in locals():
        importlib.reload(camera)

import bpy

from . import export_context
from . import materials
from . import geometry
from . import lights
from . import camera

def _dict_to_xml_node(data, name=None, indent=0):
    '''Recursively convert a Mitsuba scene dict to XML lines.'''
    lines = []
    pad = '    ' * indent

    if not isinstance(data, dict):
        return lines

    plugin_type = data.get('type')
    tag = name if name else 'scene'

    if plugin_type:
        attrs = f' type="{plugin_type}"'
        if 'id' in data:
            attrs += f' id="{data["id"]}"'
        children = {k: v for k, v in data.items() if k not in ('type', 'id')}
        if not children:
            lines.append(f'{pad}<{tag}{attrs}/>')
        else:
            lines.append(f'{pad}<{tag}{attrs}>')
            for k, v in children.items():
                lines.extend(_dict_value_to_xml(k, v, indent + 1))
            lines.append(f'{pad}</{tag}>')
    elif name is None:
        # Root scene node
        lines.append(f'{pad}<scene version="3.0.0">')
        for k, v in data.items():
            if k != 'type':
                lines.extend(_dict_value_to_xml(k, v, indent + 1))
        lines.append(f'{pad}</scene>')

    return lines

def _dict_value_to_xml(name, value, indent):
    pad = '    ' * indent
    if isinstance(value, dict):
        return _dict_to_xml_node(value, name=name, indent=indent)
    elif isinstance(value, bool):
        return [f'{pad}<boolean name="{name}" value="{str(value).lower()}"/>']
    elif isinstance(value, int):
        return [f'{pad}<integer name="{name}" value="{value}"/>']
    elif isinstance(value, float):
        return [f'{pad}<float name="{name}" value="{value}"/>']
    elif isinstance(value, str):
        return [f'{pad}<string name="{name}" value="{value}"/>']
    elif isinstance(value, (list, tuple)) and len(value) in (3, 4):
        vals = ', '.join(str(x) for x in value)
        tag = 'rgb' if len(value) == 3 else 'spectrum'
        return [f'{pad}<{tag} name="{name}" value="{vals}"/>']
    return []

def _write_mitsuba_xml(scene_data, path):
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    lines.extend(_dict_to_xml_node(scene_data))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


class SceneConverter:
    '''
    Converts a blender scene to a Mitsuba-compatible dict.
    Either save it as an XML or load it as a scene.
    '''
    def __init__(self, render=False):
        self.export_ctx = export_context.ExportContext()
        self.use_selection = False # Only export selection
        self.ignore_background = True
        self.render = render

    def set_path(self, name, split_files=False):
        if not self.render:
            self.xml_output_path = name
            self.xml_split_files = split_files
        # Give the path to the export context, for saving meshes and files
        self.export_ctx.directory, _ = os.path.split(name)

    def scene_to_dict(self, depsgraph, window_manager=None):
        # Switch to object mode before exporting stuff, so everything is defined properly
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        #depsgraph = context.evaluated_depsgraph_get()
        self.export_ctx.deg = depsgraph

        b_scene = depsgraph.scene #TODO: what if there are multiple scenes?
        if b_scene.render.engine == 'MITSUBA':
            integrator = getattr(b_scene.mitsuba.available_integrators,b_scene.mitsuba.active_integrator).to_dict()
        else:
            integrator = {
                'type':'path',
                'max_depth': b_scene.cycles.max_bounces
            }
        self.export_ctx.data_add(integrator)

        materials.export_world(self.export_ctx, b_scene.world, self.ignore_background)

        # Establish list of particle objects
        particles = []
        for particle_sys in bpy.data.particles:
            if particle_sys.render_type == 'OBJECT':
                particles.append(particle_sys.instance_object.name)
            elif particle_sys.render_type == 'COLLECTION':
                for obj in particle_sys.instance_collection.objects:
                    particles.append(obj.name)

        progress_counter = 0
        # Main export loop
        for object_instance in depsgraph.object_instances:
            if window_manager is not None:
                window_manager.progress_update(progress_counter)
            progress_counter += 1

            if self.use_selection:
                #skip if it's not selected or if it's an instance and the parent object is not selected
                if not object_instance.is_instance and not object_instance.object.original.select_get():
                    continue
                if (object_instance.is_instance and object_instance.object.parent
                    and not object_instance.object.parent.original.select_get()):
                    continue

            evaluated_obj = object_instance.object
            object_type = evaluated_obj.type
            #type: enum in ['MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'ARMATURE', 'LATTICE', 'EMPTY', 'GPENCIL', 'CAMERA', 'LIGHT', 'SPEAKER', 'LIGHT_PROBE'], default 'EMPTY', (readonly)
            if evaluated_obj.hide_render or (object_instance.is_instance
                and evaluated_obj.parent and evaluated_obj.parent.original.hide_render):
                self.export_ctx.log("Object: {} is hidden for render. Ignoring it.".format(evaluated_obj.name), 'INFO')
                continue#ignore it since we don't want it rendered (TODO: hide_viewport)
            if object_type in {'MESH', 'FONT', 'SURFACE', 'META'}:
                geometry.export_object(object_instance, self.export_ctx, evaluated_obj.name in particles)
            elif object_type == 'CAMERA':
                # When rendering inside blender, export only the active camera
                if (self.render and evaluated_obj.name_full == b_scene.camera.name_full) or not self.render:
                    camera.export_camera(object_instance, b_scene, self.export_ctx)
            elif object_type == 'LIGHT':
                lights.export_light(object_instance, self.export_ctx)
            else:
                self.export_ctx.log("Object: %s of type '%s' is not supported!" % (evaluated_obj.name_full, object_type), 'WARN')

    def dict_to_xml(self):
        _write_mitsuba_xml(self.export_ctx.scene_data, self.xml_output_path)

    def dict_to_scene(self):
        from mitsuba import load_dict
        return load_dict(self.export_ctx.scene_data)
