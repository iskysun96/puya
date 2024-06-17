from __future__ import annotations

import typing

import mypy.nodes

from puya import log
from puya.algo_constants import ENCODED_ADDRESS_LENGTH
from puya.awst import wtypes
from puya.awst.nodes import (
    AddressConstant,
    CheckedMaybe,
    Expression,
    IntrinsicCall,
    NumericComparison,
    NumericComparisonExpression,
    ReinterpretCast,
    TupleItemExpression,
    UInt64Constant,
)
from puya.awst_build import intrinsic_factory, pytypes
from puya.awst_build.eb._base import (
    FunctionBuilder,
)
from puya.awst_build.eb._bytes_backed import BytesBackedTypeBuilder
from puya.awst_build.eb._utils import cast_to_bytes, compare_bytes, compare_expr_bytes
from puya.awst_build.eb.bool import BoolExpressionBuilder
from puya.awst_build.eb.interface import (
    BuilderComparisonOp,
    InstanceBuilder,
    LiteralBuilder,
    LiteralConverter,
    NodeBuilder,
)
from puya.awst_build.eb.reference_types._base import ReferenceValueExpressionBuilder
from puya.errors import CodeError

if typing.TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from puya.parse import SourceLocation


logger = log.get_logger(__name__)


class AccountTypeBuilder(BytesBackedTypeBuilder, LiteralConverter):
    def __init__(self, location: SourceLocation):
        super().__init__(pytypes.AccountType, location)

    @typing.override
    @property
    def convertable_literal_types(self) -> Collection[pytypes.PyType]:
        return (pytypes.StrLiteralType,)

    @typing.override
    def convert_literal(
        self, literal: LiteralBuilder, location: SourceLocation
    ) -> InstanceBuilder:
        return self.call([literal], [mypy.nodes.ARG_POS], [None], location)  # TODO: fixme

    @typing.override
    def call(
        self,
        args: Sequence[NodeBuilder],
        arg_kinds: list[mypy.nodes.ArgKind],
        arg_names: list[str | None],
        location: SourceLocation,
    ) -> InstanceBuilder:
        match args:
            case []:
                value: Expression = intrinsic_factory.zero_address(location)
            case [LiteralBuilder(value=str(addr_value))]:
                if not wtypes.valid_address(addr_value):
                    raise CodeError(
                        f"Invalid address value. Address literals should be"
                        f" {ENCODED_ADDRESS_LENGTH} characters and not include base32 padding",
                        location,
                    )
                value = AddressConstant(value=addr_value, source_location=location)
            case [InstanceBuilder(pytype=pytypes.BytesType) as eb]:
                address_bytes_temp = eb.single_eval().resolve()
                is_correct_length = NumericComparisonExpression(
                    operator=NumericComparison.eq,
                    source_location=location,
                    lhs=UInt64Constant(value=32, source_location=location),
                    rhs=intrinsic_factory.bytes_len(address_bytes_temp, location),
                )
                value = CheckedMaybe.from_tuple_items(
                    expr=ReinterpretCast(
                        expr=address_bytes_temp,
                        wtype=wtypes.account_wtype,
                        source_location=location,
                    ),
                    check=is_correct_length,
                    source_location=location,
                    comment="Address length is 32 bytes",
                )
            case _:
                logger.error("Invalid/unhandled arguments", location=location)
                # dummy value to continue with
                value = intrinsic_factory.zero_address(location)
        return AccountExpressionBuilder(value)


class AccountExpressionBuilder(ReferenceValueExpressionBuilder):
    def __init__(self, expr: Expression):
        native_type = pytypes.BytesType
        native_access_member = "bytes"
        field_mapping = {
            "balance": ("AcctBalance", pytypes.UInt64Type),
            "min_balance": ("AcctMinBalance", pytypes.UInt64Type),
            "auth_address": ("AcctAuthAddr", pytypes.AccountType),
            "total_num_uint": ("AcctTotalNumUint", pytypes.UInt64Type),
            "total_num_byte_slice": ("AcctTotalNumByteSlice", pytypes.UInt64Type),
            "total_extra_app_pages": ("AcctTotalExtraAppPages", pytypes.UInt64Type),
            "total_apps_created": ("AcctTotalAppsCreated", pytypes.UInt64Type),
            "total_apps_opted_in": ("AcctTotalAppsOptedIn", pytypes.UInt64Type),
            "total_assets_created": ("AcctTotalAssetsCreated", pytypes.UInt64Type),
            "total_assets": ("AcctTotalAssets", pytypes.UInt64Type),
            "total_boxes": ("AcctTotalBoxes", pytypes.UInt64Type),
            "total_box_bytes": ("AcctTotalBoxBytes", pytypes.UInt64Type),
        }
        field_op_code = "acct_params_get"
        field_bool_comment = "account funded"
        super().__init__(
            expr,
            typ=pytypes.AccountType,
            native_type=native_type,
            native_access_member=native_access_member,
            field_mapping=field_mapping,
            field_op_code=field_op_code,
            field_bool_comment=field_bool_comment,
        )

    @typing.override
    def to_bytes(self, location: SourceLocation) -> Expression:
        return cast_to_bytes(self.resolve(), location)

    @typing.override
    def member_access(self, name: str, location: SourceLocation) -> NodeBuilder:
        if name == "is_opted_in":
            return _IsOptedIn(self.resolve(), location)
        return super().member_access(name, location)

    @typing.override
    def bool_eval(self, location: SourceLocation, *, negate: bool = False) -> InstanceBuilder:
        return compare_expr_bytes(
            source_location=location,
            lhs=self.resolve(),
            op=BuilderComparisonOp.eq if negate else BuilderComparisonOp.ne,
            rhs=intrinsic_factory.zero_address(location),
        )

    @typing.override
    def compare(
        self, other: InstanceBuilder, op: BuilderComparisonOp, location: SourceLocation
    ) -> InstanceBuilder:
        other = other.resolve_literal(converter=AccountTypeBuilder(other.source_location))
        return compare_bytes(lhs=self, op=op, rhs=other, source_location=location)


class _IsOptedIn(FunctionBuilder):
    def __init__(self, expr: Expression, source_location: SourceLocation):
        super().__init__(source_location)
        self.expr = expr

    @typing.override
    def call(
        self,
        args: Sequence[NodeBuilder],
        arg_kinds: list[mypy.nodes.ArgKind],
        arg_names: list[str | None],
        location: SourceLocation,
    ) -> InstanceBuilder:
        match args:
            case [InstanceBuilder(pytype=pytypes.AssetType) as asset]:
                return BoolExpressionBuilder(
                    TupleItemExpression(
                        base=IntrinsicCall(
                            op_code="asset_holding_get",
                            immediates=["AssetBalance"],
                            stack_args=[self.expr, asset.resolve()],
                            wtype=wtypes.WTuple(
                                (wtypes.uint64_wtype, wtypes.bool_wtype), location
                            ),
                            source_location=location,
                        ),
                        index=1,
                        source_location=location,
                    )
                )
            case [InstanceBuilder(pytype=pytypes.ApplicationType) as app]:
                return BoolExpressionBuilder(
                    IntrinsicCall(
                        op_code="app_opted_in",
                        stack_args=[self.expr, app.resolve()],
                        source_location=location,
                        wtype=wtypes.bool_wtype,
                    )
                )
        raise CodeError("Unexpected argument", location)
