from PIL import Image

from shapely.geometry import CAP_STYLE, JOIN_STYLE

from ... import config
if not config.is_building_docs():
    from qiskit_metal import is_true

from qiskit_metal import draw, Dict
from qiskit_metal.toolbox_metal import math_and_overrides
import qiskit_metal as metal

import math        
import ezdxf
from shapely.geometry import Polygon

from qiskit_metal import draw, Dict
from qiskit_metal.qlibrary.core.base import QComponent
import numpy as np


class MyQComponent2(QComponent):
    # Edit these to define your own tempate options for creation
    # Default drawing options
    default_options = Dict(width='500um',
                           height='300um',
                           pos_x='0um',
                           pos_y='0um',
                           orientation='0',
                           layer='1')
    """Default drawing options"""

    # Name prefix of component, if user doesn't provide name
    component_metadata = Dict(short_name='component',
                             _qgeometry_table_poly='True')
    """Component metadata"""
    def dxf_conf(filename):
        doc = ezdxf.readfile(filename)  # Read the DXF file
        msp = doc.modelspace()  # Get the modelspace of the document
        for entity in msp:
            if entity.dxftype() == 'LWPOLYLINE':
                vertices = [(vertex[0], vertex[1]) for vertex in entity.get_points()]
            elif entity.dxftype() == 'POLYLINE':
                vertices = [(point[0], point[1]) for point in entity.points()]
            print(vertices) #you might want to check and add vertices manually to draw.polygon
            return vertices
        
    
    filename5 = "arrows.dxf"
    vertices5 = dxf_conf(filename5)
    
    filename6 = "cat.dxf"
    vertices6 = dxf_conf(filename6)
    
    filename7 = "Qiskit.dxf"
    vertices7 = dxf_conf(filename7)
    
    filename8 = "star.dxf"
    vertices8 = dxf_conf(filename8)
    

    def make(self):
        """Convert self.options into QGeometry."""
        p = self.parse_options()  # Parse the string options into numbers
        
        
        #flbias pin etched
        face1 = draw.Polygon(self.vertices5) #DRAWING arrows
        face1 = draw.scale(face1,1.5,1.5)
        face1 = draw.translate(face1,5.5,5.25)
        
        
        face2 = draw.Polygon(self.vertices6) #DRAWING cat
        face2 = draw.translate(face2,1.25,0)

        face3 = draw.Polygon(self.vertices7) #DRAWING Qiskit
        face3 = draw.translate(face3,0,6.5)
        
        face4 = draw.Polygon(self.vertices8) #DRAWING star
        face4 = draw.scale(face4,0.6,0.6)
        face4 = draw.translate(face4,4.75,0.5)
        
        
        face11 = draw.union(face1,face2,face3,face4)
        
        face11
        geom11 = {'my_polygon': face11}
        self.add_qgeometry('poly', geom11, layer=p.layer, subtract=False)
