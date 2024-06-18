# Copyright (c) 2024 Airbyte, Inc., all rights reserved.
"""Destination base classes."""

from __future__ import annotations

import abc
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
import yaml
from rich import print
from rich.syntax import Syntax
from typing_extensions import Literal

from airbyte_protocol.models import (
    AirbyteMessage,
    ConnectorSpecification,
    Status,
    TraceType,
    Type,
)

from airbyte import exceptions as exc
from airbyte._util.telemetry import (
    EventState,
    log_config_validation_result,
    log_source_check_result,
)
from airbyte._util.temp_files import as_temp_files


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from io import IOBase

    from airbyte._executor import Executor


MAX_LOG_LINES = 20


class ConnectorBase(abc.ABC):
    """A class representing a destination that can be called."""

    connector_type: Literal["destination", "source"]

    def __init__(
        self,
        executor: Executor,
        name: str,
        config: dict[str, Any] | None = None,
        *,
        validate: bool = False,
    ) -> None:
        """Initialize the source.

        If config is provided, it will be validated against the spec if validate is True.
        """
        self.executor = executor
        self.name = name
        self._config_dict: dict[str, Any] | None = None
        self._last_log_messages: list[str] = []
        self._spec: ConnectorSpecification | None = None
        self._selected_stream_names: list[str] = []
        if config is not None:
            self.set_config(config, validate=validate)

    def set_config(
        self,
        config: dict[str, Any],
        *,
        validate: bool = True,
    ) -> None:
        """Set the config for the connector.

        If validate is True, raise an exception if the config fails validation.

        If validate is False, validation will be deferred until check() or validate_config()
        is called.
        """
        if validate:
            self.validate_config(config)

        self._config_dict = config

    def get_config(self) -> dict[str, Any]:
        """Get the config for the connector."""
        return self._config

    @property
    def _config(self) -> dict[str, Any]:
        if self._config_dict is None:
            raise exc.AirbyteConnectorConfigurationMissingError(
                guidance="Provide via get_destination() or set_config()"
            )
        return self._config_dict

    def validate_config(self, config: dict[str, Any] | None = None) -> None:
        """Validate the config against the spec.

        If config is not provided, the already-set config will be validated.
        """
        spec = self._get_spec(force_refresh=False)
        config = self._config if config is None else config
        try:
            jsonschema.validate(config, spec.connectionSpecification)
            log_config_validation_result(
                name=self.name,
                state=EventState.SUCCEEDED,
            )
        except jsonschema.ValidationError as ex:
            validation_ex = exc.AirbyteConnectorValidationFailedError(
                message="The provided config is not valid.",
                context={
                    "error_message": ex.message,
                    "error_path": ex.path,
                    "error_instance": ex.instance,
                    "error_schema": ex.schema,
                },
            )
            log_config_validation_result(
                name=self.name,
                state=EventState.FAILED,
                exception=validation_ex,
            )
            raise validation_ex from ex

    def _get_spec(self, *, force_refresh: bool = False) -> ConnectorSpecification:
        """Call spec on the connector.

        This involves the following steps:
        * execute the connector with spec
        * Listen to the messages and return the first AirbyteCatalog that comes along.
        * Make sure the subprocess is killed when the function returns.
        """
        if force_refresh or self._spec is None:
            for msg in self._execute(["spec"]):
                if msg.type == Type.SPEC and msg.spec:
                    self._spec = msg.spec
                    break

        if self._spec:
            return self._spec

        raise exc.AirbyteConnectorMissingSpecError(
            log_text=self._last_log_messages,
        )

    @property
    def config_spec(self) -> dict[str, Any]:
        """Generate a configuration spec for this connector, as a JSON Schema definition.

        This function generates a JSON Schema dictionary with configuration specs for the
        current connector, as a dictionary.

        Returns:
            dict: The JSON Schema configuration spec as a dictionary.
        """
        return self._get_spec(force_refresh=True).connectionSpecification

    def print_config_spec(
        self,
        format: Literal["yaml", "json"] = "yaml",  # noqa: A002
        *,
        output_file: Path | str | None = None,
    ) -> None:
        """Print the configuration spec for this connector.

        Args:
        - format: The format to print the spec in. Must be "yaml" or "json".
        - output_file: Optional. If set, the spec will be written to the given file path. Otherwise,
          it will be printed to the console.
        """
        if format not in {"yaml", "json"}:
            raise exc.PyAirbyteInputError(
                message="Invalid format. Expected 'yaml' or 'json'",
                input_value=format,
            )
        if isinstance(output_file, str):
            output_file = Path(output_file)

        if format == "yaml":
            content = yaml.dump(self.config_spec, indent=2)
        elif format == "json":
            content = json.dumps(self.config_spec, indent=2)

        if output_file:
            output_file.write_text(content)
            return

        syntax_highlighted = Syntax(content, format)
        print(syntax_highlighted)

    @property
    def _yaml_spec(self) -> str:
        """Get the spec as a yaml string.

        For now, the primary use case is for writing and debugging a valid config for a source.

        This is private for now because we probably want better polish before exposing this
        as a stable interface. This will also get easier when we have docs links with this info
        for each connector.
        """
        spec_obj: ConnectorSpecification = self._get_spec()
        spec_dict = spec_obj.dict(exclude_unset=True)
        # convert to a yaml string
        return yaml.dump(spec_dict)

    @property
    def docs_url(self) -> str:
        """Get the URL to the connector's documentation."""
        # TODO: Replace with docs URL from metadata when available
        return (
            f"https://docs.airbyte.com/integrations/{self.connector_type}s/"
            + self.name.lower().replace(f"{self.connector_type}-", "")
        )

    @property
    def connector_version(self) -> str | None:
        """Return the version of the connector as reported by the executor.

        Returns None if the version cannot be determined.
        """
        return self.executor.get_installed_version()

    def check(self) -> None:
        """Call check on the connector.

        This involves the following steps:
        * Write the config to a temporary file
        * execute the connector with check --config <config_file>
        * Listen to the messages and return the first AirbyteCatalog that comes along.
        * Make sure the subprocess is killed when the function returns.
        """
        with as_temp_files([self._config]) as [config_file]:
            try:
                for msg in self._execute(["check", "--config", config_file]):
                    if msg.type == Type.CONNECTION_STATUS and msg.connectionStatus:
                        if msg.connectionStatus.status != Status.FAILED:
                            print(f"Connection check succeeded for `{self.name}`.")
                            log_source_check_result(
                                name=self.name,
                                state=EventState.SUCCEEDED,
                            )
                            return

                        log_source_check_result(
                            name=self.name,
                            state=EventState.FAILED,
                        )
                        raise exc.AirbyteConnectorCheckFailedError(
                            help_url=self.docs_url,
                            context={
                                "failure_reason": msg.connectionStatus.message,
                            },
                        )
                raise exc.AirbyteConnectorCheckFailedError(log_text=self._last_log_messages)
            except exc.AirbyteConnectorReadError as ex:
                raise exc.AirbyteConnectorCheckFailedError(
                    message="The connector failed to check the connection.",
                    log_text=ex.log_text,
                ) from ex

    def install(self) -> None:
        """Install the connector if it is not yet installed."""
        self.executor.install()
        print("For configuration instructions, see: \n" f"{self.docs_url}#reference\n")

    def uninstall(self) -> None:
        """Uninstall the connector if it is installed.

        This only works if the use_local_install flag wasn't used and installation is managed by
        PyAirbyte.
        """
        self.executor.uninstall()

    def _add_to_logs(self, message: str) -> None:
        self._last_log_messages.append(message)
        if len(self._last_log_messages) > MAX_LOG_LINES:
            self._last_log_messages = self._last_log_messages[-MAX_LOG_LINES:]

    def _execute(
        self,
        args: list[str],
        *,
        stdin: IOBase | Iterable[str] | None = None,
    ) -> Iterator[AirbyteMessage]:
        """Execute the connector with the given arguments.

        This involves the following steps:
        * Locate the right venv. It is called ".venv-<connector_name>"
        * Spawn a subprocess with .venv-<connector_name>/bin/<connector-name> <args>
        * Read the output line by line of the subprocess and serialize them AirbyteMessage objects.
          Drop if not valid.
        """
        # Fail early if the connector is not installed.
        self.executor.ensure_installation(auto_fix=False)

        try:
            self._last_log_messages = []
            for line in self.executor.execute(args, stdin=stdin):
                try:
                    message = AirbyteMessage.parse_raw(line)
                    if message.type is Type.RECORD:
                        pass
                    elif message.type == Type.LOG:
                        self._add_to_logs(message.log.message)
                    elif message.type == Type.TRACE and message.trace.type == TraceType.ERROR:
                        self._add_to_logs(message.trace.error.message)
                    yield message

                except Exception:
                    self._add_to_logs(line)

        except Exception as e:
            raise exc.AirbyteConnectorReadError(
                log_text=self._last_log_messages,
            ) from e


__all__ = [
    "ConnectorBase",
]