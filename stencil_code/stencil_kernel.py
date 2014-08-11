"""
This version was taken from the stencil_specializer project and has all asp
stuff removed in order to work on a direct c-tree llvm implementation

The main driver, intercepts the kernel() call and invokes the other components.

Stencil kernel classes are subclassed from the StencilKernel class
defined here. At initialization time, the text of the kernel() method
is parsed into a Python AST, then converted into a StencilModel by
stencil_python_front_end. The kernel() function is replaced by
shadow_kernel(), which intercepts future calls to kernel().

During each call to kernel(), stencil_unroll_neighbor_iter is called
to unroll neighbor loops, stencil_convert is invoked to convert the
model to C++, and an external compiler tool is invoked to generate a
binary which then efficiently completes executing the call. The binary
is cached for future calls.
"""

import math

from collections import namedtuple

from ctree.jit import LazySpecializedFunction, ConcreteSpecializedFunction
from ctree.c.nodes import FunctionDecl
from ctree.ocl.nodes import OclFile
import ctree.np
ctree.np  # Make PEP8 happy
from ctree.frontend import get_ast
from backend.omp import StencilOmpTransformer
from backend.ocl import StencilOclTransformer, StencilOclSemanticTransformer
from backend.c import StencilCTransformer
# from stencil_grid import StencilGrid
from python_frontend import PythonToStencilModel
# import optimizer as optimizer
from ctypes import byref, c_float, CFUNCTYPE, c_void_p, POINTER, sizeof
import pycl as cl
from pycl import (
    clCreateProgramWithSource, buffer_from_ndarray, buffer_to_ndarray, cl_mem,
    localmem, clEnqueueNDRangeKernel
)
import numpy as np
import ast
import operator
import itertools


class StencilFunction(ConcreteSpecializedFunction):
    """StencilFunction

    The standard concrete specialized function that is returned when using the
    C or OpenMP backend.
    """

    def finalize(self, tree, entry_name, entry_type, output):
        """finalize

        :param tree: A project node containing any files to be compiled for this
                     specialized function.
        :type tree: Project
        :param entry_name: The name of the function that will be the entry point
                           to the compiled project.
        :type entry_name: str
        :param entry_type: The type signature of the function described by
                           `entry_name`.
        :type entry_type: CFUNCTYPE
        """
        self.output = output
        self._c_function = self._compile(entry_name, tree, entry_type)
        return self

    def __call__(self, *args):
        """__call__

        :param *args: Arguments to be passed to our C function, the types should
                      match the types specified by the `entry_type` that was
                      passed to :attr: `finalize`.

        """
        # TODO: provide stronger type checking to give users better error
        # messages.
        duration = c_float()
        args += (self.output, byref(duration))
        self._c_function(*args)
        return self.output


class OclStencilFunction(ConcreteSpecializedFunction):
    """OclStencilFunction

    The ConcreteSpecializedFunction used by the OpenCL backend.  Allows us to
    leverage pycl for handling numpy arrays and buffers cleanly.
    """
    def __init__(self):
        """__init__
        Creates a context and queue that can be reused across calls to this
        function.
        """
        devices = cl.clGetDeviceIDs()
        self.device = devices[-1]
        self.context = cl.clCreateContext([self.device])
        self.queue = cl.clCreateCommandQueue(self.context)

    def finalize(self, kernel, global_size, ghost_depth, output_grid):
        """finalize

        :param kernel: The stencil kernel generated by transform which will be
                       used in __call__.
        :type kernel: cl_kernel
        :param global_size: The global work size for the kernel calculated in
                            transform.
        :type global_size: int or sequence of ints
        :rtype: OclStencilFunction
        """
        self.kernel = kernel
        self.global_size = global_size
        self.ghost_depth = ghost_depth
        self.output_grid = output_grid
        return self

    def __call__(self, *args):
        """__call__

        :param *args:
        """
        self.kernel.argtypes = tuple(
            cl_mem for _ in args[:-1]
        ) + (localmem, )
        bufs = []
        events = []
        for index, arg in enumerate(args[:-1]):
            buf, evt = buffer_from_ndarray(self.queue, arg, blocking=False)
            # evt.wait()
            events.append(evt)
            bufs.append(buf)
            self.kernel.setarg(index, buf, sizeof(cl_mem))
        cl.clWaitForEvents(*events)
        if self.device.type == cl.cl_device_type.CL_DEVICE_TYPE_GPU:
            local = 8
        else:
            local = 1
        localmem_size = reduce(
            operator.mul,
            (local + (self.ghost_depth * 2) for _ in range(args[0].ndim)),
            sizeof(c_float)
        )
        self.kernel.setarg(
            len(args) - 1, localmem(localmem_size), localmem_size
        )
        evt = clEnqueueNDRangeKernel(
            self.queue, self.kernel, self.global_size,
            tuple(local for _ in range(args[0].ndim))
        )
        evt.wait()
        buf, evt = buffer_to_ndarray(
            self.queue, bufs[-1], args[-2]
        )
        evt.wait()
        for mem in bufs:
            del mem

        return buf

    def __del__(self):
        del self.context
        del self.queue


StencilArgConfig = namedtuple(
    'StencilArgConfig', ['size', 'dtype', 'ndim', 'shape']
)


class SpecializedStencil(LazySpecializedFunction):
    backend_dict = {"c": StencilCTransformer,
                    "omp": StencilOmpTransformer,
                    "ocl": StencilOclTransformer,
                    "opencl": StencilOclTransformer}

    def __init__(self, kernel, backend, testing=False):
        """
        Initializes an instance of a SpecializedStencil. This function
        inherits from ctree's LazySpecializedFunction. When the specialized
        function is called, it will either load a cached version, or generate
        a new version using the kernel method's AST and the passed parameters
        . The tuning configurations are defined in get_tuning_driver. The
        arguments to the specialized function call are passed to
        args_to_subconfig where they can be processed to a form usable by the
        specializer. For more information consult the ctree docs.

        :param func: Stencil Kernel function to be specialized.
        :param input_grids: List of input grids passed as arguments to stencil.
        :param output_grid: Output grid passed to stencil.
        :param kernel: The Kernel object containing the kernel function.
        :param testing: Optional - whether or not we are in testing mode
        """
        self.testing = testing
        self.kernel = kernel
        self.backend = self.backend_dict[backend]
        self.output = None
        super(SpecializedStencil, self).__init__(get_ast(kernel.kernel))

    def args_to_subconfig(self, args):
        """
        Generates a configuration for the transform method based on the
        arguments passed into the stencil.

        :param args: StencilGrid instances being passed as params.
        :return: Tuple of information about the StencilGrids
        """
        self.args = args
        return tuple(
            StencilArgConfig(len(arg), arg.dtype, arg.ndim, arg.shape)
            for arg in args
        )

    # def get_tuning_driver(self):
    #     """
    #     Returns the tuning driver used for this Specialized Function.
    #     Initializes a brute force tuning driver that explores the space of
    #     loop unrolling factors as well as cache blocking factors for each
    #     dimension of our input StencilGrids.

    #     :return: A BruteForceTuning driver instance
    #     """
    #     from ctree.tune import (
    #         BruteForceTuningDriver,
    #         IntegerParameter,
    #         MinimizeTime
    #     )

    #     params = [IntegerParameter("unroll_factor", 1, 4)]
    #     for d in range(len(self.input_grids[0].shape) - 1):
    #         params.append(IntegerParameter("block_factor_%s" % d, 4, 8))
    #     return BruteForceTuningDriver(params, MinimizeTime())

    def transform(self, tree, program_config):
        """
        Transforms the python AST representing our un-specialized stencil
        kernel into a c_ast which can be JIT compiled.

        :param tree: python AST of the kernel method.
        :param program_config: The configuration generated by args_to_subconfig
        :return: A ctree Project node, and our entry point type signature.
        """
        arg_cfg, tune_cfg = program_config
        output = self.generate_output(self.args)
        param_types = [
            np.ctypeslib.ndpointer(arg.dtype, arg.ndim, arg.shape)
            for arg in arg_cfg + (output,)
        ]

        if self.backend == StencilOclTransformer:
            param_types.append(param_types[0])
        else:
            param_types.append(POINTER(c_float))

        # block_factors = [2**tune_cfg['block_factor_%s' % d] for d in
        #                  range(len(self.input_grids[0].shape) - 1)]
        # unroll_factor = 2**tune_cfg['unroll_factor']
        unroll_factor = 0

        for transformer in [PythonToStencilModel(),
                            self.backend(self.args,
                                         output,
                                         self.kernel
                                         )]:
            tree = transformer.visit(tree)
        # first_For = tree.find(For)
        # TODO: let the optimizer handle this? Or move the find inner most loop
        # code somewhere else?
        # inner_For = optimizer.FindInnerMostLoop().find(first_For)
        # self.block(inner_For, first_For, block_factor)
        # TODO: If should unroll check
        # optimizer.unroll(inner_For, unroll_factor)
        entry_point = tree.find(FunctionDecl, name="stencil_kernel")
        # TODO: This should be handled by the backend
        # if self.backend != StencilOclTransformer:
        for index, _type in enumerate(param_types):
            entry_point.params[index].type = _type()
        # entry_point.set_typesig(kernel_sig)
        # TODO: This logic should be provided by the backends
        if self.backend == StencilOclTransformer:
            entry_point.set_kernel()
            kernel = OclFile("kernel", [entry_point])
            return kernel
        else:
            if self.args[0].shape[len(self.args[0].shape) - 1] \
                    >= unroll_factor:
                # FIXME: Lack of parent pointers breaks current loop unrolling
                # first_For = tree.find(For)
                # inner_For = optimizer.FindInnerMostLoop().find(first_For)
                # inner, first = optimizer.block_loops(inner_For, tree,
                #                                      block_factors + [1])
                # first_For.replace(first)
                # optimizer.unroll(tree, inner_For, unroll_factor)
                pass

        # import ast
        # print(ast.dump(tree))
        # TODO: This should be done in the visitors
        tree.files[0].config_target = 'omp'
        return tree

    def finalize(self, tree, program_config):
        arg_cfg, tune_cfg = program_config
        param_types = [
            np.ctypeslib.ndpointer(arg.dtype, arg.ndim, arg.shape)
            for arg in arg_cfg + (self.output, )
        ]

        if self.backend == StencilOclTransformer:
            param_types.append(param_types[0])
            fn = OclStencilFunction()
            program = clCreateProgramWithSource(fn.context,
                                                tree.codegen()).build()
            stencil_kernel_ptr = program['stencil_kernel']
            global_size = tuple(
                dim - 2 * self.kernel.ghost_depth
                for dim in arg_cfg[0].shape
            )
            return fn.finalize(
                stencil_kernel_ptr, global_size,
                self.kernel.ghost_depth, self.output
            )
        else:
            param_types.append(POINTER(c_float))
            kernel_sig = CFUNCTYPE(c_void_p, *param_types)
            fn = StencilFunction()
            return fn.finalize(tree, "stencil_kernel", kernel_sig,
                               self.output)

    def generate_output(self, args):
        if self.output is not None:
            return self.output
        self.output = np.zeros_like(args[0])
        return self.output

class StencilKernel(object):
    backend_dict = {"c": StencilCTransformer,
                    "omp": StencilOmpTransformer,
                    "ocl": StencilOclTransformer,
                    "opencl": StencilOclTransformer,
                    "python": None}

    def __new__(cls, backend="c", testing=False):
        if backend == 'python':
            cls.__call__ = cls.pure_python
            return super(StencilKernel, cls).__new__(cls, backend, testing)
        elif backend in ['c', 'omp', 'ocl']:
            new = super(StencilKernel, cls).__new__(cls, backend, testing)
            return SpecializedStencil(new, backend, testing)

    def __init__(self, backend="c", testing=False):
        """
        Our StencilKernel class wraps an un-specialized stencil kernel
        function.  This class should be sub-classed by the user, and should
        have a kernel method defined.  When initialized, an instance of
        StencilKernel will store the kernel method and replace it with a
        shadow_kernel method, which when called will begin the JIT
        specialization process using ctree's infrastructure.

        :param backend: Optional backend that should be used by ctree.
        Supported backends are c, omp (openmp), and ocl (opencl).
        :param pure_python: Setting this will true will cause the python
        version of the kernel to be preserved.  Any subsequent calls will be
        run in python without any JIT specializiation.
        :param testing: Used for testing.
        :raise Exception: If no kernel method is defined.
        """

        # we want to raise an exception if there is no kernel()
        # method defined.
        try:
            dir(self).index("kernel")
        except ValueError:
            raise Exception("No kernel method defined.")

        self.backend = self.backend_dict[backend]
        self.testing = testing

        self.model = self.kernel

        # self.pure_python = pure_python
        # self.pure_python_kernel = self.kernel
        self.should_unroll = True
        self.should_cacheblock = False
        self.block_size = 1

        # replace kernel with shadow version
        # self.kernel = self.shadow_kernel

        self.specialized_sizes = None

    def pure_python(self, *args):
        output = np.zeros_like(args[0])
        self.kernel(*(args + (output,)))
        return output

    @property
    def constants(self):
        return {}

    def shadow_kernel(self, *args):
        """
        This shadow_kernel method will replace the kernel method that is
        defined in the sub-class of StencilKernel.  If in pure python mode,
        it will execute the kernel in python.  Else, it first checks if we
        have a cached version of the specialized function for the shapes of
        the arguments.  If so, we make a call to that function with our new
        arguments.  If not, we create a new SpecializedStencil with our
        arguments and original kernel method and call it with our arguments.
        :param args: The arguments to our original kernel method.
        :return: Undefined
        """
        output_grid = np.zeros_like(args[0])
        # output_grid = StencilGrid(args[0].shape)
        # output_grid.ghost_depth = self.ghost_depth
        if self.pure_python:
            self.pure_python_kernel(*(args + (output_grid,)))
            return output_grid

        if not self.specialized_sizes or\
                self.specialized_sizes != [y.shape for y in args]:
            self.specialized = SpecializedStencil(
                self.model, args, output_grid, self, self.testing
            )
            self.specialized_sizes = [arg.shape for arg in args]

        duration = c_float()
        # args = [arg.data for arg in args]
        args += (output_grid, byref(duration))
        self.specialized(*args)
        self.specialized.report(time=duration)
        # print("Took %.3fs" % duration.value)
        return output_grid

    def interior_points(self, x):
        dims = (range(self.ghost_depth, dim - self.ghost_depth)
                    for dim in x.shape)
        for item in itertools.product(*dims):
            yield tuple(item)


    def get_semantic_node(self, arg_names, *args):
        class StencilCall(ast.AST):
            _fields = ['params', 'body']

            def __init__(self, function_decl, input_grids, output_grid, kernel):
                self.params = function_decl.params
                self.body = function_decl.defn
                self.function_decl = function_decl
                self.input_grids = input_grids
                self.output_grid = output_grid
                self.kernel = kernel

            def label(self):
                return ""

            def to_dot(self):
                return "digraph mytree {\n%s}" % self._to_dot()

            def _to_dot(self):
                from ctree.dotgen import DotGenVisitor

                return DotGenVisitor().visit(self)

            def add_undef(self):
                self.function_decl.defn[0].add_undef()

            def remove_types_from_decl(self):
                self.function_decl.defn[1].remove_types_from_decl()

            def backend_transform(self, block_padding, local_input):
                return StencilOclTransformer(
                    self.input_grids, self.output_grid, self.kernel,
                    block_padding
                ).visit(self.function_decl)

            def backend_semantic_transform(self, fusion_padding):
                self.function_decl = StencilOclSemanticTransformer(
                    self.input_grids, self.output_grid, self.kernel,
                    fusion_padding
                ).visit(self.function_decl)
                self.body = self.function_decl.defn
                self.params = self.function_decl.params

        func_decl = PythonToStencilModel(arg_names).visit(
            get_ast(self.model)
        ).files[0].body[0]
        return StencilCall(func_decl, args[:-1], args[-1], self)

    def distance(self, x, y):
        """
        default euclidean distance override this to return something
        reasonable for each neighbor cell distance
        :param x: Point represented as a list or tuple
        :param y: Point represented as a list or tuple
        """
        return math.sqrt(sum([(x[i]-y[i])**2 for i in range(0, len(x))]))
