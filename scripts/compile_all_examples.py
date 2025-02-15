#!/usr/bin/env python3
import argparse
import operator
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import attrs
import prettytable

SCRIPT_DIR = Path(__file__).parent
GIT_ROOT = SCRIPT_DIR.parent
CONTRACT_ROOT_DIRS = [
    GIT_ROOT / "examples",
    GIT_ROOT / "test_cases",
]
SIZE_TALLY_PATH = GIT_ROOT / "examples" / "sizes.txt"
ENV_WITH_NO_COLOR = dict(os.environ) | {
    "NO_COLOR": "1",  # disable colour output
    "PYTHONUTF8": "1",  # force utf8 on windows
}


def get_root_and_relative_path(path: Path) -> tuple[Path, Path]:
    for root in CONTRACT_ROOT_DIRS:
        if path.is_relative_to(root):
            return root, path.relative_to(root)
    raise RuntimeError(f"{path} is not relative to a known example")


def get_unique_name(path: Path) -> str:
    _, rel_path = get_root_and_relative_path(path)
    # strip suffixes
    while rel_path.suffixes:
        rel_path = rel_path.with_suffix("")

    use_parts = []
    for part in rel_path.parts:
        if "MyContract" in part:
            use_parts.append("".join(part.split("MyContract")))
        elif "Contract" in part:
            use_parts.append("".join(part.split("Contract")))
        elif part.endswith((f"out{SUFFIX_O0}", f"out{SUFFIX_O1}", f"out{SUFFIX_O2}")):
            pass
        else:
            use_parts.append(part)
    return "/".join(filter(None, use_parts))


@attrs.define(str=False)
class ProgramSizes:
    sizes: dict[str, dict[int, int]] = attrs.field(factory=dict)

    def add_at_level(self, level: int, bin_file: Path) -> None:
        name = get_unique_name(bin_file)
        sizes = self.sizes.setdefault(name, {})
        # this combines both approval and clear program sizes
        sizes[level] = sizes.get(level, 0) + bin_file.stat().st_size

    @classmethod
    def read_file(cls, path: Path) -> "ProgramSizes":
        lines = path.read_text("utf-8").splitlines()
        program_sizes = ProgramSizes()
        for line in lines[1:]:
            name, o0, o1, o1_delta, o2, o2_delta = line.rsplit(maxsplit=5)
            name = name.strip()
            program_sizes.sizes[name] = {}
            for key, int_str in enumerate((o0, o1, o2)):
                try:
                    val = int(int_str)
                except ValueError:
                    continue
                program_sizes.sizes[name][key] = val
        return program_sizes

    def __str__(self) -> str:
        writer = prettytable.PrettyTable(
            field_names=["Name", "O0 size", "O1 size", "O1 ⏷", "O2 size", "O2 ⏷"],
            sortby="Name",
            header=True,
            border=False,
            padding_width=2,
            left_padding_width=0,
            right_padding_width=1,
            align="r",
        )
        writer.align["Name"] = "l"
        for name, prog_sizes in self.sizes.items():
            o0 = prog_sizes.get(0)
            o1 = prog_sizes.get(1)
            o2 = prog_sizes.get(2)
            o1_delta = o2_delta = None
            if o0 is not None and o1 is not None:
                o1_delta = o0 - o1
            if o1 is not None and o2 is not None:
                o2_delta = o1 - o2
            writer.add_row(list(map(str, (name, o0, o1, o1_delta, o2, o2_delta))))
        return writer.get_string()


@attrs.define
class CompilationResult:
    rel_path: str
    ok: bool
    bin_files: list[Path]
    stdout: str


def _stabilise_logs(stdout: str) -> list[str]:
    return [
        line.replace("\\", "/").replace(str(GIT_ROOT).replace("\\", "/"), "<git root>")
        for line in stdout.splitlines()
        if not line.startswith(
            (
                "debug: Skipping algopy stub ",
                "debug: Skipping typeshed stub ",
                "warning: Skipping stub: ",
                "debug: Skipping stdlib stub ",
                "debug: Building AWST for ",
                "debug: Discovered user module ",
                # ignore platform specific paths
                "debug: Using python executable: ",
                "debug: Using python site-packages: ",
                "debug: Found algopy: ",
            )
        )
    ]


def checked_compile(
    p: Path, flags: list[str], *, out_suffix: str, write_logs: bool
) -> CompilationResult:
    assert p.is_dir()
    out_dir = (p / f"out{out_suffix}").resolve()
    template_vars_path = p / "template.vars"

    root, rel_path_ = get_root_and_relative_path(p)
    rel_path = str(rel_path_)

    if out_dir.exists():
        for prev_out_file in out_dir.iterdir():
            if prev_out_file.is_dir():
                shutil.rmtree(prev_out_file)
            elif prev_out_file.suffix != ".log":
                prev_out_file.unlink()
    cmd = [
        "poetry",
        "run",
        "puyapy",
        *flags,
        f"--out-dir={out_dir}",
        "--output-bytecode",
        "--log-level=debug",
        *_load_template_vars(template_vars_path),
        rel_path,
    ]
    result = subprocess.run(
        cmd,
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=ENV_WITH_NO_COLOR,
        encoding="utf-8",
    )
    bin_files_written = re.findall(r"info: Writing (.+\.bin)", result.stdout)

    if write_logs:
        if p.is_dir():
            log_path = p / "puya.log"
        else:
            log_path = p.with_suffix(".puya.log")

        log_txt = "\n".join(_stabilise_logs(result.stdout))
        log_path.write_text(log_txt, encoding="utf8")
    ok = result.returncode == 0
    return CompilationResult(
        rel_path=rel_path,
        ok=ok,
        bin_files=[root / p for p in bin_files_written],
        stdout=result.stdout if not ok else "",  # don't thunk stdout if no errors
    )


def _load_template_vars(path: Path) -> Iterable[str]:
    if path.exists():
        for line in path.read_text("utf8").splitlines():
            if line.startswith("prefix="):
                prefix = line.removeprefix("prefix=")
                yield f"--template-vars-prefix={prefix}"
            else:
                yield f"-T={line}"


SUFFIX_O0 = "_unoptimized"
SUFFIX_O1 = ""
SUFFIX_O2 = "_O2"


def _compile_for_level(arg: tuple[Path, int]) -> tuple[CompilationResult, int]:
    p, optimization_level = arg
    if optimization_level == 0:
        flags = [
            "-O0",
            "--output-destructured-ir",
            "--no-output-arc32",
        ]
        out_suffix = SUFFIX_O0
        write_logs = False
    elif optimization_level == 2:
        flags = [
            "-O2",
            "--output-destructured-ir",
            "--no-output-arc32",
            "-g0",
        ]
        out_suffix = SUFFIX_O2
        write_logs = False
    else:
        assert optimization_level == 1
        flags = [
            "-O1",
            "--output-awst",
            "--output-ssa-ir",
            "--output-optimization-ir",
            "--output-destructured-ir",
            "--output-memory-ir",
            "--output-client",
        ]
        out_suffix = SUFFIX_O1
        write_logs = True
    result = checked_compile(p, flags=flags, out_suffix=out_suffix, write_logs=write_logs)
    return result, optimization_level


@attrs.define(kw_only=True)
class CompileAllOptions:
    limit_to: list[Path] = attrs.field(factory=list)


def main(options: CompileAllOptions) -> None:
    limit_to = options.limit_to
    if limit_to:
        to_compile = [Path(x).resolve() for x in limit_to]
    else:
        to_compile = [
            item
            for root in CONTRACT_ROOT_DIRS
            for item in root.iterdir()
            if item.is_dir() and any(item.glob("*.py"))
        ]

    failures = list[tuple[str, str]]()
    program_sizes = ProgramSizes()
    with ProcessPoolExecutor() as executor:
        # iterate optimization levels first and with O1 first and then cases, this is a workaround
        # to prevent race conditions that occur when the mypy parsing stage of O0, O2 tries to
        # read the client_<contract>.py output from the 01 level before it is finished writing to
        # disk
        args = [(case, level) for level in (1, 0, 2) for case in to_compile]
        for compilation_result, level in executor.map(_compile_for_level, args):
            rel_path = compilation_result.rel_path
            case_name = f"{rel_path} -O{level}"
            for bin_file in compilation_result.bin_files:
                program_sizes.add_at_level(level, bin_file)
            if compilation_result.ok:
                print(f"✅  {case_name}")
            else:
                print(f"💥 {case_name}", file=sys.stderr)
                failures.append((case_name, compilation_result.stdout))

    if failures:
        print("Compilation failures:")
        for name, stdout in sorted(failures, key=operator.itemgetter(0)):
            print(f" ~~~ {name} ~~~ ")
            print(
                "\n".join(
                    ln
                    for ln in stdout.splitlines()
                    if (ln.startswith("debug: Traceback ") or not ln.startswith("debug: "))
                )
            )
    print("Updating sizes.txt")
    if limit_to:
        existing = ProgramSizes.read_file(SIZE_TALLY_PATH)
        program_sizes = ProgramSizes(sizes={**existing.sizes, **program_sizes.sizes})
    SIZE_TALLY_PATH.write_text(str(program_sizes))
    sys.exit(len(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("limit_to", type=Path, nargs="*", metavar="LIMIT_TO")
    options = CompileAllOptions()
    parser.parse_args(namespace=options)
    main(options)
