
import ast

from ctree.visitors import NodeTransformer
from stencil_model import*


class PythonToStencilModel(NodeTransformer):
    def visit_For(self, node):
        node.body = list(map(self.visit, node.body))
        if type(node.iter) is ast.Call and \
           type(node.iter.func) is ast.Attribute:
            if node.iter.func.attr is 'interior_points':
                return InteriorPointsLoop(target=node.target.id,
                                          body=node.body)
            elif node.iter.func.attr is 'neighbors':
                return NeighborPointsLoop(
                    neighbor_id=node.iter.args[1].n,
                    grid_name=node.iter.func.value.id,
                    neighbor_target=node.target.id,
                    body=node.body
                )
        return node

    def visit_FunctionCall(self, node):
        node.args = list(map(self.visit, node.args))
        if str(node.func) == 'distance' or str(node.func) == 'int':
            return MathFunction(func=node.func, args=node.args)
        return node
