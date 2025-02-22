# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

from __future__ import annotations

from typing import Any, Callable, Optional

import numpy as np
import onnx
from onnx import helper, numpy_helper
from onnx.defs import OpSchema

from onnxscript import tensor, values

# Conversions from python values to ONNX are used by both the script converter as well
# as the eager-mode runtime and both need to be consistent. The script converter converts
# python values into ONNX TensorProto, while the runtime converts python values into
# ONNXScript runtime's value-representation (based on Tensor).


# Utilities to convert a python value to TensorProto (for use by the script converter)


def _py_type_to_onnx_type(pytype: type):
    if pytype is bool:
        return onnx.TensorProto.BOOL
    if pytype is int:
        return onnx.TensorProto.INT64
    if pytype is float:
        return onnx.TensorProto.FLOAT
    if pytype is str:
        return onnx.TensorProto.STRING
    raise ValueError(f"Tensor element of type {pytype} not supported")


def pyvalue_to_onnx_tensor(tensor_name: str, pyvalue):
    if isinstance(pyvalue, np.ndarray):
        return numpy_helper.from_array(pyvalue, tensor_name)
    if isinstance(pyvalue, list):
        if len(pyvalue) == 0:
            raise ValueError("Cannot convert an empty list to tensor")
        pytype = type(pyvalue[0])
        if not all(isinstance(e, pytype) for e in pyvalue):
            raise ValueError(
                "Cannot convert an list with elements of different types to tensor"
            )
        return helper.make_tensor(
            tensor_name,
            _py_type_to_onnx_type(pytype),
            [len(pyvalue)],
            pyvalue,
        )
    onnx_type = _py_type_to_onnx_type(type(pyvalue))
    if onnx_type is onnx.TensorProto.BOOL:
        return helper.make_tensor(tensor_name, onnx_type, [], [int(pyvalue)])
    if onnx_type is onnx.TensorProto.STRING:
        return helper.make_tensor(tensor_name, onnx_type, [], vals=[pyvalue.encode("utf-8")])

    return helper.make_tensor(tensor_name, onnx_type, [], [pyvalue])


# Utilities to convert python values into onnxscript tensors.


def _promotable(x) -> bool:
    """Checks if a runtime parameter value needs to be promoted into an onnxscript value.
    This is the runtime-equivalent of the promotion of literal constants into ONNX values
    in the static converter.
    """
    if isinstance(x, (bool, int, float)):
        return True
    if isinstance(x, list) and x:
        # Note: This is meant to handle valid scenarios correctly. No attempt is
        # made yet to capture all invalid usages in runtime mode.
        return _promotable(x[0])
    return False


def _get_dtype(pyvalue):
    """Return np.dtype to use when converting a python value to an onnxscript tensor.
    Note that int constants are treated as int64, as that is the common type in ONNX
    for shape/index values.
    """
    if isinstance(pyvalue, bool):
        return np.bool_
    elif isinstance(pyvalue, int):
        return np.int64
    elif isinstance(pyvalue, float):
        return np.float32
    elif isinstance(pyvalue, list):
        if pyvalue:
            # TODO: What to do about lists with mixed value types, like [1, 2.0]?
            # Should at least produce an error/warning message.
            return _get_dtype(pyvalue[0])
        raise ValueError("Cannot determine target type for empty list")
    raise TypeError(f"Value of unexpected type {type(pyvalue)}")


def cast_pyvalue_to_os_tensor(pyvalue, dtype=None):
    """Promotes python values into onnxscript tensors.
    The optional argument dtype specifies the desired np.dtype of the tensor,
    used only when a non-standard onnxscript-value is promoted into one.
    """
    if _promotable(pyvalue):
        if dtype is None:
            dtype = _get_dtype(pyvalue)
        return tensor.Tensor(np.array(pyvalue, dtype=dtype))
    return pyvalue


def cast_inputs(
    get_type_info: Callable[[Any], Any],
    cast: Callable[[Any, Any], Any],
    op_schema: OpSchema,
    args,
) -> tuple[Any, ...]:
    """Uses schema specification to support a limited form of auto-casting.

    * Scalars are promoted to tensors.
    * Further. they are cast to the required type when used in ops with other
    tensor inputs that are required to be of same type.
    Thus, in "A+1" or "Add(A, 1)", the value 1 will be converted to the same
    type as A.

    This is used by the converter in a static-mode, as well as by the eager-mode
    execution in a dynamic-mode.
    """
    if op_schema is None:
        # Either an error or a custom op.
        # No checks/casts in this case.
        return tuple(cast(x, None) for x in args)

    expected_inputs = op_schema.inputs
    # We make two passes. In the first pass, we identify known type-bindings for
    # type-variables: eg., {'T1' : np.float32, 'T2' : np.int32}.
    # In the second pass, we use these bindings to cast scalar-values to
    # tensors of appropriate types. The two passes are needed to handle cases
    # like "Add(1, X)" where 1 must be cast to the same type as X.
    type_bindings: dict[Optional[str], np.dtype] = {}
    args_typevars: list[tuple[str, Optional[str]]] = []
    for i, x in enumerate(args):
        if i < len(expected_inputs):
            expected = expected_inputs[i]
        elif expected_inputs[-1].option == OpSchema.FormalParameterOption.Variadic:
            expected = expected_inputs[-1]
            if not expected.is_homogeneous:
                args_typevars.append((x, None))
                continue
        else:
            raise ValueError(
                f"Number of actual parameters {len(args)} "
                f"exceeds number of formal parameters {len(expected_inputs)}."
            )
        typevar = expected.type_str
        if "(" not in typevar:
            # typevar is an identifier, like "T"
            typeinfo = get_type_info(x)
            if typeinfo is not None:
                type_bindings[typevar] = typeinfo
        args_typevars.append((x, typevar))
    cast_args = [cast(x, type_bindings.get(typevar)) for x, typevar in args_typevars]
    return tuple(cast_args)


def dynamic_cast_inputs(op_schema: OpSchema, args):
    """Used for autocast during eager-mode execution."""

    def get_type_info(x):
        return x.dtype if isinstance(x, tensor.Tensor) else None

    return cast_inputs(get_type_info, cast_pyvalue_to_os_tensor, op_schema, args)


def static_cast_inputs(converter, op_schema: Optional[OpSchema], args) -> tuple[str, ...]:
    """Used for autocast during script-translation."""

    def get_type_info(x):
        return x if not x.is_const() else None

    def cast(x, typeinfo) -> str:
        if x.is_const() and typeinfo is not None:
            # Scalar values are promoted to tensors of a type chosen as below:

            tmp = converter.generate_unique_name(f"{x.name}_cast")
            converter.emit(
                [tmp],
                values.Op(converter.default_opset, "CastLike"),
                [x.name, typeinfo],
                [],
            )
            return tmp
        return x.name

    return cast_inputs(get_type_info, cast, op_schema, args)
