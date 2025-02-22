"""Test op correctness by comparing with PyTorch results.

Usage:

    pytest onnxscript/tests/function_libs/torch_lib/ops_test.py

    To run tests on a specific operator (e.g. torch.ceil):

    pytest onnxscript/tests/function_libs/torch_lib/ops_test.py -k ceil

    To run tests on a nn operator (e.g. nn.functional.scaled_dot_product_attention):

    pytest onnxscript/tests/function_libs/torch_lib/ops_test.py -k nn_functional_scaled_dot_product_attention

"""
from __future__ import annotations

import unittest
from typing import Any, Callable, Optional, Sequence, Tuple

import numpy as np
import onnx
import onnxruntime as ort
import parameterized
import torch
from torch.testing._internal import common_device_type
from torch.testing._internal.opinfo import core as opinfo_core
from torch.utils import _pytree as pytree

import onnxscript
import onnxscript.evaluator
from onnxscript.tests.function_libs.torch_lib import ops_test_common, ops_test_data

# All dtypes will be tested on the generated symbolic functions.
# complex64 will be flattened to float32.
TESTED_DTYPES = (
    torch.float16,
    torch.float32,
    # Uncomment below item when we really need testing it
    # torch.bfloat16,
    # torch.float64,
    # torch.bool,
    # torch.int8,
    # torch.int16,
    # torch.int32,
    torch.int64,
    # torch.uint8,
    # torch.complex64,
    # ......
)
# NOTE: torch.complex32 is experimental in torch
COMPLEX_TYPES = (torch.complex64,)


def dtypes_except(*dtypes: torch.dtype) -> Sequence[torch.dtype]:
    """Returns all dtypes except the ones specified."""
    return tuple(dtype for dtype in TESTED_DTYPES if dtype not in dtypes)


def _should_skip_xfail_test_sample(
    op_name: str, sample
) -> Tuple[Optional[str], Optional[str]]:
    """Returns a reason if a test sample should be skipped."""
    if op_name not in ops_test_data.OP_WITH_SKIPPED_XFAIL_SUBTESTS:
        return None, None
    for decorator_meta in ops_test_data.SKIP_XFAIL_SUBTESTS:
        # Linear search on ops_test_data.SKIP_XFAIL_SUBTESTS. That's fine because the list is small.
        if decorator_meta.op_name == op_name:
            assert decorator_meta.matcher is not None, "Matcher must be defined"
            if decorator_meta.matcher(sample):
                return decorator_meta.test_behavior, decorator_meta.reason
    return None, None


def _split_function_and_wrangler(
    onnx_function_and_wrangler: Callable[..., Any]
    | tuple[Callable[..., Any], Callable[..., Any]]
) -> tuple[Callable[..., Any], Callable[..., Any] | None]:
    """Splits a function with an optional input wrangler into a function and an input wrangler."""
    if isinstance(onnx_function_and_wrangler, tuple):
        return onnx_function_and_wrangler

    assert callable(onnx_function_and_wrangler)
    return onnx_function_and_wrangler, None


class TestFunctionValidity(unittest.TestCase):
    def test_all_script_functions_are_onnx_functions(self):
        for info in ops_test_data.TESTED_TORCHLIB_OPS:
            if info.trace_only:
                continue
            with self.subTest(name=info.op_info_name):
                func = info.op
                if not isinstance(func, onnxscript.OnnxFunction):
                    raise AssertionError(
                        f"'{func}' is not an OnnxFunction. Was it decorated with '@torch_op'? "
                        "If the function is trace_only, please specify trace_only=True "
                        "in the TorchLibOpInfo entry."
                    )

    def test_all_trace_only_functions_are_not_onnx_functions(self):
        for info in ops_test_data.TESTED_TORCHLIB_OPS:
            if not info.trace_only:
                continue
            with self.subTest(name=info.op_info_name):
                func = info.op
                if not isinstance(func, onnxscript.TracedOnnxFunction):
                    raise AssertionError(
                        f"'{func.name}' is not a TracedOnnxFunction. "
                        "If the function is not trace_only, please remove trace_only=True "
                        "in the TorchLibOpInfo entry."
                    )

    @parameterized.parameterized.expand(
        [
            (info.op.name, info)
            for info in ops_test_data.TESTED_TORCHLIB_OPS
            if not info.trace_only
        ]
    )
    def test_script_function_passes_checker(
        self, _, torchlib_op_info: ops_test_data.TorchLibOpInfo
    ):
        function_proto = torchlib_op_info.op.to_function_proto()
        onnx.checker.check_function(function_proto)  # type: ignore[attr-defined]

    @parameterized.parameterized.expand(
        [(info.op.name, info) for info in ops_test_data.TESTED_TORCHLIB_OPS]
    )
    def test_function_has_op_schema(self, _, torchlib_op_info: ops_test_data.TorchLibOpInfo):
        func = torchlib_op_info.op
        schema = func.op_schema
        self.assertIsNotNone(schema)
        self.assertEqual(schema.name, func.name)


def run_test_output_match(
    test_suite: unittest.TestCase,
    device: str,
    dtype: torch.dtype,
    op: opinfo_core.OpInfo,
    function_executor: Callable,
    tested_op_mapping: dict[
        str,
        ops_test_data.TorchLibOpInfo,
    ],
):
    """Base test method for testing each opset, used by instantiate_device_type_tests.

    Args:
        test_suite: The test class instance.
        device: The PyTorch device. instantiate_device_type_tests provides this.
        dtype: The PyTorch dtype. instantiate_device_type_tests provides this.
        op: The OpInfo instance. instantiate_device_type_tests provides this.
        function_executor: The function executor. This is a function that takes
            a function and its arguments and returns the output of the function.
        tested_op_mapping: The mapping of op name to the tested op.
    """
    samples = op.sample_inputs(
        device,
        dtype,
        requires_grad=False,
    )

    torchlib_op_info = tested_op_mapping[op.name]
    # Obtain the input_wrangler that manipulates the OpInfo inputs
    # to match the aten operator signature
    # An example is nn.functional.upsample_nearest2d, which has a different signature
    # than the aten operator upsample_nearest2d
    onnx_function = torchlib_op_info.op
    input_wrangler = torchlib_op_info.input_wrangler
    if (
        not ops_test_common.dtype_op_schema_compatible(dtype, onnx_function.op_schema)
        and dtype not in COMPLEX_TYPES
    ):
        test_suite.skipTest(
            f"dtype '{dtype}' is not supported by the op '{op.name}'. "
            f"Type constraints: {onnx_function.op_schema.type_constraints}"
        )

    # Obtain the tolerance for the op
    rtol, atol = torchlib_op_info.get_tolerance(dtype)

    for i, cpu_sample in enumerate(samples):
        inputs = (cpu_sample.input, *cpu_sample.args)
        # Provide the repr to subtest because tensors are not serializable in parallel test runs
        with test_suite.subTest(
            sample_num=i,
            inputs=repr(
                [
                    f"Tensor<{inp.shape}, dtype={inp.dtype}>"
                    if isinstance(inp, torch.Tensor)
                    else inp
                    for inp in inputs
                ]
            ),
            kwargs=repr(cpu_sample.kwargs),
        ):
            test_behavior, reason = _should_skip_xfail_test_sample(op.name, cpu_sample)

            with ops_test_common.normal_xfail_skip_test_behaviors(test_behavior, reason):
                input_onnx = [ops_test_common.convert_tensor_to_numpy(x) for x in inputs]
                kwargs_onnx = ops_test_common.convert_kwargs_for_onnx(cpu_sample.kwargs)
                if input_wrangler:
                    input_onnx, kwargs_onnx = input_wrangler(input_onnx, kwargs_onnx)
                torch_output = op(*inputs, **cpu_sample.kwargs)

                if isinstance(torch_output, torch.Tensor) and torch.is_complex(torch_output):
                    torch_output = torch.view_as_real(torch_output.resolve_conj())

                reference_torch_outputs, _ = pytree.tree_flatten(torch_output)
                if (
                    op.name.startswith("split")
                    or op.name.startswith("chunk")
                    or op.name.startswith("unbind")
                ):
                    # Hack for handling split, chunk and unbind which relies on SplitToSequence op.
                    # Split returns a Sequence that should be treats as a single
                    # value. So we wrap it into a tuple.
                    # TODO(justinchuby): Find a more general solution
                    reference_torch_outputs = [reference_torch_outputs]

                function_output = function_executor(reference_torch_outputs)(
                    onnx_function, input_onnx, kwargs_onnx
                )
                # Finally we re-flatten everything
                # TODO: add pytree structure comparison.
                flattened_torch_outputs, _ = pytree.tree_flatten(torch_output)
                flattened_function_outputs, _ = pytree.tree_flatten(function_output)

                assert flattened_torch_outputs
                assert len(flattened_torch_outputs) == len(flattened_function_outputs)

                for j, (torch_output, function_output) in enumerate(
                    zip(flattened_torch_outputs, flattened_function_outputs)
                ):
                    if not isinstance(function_output, np.ndarray):
                        # An onnxscript tensor
                        function_output = function_output.value

                    actual = torch.tensor(function_output)
                    expected = (
                        torch_output
                        if isinstance(torch_output, torch.Tensor)
                        else torch.tensor(torch_output)
                    )

                    if op.name in ops_test_data.NONDETERMINISTIC_OPS:
                        # Check shape and dtype only for ops that are known to be
                        # nondeterministic
                        test_suite.assertEqual(actual.shape, expected.shape)
                        test_suite.assertEqual(actual.dtype, expected.dtype)
                        continue

                    # Use torch.testing as opposed to np.testing to ensure dtypes and shapes match
                    try:
                        torch.testing.assert_close(
                            actual,
                            expected,
                            rtol=rtol,
                            atol=atol,
                            equal_nan=True,
                            check_device=False,
                        )
                    except AssertionError as e:
                        if len(flattened_torch_outputs) > 1:
                            raise AssertionError(f"Output {j} mismatch") from e
                        raise


class TestOutputConsistencyEager(unittest.TestCase):
    """Test output consistency between the ONNX op run with ONNX eager mode and PyTorch eager mode.

    This is a parameterized test suite.
    """

    def setUp(self) -> None:
        torch.manual_seed(42)
        np.random.seed(42)
        ort.set_seed(42)

    @ops_test_common.add_decorate_info(
        ops_test_data.OPS_DB,
        "TestOutputConsistencyEager",
        "test_output_match_opinfo_",
        skip_or_xfails=ops_test_data.EXPECTED_SKIPS_OR_FAILS,
    )
    @common_device_type.ops(  # type: ignore[misc]
        [info for info in ops_test_data.OPS_DB if info.name in ops_test_data.TESTED_OPS],
        allowed_dtypes=TESTED_DTYPES,
    )
    def test_output_match_opinfo_(
        self, device: str, dtype: torch.dtype, op: opinfo_core.OpInfo
    ):
        # Base test method for testing each op with the eager executor, used by instantiate_device_type_tests.
        run_test_output_match(
            self,
            device,
            dtype,
            op,
            ops_test_common.eager_executor,
            ops_test_data.TORCHLIB_OPINFO_MAPPING,
        )

    @ops_test_common.add_decorate_info(
        ops_test_data.OPS_DB,
        "TestOutputConsistencyEager",
        "test_output_match_opinfo_",
        skip_or_xfails=ops_test_data.EXPECTED_SKIPS_OR_FAILS,
    )
    @common_device_type.ops(  # type: ignore[misc]
        [
            info
            for info in ops_test_data.OPS_DB
            if info.name in ops_test_data.COMPLEX_FUNCTION_MAPPING
        ],
        allowed_dtypes=COMPLEX_TYPES,
    )
    def test_complex_output_match_opinfo_(
        self, device: str, dtype: torch.dtype, op: opinfo_core.OpInfo
    ):
        """Base test method for testing each op with the eager executor, used by instantiate_device_type_tests."""
        run_test_output_match(
            self,
            device,
            dtype,
            op,
            ops_test_common.eager_executor,
            ops_test_data.COMPLEX_FUNCTION_MAPPING,
        )


class TestOutputConsistencyFullGraph(unittest.TestCase):
    """Test output consistency between exported ONNX op run as a graph and PyTorch eager mode.

    This is a parameterized test suite.
    """

    def setUp(self) -> None:
        torch.manual_seed(42)
        np.random.seed(42)
        ort.set_seed(42)

    @ops_test_common.add_decorate_info(
        ops_test_data.OPS_DB,
        "TestOutputConsistencyFullGraph",
        "test_output_match_opinfo_",
        skip_or_xfails=ops_test_data.EXPECTED_SKIPS_OR_FAILS,
    )
    @common_device_type.ops(  # type: ignore[misc]
        [info for info in ops_test_data.OPS_DB if info.name in ops_test_data.TESTED_OPS],
        allowed_dtypes=TESTED_DTYPES,
    )
    def test_output_match_opinfo_(
        self, device: str, dtype: torch.dtype, op: opinfo_core.OpInfo
    ):
        # Base test method for testing each op by running the full ONNX graph.
        run_test_output_match(
            self,
            device,
            dtype,
            op,
            ops_test_common.graph_executor,
            ops_test_data.TORCHLIB_OPINFO_MAPPING,
        )

    @ops_test_common.add_decorate_info(
        ops_test_data.OPS_DB,
        "TestOutputConsistencyFullGraph",
        "test_output_match_opinfo_",
        skip_or_xfails=ops_test_data.EXPECTED_SKIPS_OR_FAILS,
    )
    @common_device_type.ops(  # type: ignore[misc]
        [
            info
            for info in ops_test_data.OPS_DB
            if info.name in ops_test_data.COMPLEX_FUNCTION_MAPPING
        ],
        allowed_dtypes=COMPLEX_TYPES,
    )
    def test_complex_output_match_opinfo_(
        self, device: str, dtype: torch.dtype, op: opinfo_core.OpInfo
    ):
        """Base test method for testing each op by running the full ONNX graph."""
        run_test_output_match(
            self,
            device,
            dtype,
            op,
            ops_test_common.graph_executor,
            ops_test_data.COMPLEX_FUNCTION_MAPPING,
        )


common_device_type.instantiate_device_type_tests(
    TestOutputConsistencyEager, globals(), only_for="cpu"
)

common_device_type.instantiate_device_type_tests(
    TestOutputConsistencyFullGraph, globals(), only_for="cpu"
)

if __name__ == "__main__":
    unittest.main()
