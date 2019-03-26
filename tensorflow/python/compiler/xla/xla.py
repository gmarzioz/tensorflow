# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""xla is an experimental library that provides XLA support APIs."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import contextlib
from six.moves import xrange  # pylint: disable=redefined-builtin

from tensorflow.compiler.jit.ops import xla_ops
from tensorflow.compiler.jit.ops import xla_ops_grad  # pylint: disable=unused-import
from tensorflow.core.framework import attr_value_pb2
from tensorflow.python.distribute import summary_op_util
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.util import compat
from tensorflow.python.util import nest
from tensorflow.python.util import tf_inspect
from tensorflow.python.util.tf_export import tf_export

_XLA_COMPILE_ATTR = '_xla_compile_id'
_MAX_WARNING_LINES = 5

# Operations that indicate some error in the users graph. For example, XLA
# computation should not have any Placeholder op.
_BLACKLISTED_OPS = set([
    'Placeholder',
])

# XLA doesn't currently support reading of intermediate tensors, thus some ops
# are not supported.
_UNSUPPORTED_OPS = set([
    'AudioSummary',
    'AudioSummaryV2',
    'HistogramSummary',
    'ImageSummary',
    'MergeSummary',
    'Print',
    'ScalarSummary',
    'TensorSummary',
    'TensorSummaryV2',
])


@tf_export(v1=['xla.experimental.compile'])
def compile(computation, inputs=None):  # pylint: disable=redefined-builtin
  """Builds an operator that compiles and runs `computation` with XLA.

  Args:
    computation: A Python function that builds a computation to apply to the
      input. If the function takes n inputs, 'inputs' should be a list of n
      tensors.

      `computation` may return a list of operations and tensors.  Tensors must
      come before operations in the returned list.  The return value of
      `compile` is a list of tensors corresponding to the tensors from the
      output of `computation`.

      All `Operation`s returned from `computation` will be executed when
      evaluating any of the returned output tensors.
    inputs: A list of inputs or `None` (equivalent to an empty list). Each input
      can be a nested structure containing values that are convertible to
      tensors. Note that passing an N-dimension list of compatible values will
      result in a N-dimention list of scalar tensors rather than a single Rank-N
      tensors. If you need different behavior, convert part of inputs to tensors
      with `tf.convert_to_tensor`.

  Returns:
    Same data structure as if computation(*inputs) is called directly with some
    exceptions for correctness. Exceptions include:
      1) None output: a NoOp would be returned which control-depends on
         computation.
      2) Single value output: A tuple containing the value would be returned.
      3) Operation-only outputs: a NoOp would be returned which
         control-depends on computation.
      TODO(b/121383831): Investigate into removing these special cases.
  """
  # pylint: disable=protected-access
  return _compile_internal(computation, inputs)


class XLACompileContext(control_flow_ops.XLAControlFlowContext):
  """A `ControlFlowContext` for nodes inside an XLA computation cluster.

  THIS IS ONLY FOR TENSORFLOW INTERNAL IMPLEMENTATION, DO NO USE DIRECTLY.

  The primary role of `XLACompileContext` is to mark operators inside a
  xla.compile() computation with attribute "_xla_compile_id=XYZ", where XYZ is
  a unique name.

  `ControlFlowContext` is used to perform the annotation since it integrates
  with Tensorflow constructs like ResourceVariables. For example, if a
  `ResourceVariable` is constructed inside a xla.compile() block, the
  `ResourceVariable` implementation can use
  `with ops.control_dependencies(None)` to build the variable's definition
  outside the compiled computation.
  """

  def __init__(self, name, pivot):
    """Builds a new XLACompileContext.

    Args:
      name: a unique name for the context, used to populate the
        `_xla_compile_id` attribute.
      pivot: a pivot node. Nodes in the XLACompileContext that do not have any
        inputs will have a control dependency on the pivot node. This ensures
        that nodes are correctly included in any enclosing control flow
        contexts.
    """
    super(XLACompileContext, self).__init__()
    self._name = name
    self._name_as_bytes = compat.as_bytes(name)
    self._unsupported_ops = []
    self._pivot = pivot

  def report_unsupported_operations(self):
    if self._unsupported_ops:
      op_str = '\n'.join([
          '  %s (%s)' % (op.type, op.name)
          for op in self._unsupported_ops[:_MAX_WARNING_LINES]
      ])
      logging.warning('%d unsupported operations found: \n%s',
                      len(self._unsupported_ops), op_str)
      if len(self._unsupported_ops) > _MAX_WARNING_LINES:
        logging.warning('... and %d more',
                        len(self._unsupported_ops) - _MAX_WARNING_LINES)

  def _RemoveExternalControlEdges(self, op):
    """Remove any external control dependency on this op."""
    internal_control_inputs = []
    external_control_inputs = []
    for x in op.control_inputs:
      # pylint: disable=protected-access
      is_internal_op = False
      ctxt = x._get_control_flow_context()
      while ctxt is not None:
        if ctxt == self:
          is_internal_op = True
          break
        ctxt = ctxt._outer_context
      if is_internal_op:
        internal_control_inputs.append(x)
      else:
        external_control_inputs.append(x)
      # pylint: enable=protected-access
    # pylint: disable=protected-access
    op._remove_all_control_inputs()
    op._add_control_inputs(internal_control_inputs)
    # pylint: enable=protected-access
    return internal_control_inputs, external_control_inputs

  def AddOp(self, op):
    """Create op in XLACompileContext and notifies outer context recursively."""
    # pylint: disable=protected-access
    if op.type in _BLACKLISTED_OPS:
      logging.error(
          'Operation of type %s (%s) is not supported in XLA. Execution will '
          'fail if this op is used in the graph. ', op.type, op.name)

    # TODO(ycao): Automatically disable summaries instead of reporting them.
    if op.type in _UNSUPPORTED_OPS:
      self._unsupported_ops.append(op)

    if any(x.dtype._is_ref_dtype for x in op.inputs):
      raise NotImplementedError(
          'Non-resource Variables are not supported inside XLA computations '
          '(operator name: %s)' % op.name)

    if _XLA_COMPILE_ATTR in op.node_def.attr:
      raise ValueError('XLA compiled computations cannot be nested, (operator '
                       'name: %s)' % op.name)

    op._set_attr(
        _XLA_COMPILE_ATTR, attr_value_pb2.AttrValue(s=self._name_as_bytes))

    op.graph.prevent_feeding(op)
    op.graph.prevent_fetching(op)

    # Remove any control edges from outer control flow contexts. These may cause
    # mismatched frame errors. An example is when one of op's inputs is
    # generated in a different While control flow context.
    (internal_control_inputs,
     external_control_inputs) = self._RemoveExternalControlEdges(op)

    if not op.inputs:
      # Add a control edge from the control pivot to this op.
      if not internal_control_inputs:
        # pylint: disable=protected-access
        op._add_control_input(self._pivot)
        # pylint: enable=protected-access
    else:
      for index in xrange(len(op.inputs)):
        x = op.inputs[index]
        real_x = self.AddValue(x)
        if real_x != x:
          op._update_input(index, real_x)  # pylint: disable=protected-access

    if external_control_inputs:
      # Use an identity to pull control inputs as data inputs. Note that we
      # ignore ops which don't have outputs. TODO(phawkins): fix that.
      with ops.control_dependencies(None):
        self.Enter()
        external_control_inputs = [
            array_ops.identity(x.outputs[0]).op
            for x in external_control_inputs
            if x.outputs
        ]
        self.Exit()
      # pylint: disable=protected-access
      op._add_control_inputs(external_control_inputs)
      # pylint: enable=protected-access

    # Mark op's outputs as seen by this context and any outer contexts.
    output_names = [x.name for x in op.outputs]
    context = self
    while context is not None:
      # pylint: disable=protected-access
      context._values.update(output_names)
      context = context._outer_context
      # pylint: enable=protected-access

    if self._outer_context:
      self._outer_context.AddInnerOp(op)

  def AddValue(self, val):
    """Add `val` to the current context and its outer context recursively."""
    if val.name in self._values:
      # Use the real value if it comes from outer context.
      result = self._external_values.get(val.name)
      return val if result is None else result

    result = val
    self._values.add(val.name)
    if self._outer_context:
      result = self._outer_context.AddValue(val)
      self._values.add(result.name)

    self._external_values[val.name] = result

    return result

  def AddInnerOp(self, op):
    self.AddOp(op)
    if self._outer_context:
      self._outer_context.AddInnerOp(op)

  @property
  def grad_state(self):
    # Define the gradient loop state associated with the XLACompileContext to
    # be None as the XLACompileContext does not get nested nor does the
    # grad_state outside the XLACompileContext affect the graph inside so the
    # grad_state should be as if this is the top-level gradient state.
    return None

  @property
  def back_prop(self):
    """Forwards to the enclosing while context, if any."""
    if self.GetWhileContext():
      return self.GetWhileContext().back_prop
    return False


def _compile_internal(computation, inputs=None):
  """Builds graph operators that compiles and symbolically executes computation.

  Args:
    computation: A Python function that builds the computation to compile and
      execute.
    inputs: A list of inputs or `None` (equivalent to an empty list). Each input
      can be a nested structure containing values that are convertible to
      tensors. Note that passing an N-dimension list of compatible values will
      result in a N-dimension list of scalar tensors rather than a single Rank-N
      tensors. If you need different behavior, convert part of inputs to tensors
      with `tf.convert_to_tensor`.

  Returns:
    Same data structure as if computation(*inputs) is called directly with some
    exceptions for correctness. Exceptions include: 1) None output 2) Single
    value output 3) Operation-only outputs
  Raises:
    ValueError: If any element in computation outputs is neither an operations
      or a value that can be converted to tensor.
    ValueError: If computation outputs is non-flat and contains any Operations.
    TypeError: If `inputs` is not a list or tuple.
  """
  if inputs is None:
    inputs = []

  if not isinstance(inputs, collections.Sequence):
    raise TypeError('inputs must be a list')

  # Flatten inputs.
  flat_inputs = nest.flatten(inputs)
  # Converts inputs to Tensors.
  flat_inputs = [ops.convert_to_tensor(x) for x in flat_inputs]

  cluster_name = ops.get_default_graph().unique_name('cluster')
  pivot = control_flow_ops.no_op(name=cluster_name + '/pivot')
  context = XLACompileContext(name=cluster_name, pivot=pivot)
  try:
    context.Enter()

    # Add identity ops so even unused inputs are 'consumed' by the
    # computation.
    flat_inputs = [
        array_ops.identity(x, name='input_{}'.format(i))
        for i, x in enumerate(flat_inputs)
    ]

    # Re-pack flat_inputs in same structure as 'inputs'.
    computation_inputs = nest.pack_sequence_as(
        structure=inputs, flat_sequence=flat_inputs)

    # Only resource variables work inside an XLA computation, so turn on
    # resource variables for the computation.
    vscope = variable_scope.get_variable_scope()
    saved_use_resource = vscope.use_resource
    vscope.set_use_resource(True)

    with _disable_summary_context():
      outputs = computation(*computation_inputs)

    # Restore variable scope after computation.
    vscope.set_use_resource(saved_use_resource)

    outputs_is_flat = is_flat(outputs)
    if outputs_is_flat:
      output_tensors, control_deps = _postprocess_flat_outputs(outputs)
    else:
      output_tensors, control_deps = _postprocess_non_flat_outputs(outputs)

    context.ExitResult(output_tensors)
  finally:
    context.report_unsupported_operations()
    context.Exit()

  # When XLA computation returns only operations and no tensors, a NoOp
  # dependent on the operations in outputs is returned. Otherwise final
  # outputs would be empty and there is no way to trigger returned
  # operations.
  if not output_tensors:
    return control_flow_ops.group(control_deps, name='output_0')

  output_tensors = [
      xla_ops.xla_cluster_output(o, name='output{}'.format(i))
      for i, o in enumerate(output_tensors)
  ]

  with ops.control_dependencies(control_deps):
    # Wraps the outputs in identity operators that carries control
    # dependencies.
    output_tensors = [
        array_ops.identity(o, name='output_%d' % i)
        for i, o in enumerate(output_tensors)
    ]

  # If `computation` returned non-flat output structure, pack output tensors
  # back into same structure.
  if not outputs_is_flat:
    output_tensors = nest.pack_sequence_as(
        structure=outputs, flat_sequence=output_tensors)

  return output_tensors


def is_flat(outputs):
  """Checks if outputs is a flat structure.

    Following structures and values are considered flat:
    1) None
    2) A single object
    3) A list or tuple of Tensors/Operations

    The only structures that this function understands are sequences and
    dictionaries.  E.g. this means that if outputs contains a single
    user-defined Object, it is considered to be flat. Errors are raised later on
    if that Object cannot be converted to a Tensor.

  Args:
    outputs: Output from `computation` inside `xla.compile`.

  Returns:
    A boolean indicates whether outputs is flat.
  """
  # If outputs is a list or tuple, check if it has any nested structure. If
  # there is, then outputs is non-flat.
  if isinstance(outputs, collections.Sequence):
    for o in outputs:
      if isinstance(o, collections.Sequence) or isinstance(o, dict):
        return False

  # If outputs is a dict, it is non-flat.
  if isinstance(outputs, dict):
    return False

  # Getting here means either outputs itself is a single non-structured value
  # or it is a flat list of single non-structured values.
  return True


def _postprocess_flat_outputs(outputs):
  """Validates flat outputs and adds back device assignments.

  Args:
    outputs: Output from `computation` inside `xla.compile`.

  Returns:
    Tensors and Operations extracted from outputs.
  """
  # Following code segment is to preserve legacy behavior. Previously we only
  # supported flat outputs and thus for consistency it was nice to convert even
  # single element into a tuple. But now that we support arbitrary output
  # structure, this is no longer necessary.
  # TODO(b/121383831): Migrate all legacy use cases and delete this special
  # case.
  # If the computation returns `None`, make it an empty tuple.
  if outputs is None:
    outputs = tuple()
  # If the computation only returned one value, make it a tuple.
  if not isinstance(outputs, collections.Sequence):
    outputs = (outputs,)

  # Append `no_op` here so that return value of this function always contains
  # at least one op that can trigger XlaLaunch node.
  outputs += (control_flow_ops.no_op(),)
  try:
    outputs = [
        o if isinstance(o, ops.Operation) else ops.convert_to_tensor(o)
        for o in outputs
    ]
  except Exception as e:
    raise ValueError(
        'XLA computation function return values must all either be Operations'
        ' or convertible to Tensors. Got error: "%s"' % str(e))

  # Separates the returned Operations and Tensors.
  output_operations = [o for o in outputs if isinstance(o, ops.Operation)]
  output_tensors = [o for o in outputs if not isinstance(o, ops.Operation)]

  if outputs != output_tensors + output_operations:
    raise ValueError(
        'XLA computation function must return zero or more Tensor values '
        'followed by zero or more Operations.')

  new_output_tensors = []
  for t in output_tensors:
    with ops.device(t.device if t.device else ''):
      new_output_tensors.append(array_ops.identity(t))

  return new_output_tensors, output_operations


def _postprocess_non_flat_outputs(outputs):
  """Validates non-flat outputs and adds back device assignments.

  Args:
    outputs: Output from `computation` inside `xla.compile`.

  Returns:
    Tensors extracted from outputs and an empty list because Operations are not
    allowed in non-flat outputs..
  """
  # Convert all non-Operation outputs to Tensors.
  new_output_tensors = []
  for o in nest.flatten(outputs):
    if isinstance(o, ops.Operation):
      raise ValueError(
          'xla.compile does not support Operation as return value in non-flat '
          'output structure. You can set returned Operations as control '
          'dependencies of returned Tensors so Operations are triggered when '
          'Tensors are evaluated. Operation found: "%s"' % o.name)

    try:
      o = ops.convert_to_tensor(o)
    except Exception as e:
      raise ValueError(
          'XLA computation function return values must all either be '
          'Operations or convertible to Tensors. Got error: "%s"' % str(e))

    # Makes sure even pass-through inputs/outputs are touched in compile
    # context by creating an Identity node inside compile context.
    with ops.device(o.device if o.device else ''):
      new_output_tensors.append(array_ops.identity(o))

  return new_output_tensors, []


@contextlib.contextmanager
def _disable_summary_context():
  """Enters a context where all summary ops are skipped.

  Summaries are not yet supported in xla.compile(). So we provide this context
  manager that can skip creating summary ops. This is a temporary workaround due
  to XLA not supporting summary ops.

  Yields:
    None.
  """
  original_skip_summary_func = summary_op_util.skip_summary
  summary_op_util.skip_summary = lambda: True

  try:
    yield
  finally:
    summary_op_util.skip_summary = original_skip_summary_func


class _CapturedObject(object):
  """A placeholder to capture an object."""

  def __init__(self):
    self._object = None

  def capture(self, o):
    if self._object:
      raise RuntimeError(
          'InternalError: _CapturedObject can capture only once. Please file '
          'bug.')

    self._object = o

  def get(self):
    return self._object


def _get_scaffold(captured_scaffold_fn):
  """Retrieves the Scaffold from `captured_scaffold_fn`."""
  scaffold_fn = captured_scaffold_fn.get()

  if not scaffold_fn:
    return None

  scaffold = scaffold_fn()
  if scaffold is None:
    raise ValueError(
        'TPUEstimatorSpec.scaffold_fn returns None, which is not allowed')

  return scaffold


def check_function_argument_count(func, input_arity, infeed_queue):
  """Validate the number of input arguments to an XLA function.

  Args:
    func: the Python function that will be called to generate the body of an XLA
      computation graph.
    input_arity: the number of explicit arguments supplied by the caller.
    infeed_queue: if not None, the infeed queue that will supply
      additional arguments to the function.

  Returns:
    None if function can be called with the supplied number of
      arguments, or an error string if it cannot.
  """
  def format_error(complaint, quantity):
    return '%s %d argument%s' % (complaint, quantity, ''
                                 if quantity == 1 else 's')

  num_args_supplied = input_arity
  if infeed_queue is not None:
    num_args_supplied += infeed_queue.number_of_tuple_elements
  arg_spec = tf_inspect.getargspec(func)
  num_func_args = len(arg_spec.args)
  if arg_spec.defaults is None:
    num_func_defaults = 0
  else:
    num_func_defaults = len(arg_spec.defaults)
  min_func_args = num_func_args - num_func_defaults
  if num_args_supplied < min_func_args:
    # The required number of arguments is not enough to call the function.
    if num_func_defaults == 0 and arg_spec.varargs is None:
      return format_error('exactly', num_func_args)
    else:
      return format_error('at least', min_func_args)
  if arg_spec.varargs is None and num_args_supplied > num_func_args:
    # The required number of arguments is too many to call the function.
    if num_func_defaults == 0:
      return format_error('exactly', num_func_args)
    else:
      return format_error('at most', num_func_args)
  # Reaching here means either
  # 1) There are varargs, func can accept any number of arguments greater than
  # the minimum.
  # 2) Number of supplied arguments falls in range of acceptable argument count
  # of func.
  return None
