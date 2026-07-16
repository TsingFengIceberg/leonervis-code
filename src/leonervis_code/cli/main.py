"""Command-line interface for the current deterministic and explicit-provider slices."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from leonervis_code import __version__
from leonervis_code.agent.loop import AgentLoop
from leonervis_code.cli.brand import color_enabled
from leonervis_code.cli.repl import run_repl
from leonervis_code.core.contracts import AssistantText, ToolResult, ToolUse
from leonervis_code.core.orchestration import (
    GenerationOptions,
    OrchestrationError,
    RouteRequest,
    RouteRequirements,
)
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.factory import create_provider
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.providers.request_policy import preview_request
from leonervis_code.providers.resolver import RuntimeRouteError, resolve_runtime_route
from leonervis_code.providers.routing import (
    DEFAULT_ROUTE_REQUEST,
    FAKE_PROVIDER_PROFILES,
    resolve_route,
)
from leonervis_code.tools.read_file import ReadFileTool


def nonblank_prompt(value: str) -> str:
    """Reject prompt values that contain no visible characters."""
    if not value.strip():
        raise argparse.ArgumentTypeError("prompt must not be blank")
    return value


def nonblank_model(value: str) -> str:
    """Reject real model IDs that contain no visible characters."""
    if not value.strip():
        raise argparse.ArgumentTypeError("model must not be blank")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Create the small CLI surface available through Foundation 3A."""
    parser = argparse.ArgumentParser(
        prog="leonervis-code",
        description="Leonervis Code: a learning-first local coding-agent CLI prototype.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--model",
        type=nonblank_model,
        help="explicit provider/model selector for the prompt command",
    )
    parser.add_argument(
        "--provider-protocol",
        choices=["openai-compatible"],
        help="explicit custom provider wire protocol",
    )
    parser.add_argument("--base-url", help="custom provider API base URL")
    parser.add_argument("--api-key-env", help="environment variable holding a custom API key")
    subcommands = parser.add_subparsers(dest="command")
    prompt_parser = subcommands.add_parser("prompt", help="run one prompt turn")
    prompt_parser.add_argument("prompt", type=nonblank_prompt, help="the prompt to send")
    demo_read_parser = subcommands.add_parser(
        "demo-read", help="visibly demonstrate one deterministic read_file tool loop"
    )
    demo_read_parser.add_argument("path", help="relative workspace path for the demonstration")
    route_parser = subcommands.add_parser(
        "route", help="inspect one deterministic offline provider route plan"
    )
    route_parser.add_argument(
        "--model", dest="route_model", help="provider/model selector or unambiguous alias"
    )
    route_parser.add_argument(
        "--fallback-model",
        action="append",
        default=[],
        help="ordered fallback provider/model selector; repeat to add more",
    )
    route_parser.add_argument("--require-tool-use", action="store_true")
    route_parser.add_argument("--require-streaming", action="store_true")
    route_parser.add_argument("--max-output-tokens", type=int)
    route_parser.add_argument("--temperature", type=float)
    return parser


def render_demo_read(workspace: Path, path: str, stdout: TextIO) -> int:
    """Run and visibly report one scripted ``read_file`` tool demonstration."""
    tool_use = ToolUse(tool_use_id="demo-read-1", name="read_file", path=path)
    provider = ScriptedFakeProvider(
        [
            tool_use,
            AssistantText(text="Demo final response: provider received the read_file result."),
        ]
    )
    demo_loop = AgentLoop(provider, ReadFileTool(workspace))
    stdout.write(f"[demo] provider requested read_file: {path}\n")
    response = demo_loop.run(f"Demo read {path}")
    result = provider.received_histories[1][-1]
    assert isinstance(result, ToolResult)
    if result.is_error:
        stdout.write(f"[read_file] {path}\n  ✗ {result.content}\n")
    else:
        truncation = " (truncated)" if result.truncated else ""
        preview = result.content.splitlines()[0] if result.content else "<empty file>"
        stdout.write(
            f"[read_file] {path}\n"
            f"  ✓ {len(result.content.encode('utf-8'))} UTF-8 bytes returned{truncation}\n"
            f"  preview: {preview}\n"
        )
    stdout.write(f"{response}\n")
    return 0


def render_route(
    *,
    model: str | None,
    fallback_models: Sequence[str],
    require_tool_use: bool,
    require_streaming: bool,
    max_output_tokens: int | None,
    temperature: float | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Render one offline, redacted fake-provider request plan."""
    request = RouteRequest(
        primary_selector=model or DEFAULT_ROUTE_REQUEST.primary_selector,
        fallback_selectors=tuple(fallback_models),
        requirements=RouteRequirements(
            requires_tool_use=require_tool_use,
            requires_streaming=require_streaming,
        ),
        options=GenerationOptions(
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        ),
    )
    try:
        route = resolve_route(request)
    except OrchestrationError as error:
        print(f"route error: {error}", file=stderr)
        return 2

    profiles = {profile.provider_id: profile for profile in FAKE_PROVIDER_PROFILES}
    for label, plan in (
        ("primary", route.primary),
        *(("fallback", fallback) for fallback in route.fallbacks),
    ):
        profile = profiles[plan.provider_id]
        preview = preview_request(plan)
        credential = "configured" if profile.credential_ref is not None else "not configured"
        stdout.write(f"{label}: {plan.provider_id}/{plan.model_id}\n")
        stdout.write(f"  credential: {credential}\n")
        if plan.canonical_parameters:
            canonical = ", ".join(f"{name}={value}" for name, value in plan.canonical_parameters)
            stdout.write(f"  canonical parameters: {canonical}\n")
        else:
            stdout.write("  canonical parameters: <none>\n")
        if preview.native_parameters:
            native = ", ".join(f"{name}={value}" for name, value in preview.native_parameters)
            stdout.write(f"  native preview: {native}\n")
        else:
            stdout.write("  native preview: <none>\n")
        if preview.diagnostics:
            stdout.write("  diagnostics:\n")
            for diagnostic in preview.diagnostics:
                stdout.write(
                    f"    {diagnostic.severity} {diagnostic.code}: "
                    f"{diagnostic.message} ({diagnostic.action})\n"
                )
        else:
            stdout.write("  diagnostics: <none>\n")
    return 0


def render_runtime_route(
    *,
    model: str,
    provider_protocol: str | None,
    base_url: str | None,
    api_key_env: str | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Render one real-provider route without constructing a client or reading secrets."""
    try:
        route = resolve_runtime_route(
            model,
            environment=os.environ,
            custom_protocol=provider_protocol,
            custom_base_url=base_url,
            custom_api_key_env=api_key_env,
        )
    except RuntimeRouteError as error:
        print(f"provider route error: {error}", file=stderr)
        return 2
    definition = route.definition
    credential = "not required"
    if definition.credential_env:
        credential = (
            "configured" if os.environ.get(definition.credential_env, "").strip() else "missing"
        )
    stdout.write(f"provider: {definition.provider_id}\n")
    stdout.write(f"protocol: {definition.protocol}\n")
    stdout.write(f"selected model: {route.selected_model}\n")
    stdout.write(f"wire model: {route.wire_model}\n")
    stdout.write(f"base URL: {route.base_url} ({route.base_url_source})\n")
    stdout.write(f"credential: {credential}\n")
    return 0


def render_provider_failure(error: ProviderAdapterError, stderr: TextIO) -> int:
    """Render one normalized provider failure without exposing raw SDK data."""
    failure = error.failure
    trace = f" [request {failure.request_id}]" if failure.request_id else ""
    print(f"provider error [{failure.kind}]{trace}: {failure.message}", file=stderr)
    return 2


def run_real_prompt(
    *,
    model: str,
    prompt: str,
    workspace: Path,
    provider_protocol: str | None,
    base_url: str | None,
    api_key_env: str | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Resolve and run one explicit network-capable multi-provider prompt."""
    try:
        route = resolve_runtime_route(
            model,
            environment=os.environ,
            custom_protocol=provider_protocol,
            custom_base_url=base_url,
            custom_api_key_env=api_key_env,
        )
        provider = create_provider(route, environment=os.environ)
        response = AgentLoop(provider, ReadFileTool(workspace)).run(prompt)
    except RuntimeRouteError as error:
        print(f"provider route error: {error}", file=stderr)
        return 2
    except ProviderAdapterError as error:
        return render_provider_failure(error, stderr)
    print(response, file=stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    cwd: Path | None = None,
) -> int:
    """Run a one-shot prompt command or launch the interactive terminal surface."""
    arguments = build_parser().parse_args(argv)
    workspace = cwd or Path.cwd()
    custom_provider_requested = any(
        value is not None
        for value in (arguments.provider_protocol, arguments.base_url, arguments.api_key_env)
    )
    if arguments.command == "route" and arguments.model is not None:
        return render_runtime_route(
            model=arguments.model,
            provider_protocol=arguments.provider_protocol,
            base_url=arguments.base_url,
            api_key_env=arguments.api_key_env,
            stdout=stdout or sys.stdout,
            stderr=stderr or sys.stderr,
        )
    if custom_provider_requested and (arguments.command != "prompt" or arguments.model is None):
        print(
            "provider route error: custom provider options require --model and the prompt command",
            file=stderr or sys.stderr,
        )
        return 2
    if arguments.command == "demo-read":
        return render_demo_read(workspace, arguments.path, stdout or sys.stdout)
    if arguments.command == "route":
        return render_route(
            model=arguments.route_model,
            fallback_models=arguments.fallback_model,
            require_tool_use=arguments.require_tool_use,
            require_streaming=arguments.require_streaming,
            max_output_tokens=arguments.max_output_tokens,
            temperature=arguments.temperature,
            stdout=stdout or sys.stdout,
            stderr=stderr or sys.stderr,
        )
    if arguments.command == "prompt" and arguments.model is not None:
        return run_real_prompt(
            model=arguments.model,
            prompt=arguments.prompt,
            workspace=workspace,
            provider_protocol=arguments.provider_protocol,
            base_url=arguments.base_url,
            api_key_env=arguments.api_key_env,
            stdout=stdout or sys.stdout,
            stderr=stderr or sys.stderr,
        )

    loop = AgentLoop(ScriptedFakeProvider(), ReadFileTool(workspace))
    if arguments.command == "prompt":
        print(loop.run(arguments.prompt))
        return 0

    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    if not input_stream.isatty() or not output_stream.isatty():
        print(
            'interactive mode requires a terminal; use leonervis-code prompt "..." instead',
            file=error_stream,
        )
        return 2
    return run_repl(
        loop,
        stdin=input_stream,
        stdout=output_stream,
        version=__version__,
        cwd=workspace,
        color=color_enabled(output_stream),
    )
