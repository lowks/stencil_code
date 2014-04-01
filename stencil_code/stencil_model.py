import ast
from ctree.dotgen import DotGenVisitor


class StencilModelNode(ast.AST):
    _fields = ['base_node']

    def __init__(self, base_node=None):
        self.base_node = base_node
        super(StencilModelNode, self).__init__()

    def _to_dot(self):
        return StencilModelDotGen.visit(self)


class InteriorPointsLoop(StencilModelNode):
    def __init__(self, target=None, body=[]):
        self.target = target
        self.body = body


class NeighborPointsLoop(StencilModelNode):
    def __init__(self, neighbor_id=None, grid_name=None, neighbor_target=None, body=[]):
        self.neighbor_id = neighbor_id
        self.grid_name = grid_name
        self.neighbor_target = neighbor_target
        self.body = body


class MathFunction(StencilModelNode):
    def __init__(self, func=None, args=[]):
        self.func = func
        self.args = args


class StencilModelDotGen(DotGenVisitor):
    def label_InteriorPointsLoop(self, node):
        return r"%s" % "InteriorPointsLoop"

    def label_NeighborPointsLoop(self, node):
        return r"%s" % "NeighborPointsLoop"

    def label_MathFunction(self, node):
        return r"%s" % "MathFunction"
