# Copyright 2026 Google LLC
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

"""ADB server inspection commands (``artemis adb ...``).

Works for a local ADB server and for a remote one configured through
``ADB_HOST``/``ADB_PORT`` (for example a tester machine running
``adb -a -P 5037 nodaemon server``).
"""

from __future__ import annotations

import json
from typing import Annotated

from rich.console import Console
from rich.table import Table
import typer

from artemis.runtime.adb_endpoint import (
    AdbEndpoint,
    InvalidAdbEndpoint,
    current_adb_endpoint,
)
from artemis.runtime.adb_status import AdbEndpointStatus, probe_adb_endpoint

adb_app = typer.Typer(
    help="Inspect the configured ADB server (local or remote) and its devices.",
    no_args_is_help=True,
)

console = Console()

HostOption = Annotated[
    str | None,
    typer.Option("--host", "-H", help="Override the ADB server host for this call."),
]
PortOption = Annotated[
    int | None,
    typer.Option("--port", "-P", help="Override the ADB server port for this call."),
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Print machine-readable JSON instead of a table."),
]


def _resolve_endpoint(host: str | None, port: int | None) -> AdbEndpoint:
    """Configured endpoint, optionally overridden for a single call."""
    endpoint = current_adb_endpoint()
    if host is None and port is None:
        return endpoint
    try:
        return AdbEndpoint.create(host or endpoint.host, port or endpoint.port)
    except InvalidAdbEndpoint as exc:
        console.print(f"[bold red]✖ Invalid ADB endpoint:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc


def _print_status(status: AdbEndpointStatus, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(status.to_dict(), ensure_ascii=False))
        return

    endpoint = status.endpoint
    mode = "remote" if not endpoint.is_local_default else "local"
    console.print(
        f"[bold]ADB server[/bold] [cyan]{endpoint.host}:{endpoint.port}[/cyan] "
        f"[dim]({mode})[/dim]"
    )
    if not status.reachable:
        console.print("[bold red]✖ Unreachable[/bold red]")
        return
    if status.error:
        console.print(f"[bold red]✖ {status.error}[/bold red]")
        return
    if not status.devices:
        console.print("[yellow]⚠ Reachable, but no device is attached[/yellow]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Serial")
    table.add_column("State")
    table.add_column("Model")
    table.add_column("Product")
    for device in status.devices:
        state_style = "green" if device.usable else "yellow"
        table.add_row(
            device.serial,
            f"[{state_style}]{device.state}[/{state_style}]",
            device.model or "-",
            device.product or "-",
        )
    console.print(table)


@adb_app.command("devices")
def devices_command(
    host: HostOption = None,
    port: PortOption = None,
    as_json: JsonOption = False,
) -> None:
    """List the devices attached to the configured ADB server."""
    endpoint = _resolve_endpoint(host, port)
    status = probe_adb_endpoint(endpoint)
    _print_status(status, as_json)
    if not status.ok:
        hint = status.hint()
        if hint and not as_json:
            console.print(f"[yellow]💡 {hint}[/yellow]")
        raise typer.Exit(code=1)


@adb_app.command("check")
def check_command(
    host: HostOption = None,
    port: PortOption = None,
    as_json: JsonOption = False,
) -> None:
    """Verify connectivity to the ADB server and report actionable problems."""
    endpoint = _resolve_endpoint(host, port)
    status = probe_adb_endpoint(endpoint)
    _print_status(status, as_json)
    hint = status.hint()
    if hint and not as_json:
        console.print(f"[yellow]💡 {hint}[/yellow]")
    if not status.ok:
        raise typer.Exit(code=1)
    if not as_json:
        console.print("[bold green]✓ ADB endpoint ready[/bold green]")
