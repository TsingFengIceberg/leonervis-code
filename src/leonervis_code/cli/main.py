"""Command-line interface for offline policy, named profiles, and persistent sessions."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from leonervis_code import ProjectSession, __version__
from leonervis_code.agent.loop import AgentLoop
from leonervis_code.cli.brand import color_enabled
from leonervis_code.cli.presentation import (
    render_resume_rejection,
    render_prompt_event,
    render_session_resume,
    render_session_summary,
)
from leonervis_code.cli.repl import run_repl
from leonervis_code.core.contracts import AssistantText, ToolArguments, ToolResult, ToolUse
from leonervis_code.core.orchestration import (
    GenerationOptions,
    OrchestrationError,
    RouteRequest,
    RouteRequirements,
)
from leonervis_code.providers.definitions import BUILTIN_PROVIDERS, WireProtocol
from leonervis_code.providers.errors import ProviderAdapterError
from leonervis_code.providers.factory import create_provider
from leonervis_code.providers.fake import ScriptedFakeProvider
from leonervis_code.providers.manager import RuntimeProviderManager, RuntimeProviderStateError
from leonervis_code.providers.model_context import ModelContextCapabilityResolver
from leonervis_code.providers.profile import (
    NamedProviderProfile,
    ProviderProfileError,
    ProviderProfileSpec,
)
from leonervis_code.providers.profile_store import ProviderProfileStore
from leonervis_code.providers.request_context import ContextPreflightError
from leonervis_code.providers.request_policy import preview_request
from leonervis_code.providers.resolver import (
    RuntimeRouteError,
    resolve_profile_route,
    resolve_runtime_route,
)
from leonervis_code.providers.routing import (
    DEFAULT_ROUTE_REQUEST,
    FAKE_PROVIDER_PROFILES,
    resolve_route,
)
from leonervis_code.session import SessionResumeConflictError, SessionResumeContextError
from leonervis_code.session_store import (
    SessionResumeCommitError,
    SessionStore,
    SessionStoreError,
)
from leonervis_code.tools.glob import GlobTool
from leonervis_code.tools.grep import GrepTool
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
    """Create the Foundation 3C command, profile, and REPL surface."""
    parser = argparse.ArgumentParser(
        prog="leonervis-code",
        description="Leonervis Code: a learning-first local coding-agent CLI prototype.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-C", "--cwd", dest="workspace", help="workspace directory")
    parser.add_argument("--resume", help="resume latest, a session UUID, or a transcript path")
    profile_selector = parser.add_mutually_exclusive_group()
    profile_selector.add_argument("--profile", help="named endpoint profile for this invocation")
    profile_selector.add_argument(
        "--profile-id", dest="invocation_profile_id", help="profile UUID for this invocation"
    )
    parser.add_argument(
        "--model",
        dest="invocation_model",
        type=nonblank_model,
        help="direct provider/model selector, or model override with --profile",
    )
    parser.add_argument(
        "--provider-protocol",
        dest="invocation_provider_protocol",
        choices=["openai-compatible"],
        help="explicit custom provider wire protocol",
    )
    parser.add_argument(
        "--base-url", dest="invocation_base_url", help="custom provider API base URL"
    )
    parser.add_argument(
        "--api-key-env",
        dest="invocation_api_key_env",
        help="environment variable holding a custom API key",
    )
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
        "--fallback-model", action="append", default=[], help="ordered fallback selector"
    )
    route_parser.add_argument("--require-tool-use", action="store_true")
    route_parser.add_argument("--require-streaming", action="store_true")
    route_parser.add_argument("--max-output-tokens", type=int)
    route_parser.add_argument("--temperature", type=float)

    provider_parser = subcommands.add_parser("provider", help="manage named provider profiles")
    provider_commands = provider_parser.add_subparsers(dest="provider_command", required=True)
    add_parser = provider_commands.add_parser("add", help="add one global provider profile")
    add_parser.add_argument("name")
    add_parser.add_argument("--provider", required=True, choices=[*BUILTIN_PROVIDERS, "custom"])
    add_parser.add_argument("--model", dest="profile_model", required=True, type=nonblank_model)
    add_parser.add_argument(
        "--protocol",
        dest="profile_protocol",
        choices=["openai-compatible", "anthropic-messages"],
    )
    add_parser.add_argument("--base-url", dest="profile_base_url")
    add_parser.add_argument("--api-key-env", dest="profile_api_key_env")
    add_parser.add_argument("--max-output-tokens", type=int, default=1024)
    add_parser.add_argument("--context-window-tokens", type=int)
    add_parser.add_argument("--model-max-output-tokens", type=int)
    add_parser.add_argument("--temperature", type=float)
    add_parser.add_argument("--replace", action="store_true")
    add_parser.add_argument("--if-revision", type=int)
    list_parser = provider_commands.add_parser("list", help="list global provider profiles")
    list_parser.add_argument("--show-ids", action="store_true")
    show_parser = provider_commands.add_parser("show", help="show one redacted provider profile")
    show_selector = show_parser.add_mutually_exclusive_group(required=True)
    show_selector.add_argument("name", nargs="?")
    show_selector.add_argument("--id", dest="profile_id")
    use_parser = provider_commands.add_parser("use", help="activate one provider profile")
    use_selector = use_parser.add_mutually_exclusive_group(required=True)
    use_selector.add_argument("name", nargs="?")
    use_selector.add_argument("--id", dest="profile_id")
    use_parser.add_argument("--scope", choices=["project", "user"], default="project")
    clear_parser = provider_commands.add_parser("clear", help="clear one active profile layer")
    clear_parser.add_argument("--scope", choices=["project", "user"], default="project")
    remove_parser = provider_commands.add_parser("remove", help="remove an inactive profile")
    remove_selector = remove_parser.add_mutually_exclusive_group(required=True)
    remove_selector.add_argument("name", nargs="?")
    remove_selector.add_argument("--id", dest="profile_id")
    remove_parser.add_argument("--if-revision", type=int)
    rename_parser = provider_commands.add_parser("rename", help="rename one provider profile")
    rename_selector = rename_parser.add_mutually_exclusive_group(required=True)
    rename_selector.add_argument("name", nargs="?")
    rename_selector.add_argument("--id", dest="profile_id")
    rename_parser.add_argument("new_name")
    rename_parser.add_argument("--if-revision", type=int)
    replace_parser = provider_commands.add_parser(
        "replace", help="replace one profile configuration"
    )
    replace_parser.add_argument("name")
    replace_parser.add_argument("--provider", required=True, choices=[*BUILTIN_PROVIDERS, "custom"])
    replace_parser.add_argument("--model", dest="profile_model", required=True, type=nonblank_model)
    replace_parser.add_argument(
        "--protocol", dest="profile_protocol", choices=["openai-compatible", "anthropic-messages"]
    )
    replace_parser.add_argument("--base-url", dest="profile_base_url")
    replace_parser.add_argument("--api-key-env", dest="profile_api_key_env")
    replace_parser.add_argument("--max-output-tokens", type=int, default=1024)
    replace_parser.add_argument("--context-window-tokens", type=int)
    replace_parser.add_argument("--model-max-output-tokens", type=int)
    replace_parser.add_argument("--temperature", type=float)
    replace_parser.add_argument("--if-revision", type=int)
    provider_commands.add_parser("migrate", help="upgrade readable profile files to schema v4")

    session_parser = subcommands.add_parser("session", help="inspect durable workspace sessions")
    session_commands = session_parser.add_subparsers(dest="session_command", required=True)
    session_commands.add_parser("list", help="list durable sessions")
    session_show = session_commands.add_parser("show", help="show one durable session")
    session_show.add_argument("selector", nargs="?", default="latest")
    return parser


def render_demo_read(workspace: Path, path: str, stdout: TextIO) -> int:
    """Run and visibly report one scripted ``read_file`` tool demonstration."""
    tool_use = ToolUse(
        tool_use_id="demo-read-1",
        name="read_file",
        arguments=ToolArguments.from_mapping({"path": path}),
    )
    provider = ScriptedFakeProvider(
        [tool_use, AssistantText("Demo final response: provider received the read_file result.")]
    )
    demo_loop = AgentLoop(
        provider,
        ReadFileTool(workspace),
        GlobTool(workspace),
        GrepTool(workspace),
    )
    stdout.write(f"[demo] provider requested read_file: {path}\n")
    response = demo_loop.run(f"Demo read {path}")
    result = provider.received_requests[1].history[-1]
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
    """Render one offline fake-provider request-policy plan."""
    request = RouteRequest(
        primary_selector=model or DEFAULT_ROUTE_REQUEST.primary_selector,
        fallback_selectors=tuple(fallback_models),
        requirements=RouteRequirements(require_tool_use, require_streaming),
        options=GenerationOptions(max_output_tokens, temperature),
    )
    try:
        route = resolve_route(request)
    except OrchestrationError as error:
        print(f"route error: {error}", file=stderr)
        return 2
    profiles = {profile.provider_id: profile for profile in FAKE_PROVIDER_PROFILES}
    for label, plan in (
        ("primary", route.primary),
        *(("fallback", item) for item in route.fallbacks),
    ):
        profile = profiles[plan.provider_id]
        preview = preview_request(plan)
        credential = "configured" if profile.credential_ref is not None else "not configured"
        stdout.write(f"{label}: {plan.provider_id}/{plan.model_id}\n")
        stdout.write(f"  credential: {credential}\n")
        canonical = ", ".join(f"{name}={value}" for name, value in plan.canonical_parameters)
        native = ", ".join(f"{name}={value}" for name, value in preview.native_parameters)
        stdout.write(f"  canonical parameters: {canonical or '<none>'}\n")
        stdout.write(f"  native preview: {native or '<none>'}\n")
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
    route,
    environment: Mapping[str, str],
    stdout: TextIO,
    *,
    profile_override: int | None = None,
    model_max_output_override: int | None = None,
) -> int:
    """Render one real route without constructing a client or exposing secrets."""
    definition = route.definition
    capability = ModelContextCapabilityResolver().resolve_offline(
        route,
        profile_override=profile_override,
        model_max_output_override=model_max_output_override,
    )
    credential = "not required"
    if definition.credential_env:
        credential = (
            "configured" if environment.get(definition.credential_env, "").strip() else "missing"
        )
    stdout.write(f"provider: {definition.provider_id}\n")
    stdout.write(f"protocol: {definition.protocol}\n")
    stdout.write(f"selected model: {route.selected_model}\n")
    stdout.write(f"wire model: {route.wire_model}\n")
    stdout.write(f"base URL: {route.base_url} ({route.base_url_source})\n")
    stdout.write(f"credential: {credential}\n")
    context = capability.context_window_tokens or "unknown"
    stdout.write(f"context window: {context} ({capability.source.value})\n")
    model_output = capability.model_max_output_tokens or "unknown"
    stdout.write(f"model max output: {model_output} ({capability.model_max_output_source.value})\n")
    stdout.write(f"requested output reserve: {route.max_output_tokens}\n")
    if capability.diagnostic:
        stdout.write(f"context diagnostic: {capability.diagnostic}\n")
    return 0


def render_provider_failure(error: ProviderAdapterError, stderr: TextIO) -> int:
    """Render one normalized provider failure without exposing raw SDK data."""
    failure = error.failure
    trace = f" [request {failure.request_id}]" if failure.request_id else ""
    print(f"provider error [{failure.kind}]{trace}: {failure.message}", file=stderr)
    return 2


def render_profile(
    profile: NamedProviderProfile, environment: Mapping[str, str], stdout: TextIO
) -> None:
    """Render one profile's non-secret endpoint configuration."""
    credential = "not required"
    if profile.api_key_env:
        credential = "configured" if environment.get(profile.api_key_env, "").strip() else "missing"
    elif profile.provider_id in BUILTIN_PROVIDERS:
        name = BUILTIN_PROVIDERS[profile.provider_id].credential_env
        if name:
            credential = "configured" if environment.get(name, "").strip() else "missing"
    stdout.write(f"name: {profile.name}\n")
    stdout.write(f"profile ID: {profile.profile_id}\n")
    stdout.write(f"revision: {profile.revision}\n")
    stdout.write(f"provider: {profile.provider_id}\n")
    stdout.write(f"protocol: {profile.protocol.value}\n")
    stdout.write(f"model: {profile.model}\n")
    stdout.write(f"base URL: {profile.base_url or '<provider default>'}\n")
    stdout.write(f"credential: {credential}\n")
    stdout.write(f"max output tokens: {profile.max_output_tokens}\n")
    stdout.write(
        "context window override: "
        f"{profile.context_window_tokens if profile.context_window_tokens is not None else '<none>'}\n"
    )
    stdout.write(
        "model max output override: "
        f"{profile.model_max_output_tokens if profile.model_max_output_tokens is not None else '<none>'}\n"
    )
    stdout.write(
        f"temperature: {profile.temperature if profile.temperature is not None else '<default>'}\n"
    )


def _profile_protocol(provider_id: str, option: str | None) -> WireProtocol:
    if provider_id in BUILTIN_PROVIDERS:
        expected = BUILTIN_PROVIDERS[provider_id].protocol
        if option is not None and option != _protocol_option(expected):
            raise ProviderProfileError(
                f"profile protocol does not match built-in provider {provider_id}"
            )
        return expected
    if option != "openai-compatible":
        raise ProviderProfileError("custom profiles require --protocol openai-compatible")
    return WireProtocol.OPENAI_CHAT_COMPLETIONS


def _protocol_option(protocol: WireProtocol) -> str:
    return (
        "anthropic-messages" if protocol == WireProtocol.ANTHROPIC_MESSAGES else "openai-compatible"
    )


def _store(
    workspace: Path,
    environment: Mapping[str, str],
    user_profile_path: Path | None,
    project_profile_path: Path | None,
) -> ProviderProfileStore:
    return ProviderProfileStore.for_workspace(
        workspace,
        environment=environment,
        user_path=user_profile_path,
        project_path=project_profile_path,
    )


def _profile_spec(arguments: argparse.Namespace) -> ProviderProfileSpec:
    return ProviderProfileSpec(
        name=arguments.name,
        provider_id=arguments.provider,
        protocol=_profile_protocol(arguments.provider, arguments.profile_protocol),
        model=arguments.profile_model,
        base_url=arguments.profile_base_url,
        api_key_env=arguments.profile_api_key_env,
        max_output_tokens=arguments.max_output_tokens,
        context_window_tokens=arguments.context_window_tokens,
        model_max_output_tokens=arguments.model_max_output_tokens,
        temperature=arguments.temperature,
    )


def _selected_profile(
    store: ProviderProfileStore, arguments: argparse.Namespace
) -> NamedProviderProfile:
    profile_id = getattr(arguments, "profile_id", None)
    return store.get_profile_by_id(profile_id) if profile_id else store.get_profile(arguments.name)


def handle_provider_command(
    arguments: argparse.Namespace,
    *,
    workspace: Path,
    environment: Mapping[str, str],
    user_profile_path: Path | None,
    project_profile_path: Path | None,
    provider_factory,
    stdout: TextIO,
) -> int:
    """Execute one profile CRUD or activation command."""
    store = _store(workspace, environment, user_profile_path, project_profile_path)
    command = arguments.provider_command
    if command == "add":
        configured = store.add_profile(
            _profile_spec(arguments),
            replace=arguments.replace,
            expected_revision=arguments.if_revision,
        )
        stdout.write(f"Saved provider profile {configured.name}.\n")
    elif command == "replace":
        current = store.get_profile(arguments.name)
        configured = store.replace_profile(
            current.profile_id,
            _profile_spec(arguments),
            expected_revision=arguments.if_revision,
        )
        stdout.write(
            f"Replaced provider profile {configured.name} at revision {configured.revision}.\n"
        )
    elif command == "list":
        profiles = store.list_profiles()
        active = store.active_selection()
        if not profiles:
            stdout.write("No provider profiles configured.\n")
        for configured in profiles:
            marker = " *" if active and active.profile_id == configured.profile_id else ""
            identity = (
                f" [{configured.profile_id} r{configured.revision}]" if arguments.show_ids else ""
            )
            stdout.write(
                f"{configured.name}{marker}: {configured.provider_id}/{configured.model}{identity}\n"
            )
    elif command == "show":
        render_profile(_selected_profile(store, arguments), environment, stdout)
    elif command == "remove":
        configured = _selected_profile(store, arguments)
        store.remove_profile_by_id(configured.profile_id, expected_revision=arguments.if_revision)
        stdout.write(f"Removed provider profile {configured.name}.\n")
    elif command == "rename":
        configured = _selected_profile(store, arguments)
        renamed = store.rename_profile(
            configured.profile_id,
            arguments.new_name,
            expected_revision=arguments.if_revision,
        )
        stdout.write(f"Renamed provider profile {configured.name} to {renamed.name}.\n")
    elif command == "migrate":
        store.migrate()
        stdout.write("Migrated provider configuration to schema v4.\n")
    elif command == "clear":
        RuntimeProviderManager.prepare_clear(
            store,
            scope=arguments.scope,
            environment=environment,
            provider_factory=provider_factory,
        )
        stdout.write(f"Cleared {arguments.scope} active provider profile.\n")
    elif command == "use":
        configured = _selected_profile(store, arguments)
        status = RuntimeProviderManager.prepare_profile(
            store,
            configured.name,
            scope=arguments.scope,
            environment=environment,
            provider_factory=provider_factory,
        )
        stdout.write(f"Using provider profile {status.profile} at {arguments.scope} scope.\n")
    return 0


def render_session_info(info, stdout: TextIO) -> None:
    """Render durable Session metadata without transcript content."""
    stdout.write(f"session ID: {info.session_id}\n")
    stdout.write(f"workspace: {info.workspace}\n")
    stdout.write(f"transcript: {info.path}\n")
    stdout.write(f"created: {info.created_at}\n")
    stdout.write(f"turns: {info.turn_count}\n")
    stdout.write(f"records: {info.record_count}\n")
    stdout.write(f"closed: {'yes' if info.closed else 'no'}\n")
    stdout.write(f"last provider: {info.binding.provider_id}\n")
    stdout.write(f"last model: {info.binding.selected_model or '<none>'}\n")


def handle_session_command(arguments: argparse.Namespace, workspace: Path, stdout: TextIO) -> int:
    """List or inspect validated Session transcripts without taking a writer lease."""
    store = SessionStore(workspace)
    if arguments.session_command == "show":
        render_session_info(store.show(arguments.selector), stdout)
        return 0
    sessions = store.list()
    if not sessions:
        stdout.write("No durable sessions found.\n")
        return 0
    latest_id = store.show("latest").session_id
    for info in sessions:
        stdout.write(f"{render_session_summary(info, latest_session_id=latest_id)}\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    user_profile_path: Path | None = None,
    project_profile_path: Path | None = None,
    provider_factory=None,
) -> int:
    """Run a command or launch one persistent project conversation session."""
    arguments = build_parser().parse_args(argv)
    workspace = (
        Path(arguments.workspace).resolve()
        if arguments.workspace
        else (cwd or Path.cwd()).resolve()
    )
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    env = os.environ if environment is None else environment
    factory = provider_factory or create_provider
    custom_requested = any(
        value is not None
        for value in (
            arguments.invocation_provider_protocol,
            arguments.invocation_base_url,
            arguments.invocation_api_key_env,
        )
    )
    try:
        if arguments.resume is not None and arguments.command not in {None, "prompt"}:
            raise ProviderProfileError("--resume is only valid with prompt or interactive mode")
        if arguments.invocation_profile_id is not None:
            arguments.profile = (
                _store(workspace, env, user_profile_path, project_profile_path)
                .get_profile_by_id(arguments.invocation_profile_id)
                .name
            )
        if arguments.profile is not None and custom_requested:
            raise ProviderProfileError("--profile cannot be combined with custom endpoint options")
        if arguments.command == "provider":
            if (
                arguments.profile is not None
                or arguments.invocation_profile_id is not None
                or arguments.invocation_model is not None
                or custom_requested
            ):
                raise ProviderProfileError(
                    "global provider selection options cannot be combined with provider management"
                )
            return handle_provider_command(
                arguments,
                workspace=workspace,
                environment=env,
                user_profile_path=user_profile_path,
                project_profile_path=project_profile_path,
                provider_factory=factory,
                stdout=output,
            )
        if arguments.command == "session":
            if (
                arguments.profile is not None
                or arguments.invocation_model is not None
                or custom_requested
            ):
                raise ProviderProfileError(
                    "provider selection options cannot be combined with session inspection"
                )
            return handle_session_command(arguments, workspace, output)
        if arguments.command == "demo-read":
            if (
                arguments.profile is not None
                or arguments.invocation_model is not None
                or custom_requested
            ):
                raise ProviderProfileError("demo-read does not accept provider selection options")
            return render_demo_read(workspace, arguments.path, output)
        if arguments.command == "route":
            if arguments.profile is not None:
                configured = _store(
                    workspace, env, user_profile_path, project_profile_path
                ).get_profile(arguments.profile)
                route = resolve_profile_route(
                    configured, environment=env, model_override=arguments.invocation_model
                )
                return render_runtime_route(
                    route,
                    env,
                    output,
                    profile_override=(
                        configured.context_window_tokens
                        if arguments.invocation_model is None
                        else None
                    ),
                    model_max_output_override=(
                        configured.model_max_output_tokens
                        if arguments.invocation_model is None
                        else None
                    ),
                )
            if arguments.invocation_model is not None:
                route = resolve_runtime_route(
                    arguments.invocation_model,
                    environment=env,
                    custom_protocol=arguments.invocation_provider_protocol,
                    custom_base_url=arguments.invocation_base_url,
                    custom_api_key_env=arguments.invocation_api_key_env,
                )
                return render_runtime_route(route, env, output)
            if custom_requested:
                raise ProviderProfileError("custom endpoint options require --model")
            return render_route(
                model=arguments.route_model,
                fallback_models=arguments.fallback_model,
                require_tool_use=arguments.require_tool_use,
                require_streaming=arguments.require_streaming,
                max_output_tokens=arguments.max_output_tokens,
                temperature=arguments.temperature,
                stdout=output,
                stderr=errors,
            )
        if custom_requested and (
            arguments.command != "prompt" or arguments.invocation_model is None
        ):
            raise ProviderProfileError(
                "custom endpoint options require --model and the prompt command"
            )
        if arguments.command is None:
            input_stream = stdin or sys.stdin
            if not input_stream.isatty() or not output.isatty():
                print(
                    'interactive mode requires a terminal; use leonervis-code prompt "..." instead',
                    file=errors,
                )
                return 2
        session = ProjectSession.open(
            workspace,
            resume=arguments.resume,
            profile=arguments.profile,
            model=arguments.invocation_model,
            custom_protocol=arguments.invocation_provider_protocol,
            custom_base_url=arguments.invocation_base_url,
            custom_api_key_env=arguments.invocation_api_key_env,
            environment=env,
            user_profile_path=user_profile_path,
            project_profile_path=project_profile_path,
            provider_factory=factory,
            read_file_factory=ReadFileTool,
            glob_factory=GlobTool,
            grep_factory=GrepTool,
        )
        try:
            resume_result = session.startup_resume_result
            if resume_result is not None:
                message, _ = render_session_resume(resume_result)
                print(message, file=errors)
            if arguments.command == "prompt":

                def prompt_event_sink(event) -> None:
                    message, _ = render_prompt_event(event)
                    print(message, file=errors, flush=True)

                print(
                    session.prompt(arguments.prompt, event_sink=prompt_event_sink),
                    file=output,
                )
                return 0
            return run_repl(
                session,
                stdin=stdin or sys.stdin,
                stdout=output,
                version=__version__,
                cwd=workspace,
                color=color_enabled(output, env),
            )
        finally:
            session.close()
    except RuntimeProviderStateError as error:
        print(f"provider runtime state error: {error}", file=errors)
        return 2
    except RuntimeRouteError as error:
        print(f"provider route error: {error}", file=errors)
        return 2
    except ContextPreflightError as error:
        print(f"context preflight error: {error}", file=errors)
        return 2
    except ProviderAdapterError as error:
        return render_provider_failure(error, errors)
    except ProviderProfileError as error:
        print(f"provider profile error: {error}", file=errors)
        return 2
    except SessionResumeContextError as error:
        print(render_resume_rejection(error.report, startup=True), file=errors)
        return 2
    except SessionResumeConflictError as error:
        print(f"session resume conflict: {error}", file=errors)
        return 2
    except SessionResumeCommitError as error:
        print(f"session resume commit error [{error.stage.value}]: {error}", file=errors)
        return 2
    except SessionStoreError as error:
        print(f"session error: {error}", file=errors)
        return 2
