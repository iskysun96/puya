import abc
import contextlib
from collections.abc import Callable, Iterator

import attrs

from puya.errors import InternalError
from puya.mir import models
from puya.mir.context import ProgramMIRContext
from puya.mir.stack import Stack
from puya.mir.vla import VariableLifetimeAnalysis


@attrs.define
class AlignedWriter:
    _headers: list[str] = attrs.field(factory=list)
    _lines: list[list[str] | str] = attrs.field(factory=list)
    _current_line: list[str] = attrs.field(factory=list)
    _ignore_current_line: bool = False
    _padding: dict[int, int] = attrs.field(factory=dict)
    _indent: str = ""
    omit_empty_columns: bool = True

    @property
    def ignore_current_line(self) -> bool:
        return self._ignore_current_line

    @contextlib.contextmanager
    def indent(self) -> Iterator[None]:
        indent_width = 4
        original_indent = self._indent
        self._indent += " " * indent_width
        try:
            yield
        finally:
            self._indent = original_indent

    def add_header(self, header: str, padding: int = 1) -> None:
        self._padding[len(self._headers)] = padding
        self._headers.append(header)

    def append(self, part: str) -> None:
        self._current_line.append(part)

    def new_line(self) -> None:
        parts = self._current_line
        if not self._ignore_current_line:
            if parts and self._indent:
                parts[0] = f"{self._indent}{parts[0]}"
            self._lines.append(parts)
        self._ignore_current_line = False
        self._current_line = []

    def append_line(self, line: str) -> None:
        if self._current_line or self._ignore_current_line:
            raise InternalError(
                "Cannot append a new line while a current line is in progress, missing new_line()?"
            )
        self._lines.append(line)

    def ignore_line(self) -> None:
        self._ignore_current_line = True

    def _join_columns(self, line: list[str], widths: dict[int, int]) -> str:
        return "".join(
            part.ljust(widths.get(column, 0))
            for column, part in enumerate(line)
            if widths.get(column, 0) or not self.omit_empty_columns
        ).rstrip()

    def write(self) -> list[str]:
        widths = dict[int, int]()
        all_lines = self._lines.copy()
        for parts in all_lines:
            if isinstance(parts, list):
                for column, part in enumerate(parts):
                    widths[column] = max(widths.get(column, 0), len(part))

        for column, width in widths.items():
            if width == 0 and self.omit_empty_columns:
                continue
            if column < len(self._headers):
                width = max(width, len(self._headers[column]))
            widths[column] = width + self._padding.get(column, 1)

        if self._headers:
            all_lines.insert(0, self._headers)

        return [
            line if isinstance(line, str) else self._join_columns(line, widths)
            for line in all_lines
        ]


class OpAnnotater(abc.ABC):
    @abc.abstractmethod
    def annotate(self, writer: AlignedWriter, op: models.BaseOp) -> None: ...

    @abc.abstractmethod
    def begin_block(self, writer: AlignedWriter, block: models.MemoryBasicBlock) -> None: ...


@attrs.frozen
class SimpleOpAnnotater(OpAnnotater):
    _annotate: Callable[[AlignedWriter, models.BaseOp], None]

    def annotate(self, writer: AlignedWriter, op: models.BaseOp) -> None:
        return self._annotate(writer, op)

    def begin_block(self, writer: AlignedWriter, block: models.MemoryBasicBlock) -> None:
        pass


@attrs.frozen(kw_only=True)
class EmitProgramContext(ProgramMIRContext):
    annotaters: "list[MIRAnnotater]"
    writer: AlignedWriter = attrs.field(factory=AlignedWriter)
    stack: Stack = attrs.field()

    @stack.default
    def _stack_factory(self) -> Stack:
        return Stack(allow_virtual=False)


@attrs.frozen(kw_only=True)
class EmitSubroutineContext(EmitProgramContext):
    subroutine: models.MemorySubroutine


class MIRAnnotater(abc.ABC):
    @abc.abstractmethod
    def header(self, writer: AlignedWriter) -> None: ...

    @abc.abstractmethod
    def create_op_annotater(self, context: EmitSubroutineContext) -> OpAnnotater: ...


class BeginCommentsAnnotater(MIRAnnotater):
    def header(self, writer: AlignedWriter) -> None:
        writer.add_header("//")

    def create_op_annotater(self, _context: EmitSubroutineContext) -> OpAnnotater:
        def annotate(writer: AlignedWriter, _op: models.BaseOp) -> None:
            writer.append("//")

        return SimpleOpAnnotater(annotate)


class OpDescriptionAnnotation(MIRAnnotater):
    def header(self, writer: AlignedWriter) -> None:
        writer.add_header("Op Description")

    def create_op_annotater(self, _context: EmitSubroutineContext) -> OpAnnotater:
        def annotate(writer: AlignedWriter, op: models.BaseOp) -> None:
            if isinstance(
                op, models.StoreOp | models.LoadOp | models.Allocate | models.VirtualStackOp
            ):
                writer.append(str(op))
            else:
                writer.append("")

        return SimpleOpAnnotater(annotate)


class StackAnnotation(MIRAnnotater):
    def header(self, writer: AlignedWriter) -> None:
        writer.add_header("Stack (out)", 4)

    def create_op_annotater(self, context: EmitSubroutineContext) -> OpAnnotater:
        @attrs.define
        class Annotater(OpAnnotater):
            stack: Stack = attrs.field(factory=Stack)

            def begin_block(self, _writer: AlignedWriter, block: models.MemoryBasicBlock) -> None:
                self.stack.begin_block(context.subroutine, block)

            def annotate(self, writer: AlignedWriter, op: models.BaseOp) -> None:
                op.accept(self.stack)
                writer.append(self.stack.full_stack_desc)

        return Annotater()


class VLAAnnotation(MIRAnnotater):
    def header(self, writer: AlignedWriter) -> None:
        writer.add_header("Live (out)", 4)

    def create_op_annotater(self, context: EmitSubroutineContext) -> OpAnnotater:
        vla = VariableLifetimeAnalysis.analyze(context.subroutine)

        def annotater(writer: AlignedWriter, op: models.BaseOp) -> None:
            live = vla.get_live_out_variables(op)
            writer.append(", ".join(live))

        return SimpleOpAnnotater(annotater)


@attrs.define
class SourceAnnotation(MIRAnnotater):
    max_width: int = 100

    def header(self, writer: AlignedWriter) -> None:
        writer.add_header("Source code")
        writer.add_header("Source line")

    def create_op_annotater(self, context: EmitSubroutineContext) -> OpAnnotater:
        def annotater(writer: AlignedWriter, op: models.BaseOp) -> None:
            op_loc = op.source_location
            code_lines = context.try_get_source(op_loc) or []
            # at lower debug levels only include first line of source
            if context.options.debug_level < 2:
                code_lines = code_lines[:1]

            code = "\\n".join([line.strip() for line in code_lines])
            if len(code) > self.max_width:
                code = code[: self.max_width - 3] + "..."
            writer.append(code)
            writer.append(str(op_loc) if op_loc else "")

        return SimpleOpAnnotater(annotater)


class XStack(MIRAnnotater):
    def header(self, writer: AlignedWriter) -> None:
        writer.add_header("X stack")

    def create_op_annotater(self, _context: EmitSubroutineContext) -> OpAnnotater:
        class Annotater(OpAnnotater):
            block: models.MemoryBasicBlock
            need_x_stack_in: bool

            def begin_block(self, _writer: AlignedWriter, block: models.MemoryBasicBlock) -> None:
                self.block = block
                # keep track of if we x_stack_in needs to be emitted as sometimes a line is
                # ignored, so need to emit on first line that isn't ignored
                self.need_x_stack_in = bool(block.x_stack_in)

            def annotate(self, writer: "AlignedWriter", op: models.BaseOp) -> None:
                if self.need_x_stack_in and not writer.ignore_current_line:
                    self.need_x_stack_in = False
                    writer.append(", ".join(self.block.x_stack_in or ()))
                elif op == self.block.ops[-1] and self.block.x_stack_out:
                    writer.append(", ".join(self.block.x_stack_out))
                else:
                    writer.append("")

        return Annotater()
