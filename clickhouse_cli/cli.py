import http.client
import os

from uuid import uuid4
from urllib.parse import parse_qs

import click
import pygments
import sqlparse
from pygments.formatters import TerminalFormatter, TerminalTrueColorFormatter
from prompt_toolkit import Application, CommandLineInterface
from prompt_toolkit.layout.lexers import PygmentsLexer
from prompt_toolkit.shortcuts import create_eventloop, create_prompt_layout

from clickhouse_cli import __version__
from clickhouse_cli.clickhouse.client import Client, ConnectionError, DBException, TimeoutError
from clickhouse_cli.clickhouse.definitions import EXIT_COMMANDS, PRETTY_FORMATS
from clickhouse_cli.clickhouse.sqlparse_patch import KEYWORDS
from clickhouse_cli.helpers import parse_headers_stream
from clickhouse_cli.ui.lexer import CHLexer, CHPrettyFormatLexer
from clickhouse_cli.ui.prompt import (
    CLIBuffer, KeyBinder, get_continuation_tokens, get_prompt_tokens
)
from clickhouse_cli.ui.style import CHStyle, Echo, CHPygmentsStyle
from clickhouse_cli.config import read_config

# monkey-patch sqlparse
sqlparse.keywords.SQL_REGEX = CHLexer.tokens
sqlparse.keywords.KEYWORDS = KEYWORDS
sqlparse.keywords.KEYWORDS_COMMON = {}
sqlparse.keywords.KEYWORDS_ORACLE = {}

# monkey-patch http.client
http.client.parse_headers = parse_headers_stream


def show_version():
    print("clickhouse-cli version: {version}".format(version=__version__))


class CLI:

    def __init__(self, host, port, user, password, database,
                 settings, format, format_stdin, multiline, stacktrace):
        self.config = None

        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.settings = {k: v[0] for k, v in parse_qs(settings).items()}
        self.format = format
        self.format_stdin = format_stdin
        self.multiline = multiline
        self.stacktrace = stacktrace
        self.server_version = None

        self.query_ids = []
        self.client = None
        self.echo = Echo(verbose=True, colors=True)

        self.metadata = {}

    def connect(self):
        self.url = 'http://{host}:{port}/'.format(host=self.host, port=self.port)
        self.client = Client(
            self.url,
            self.user,
            self.password,
            self.database,
            self.settings,
            self.stacktrace,
            self.conn_timeout,
            self.conn_timeout_retry,
            self.conn_timeout_retry_delay,
        )

        self.echo.print("Connecting to {host}:{port}".format(
            host=self.host, port=self.port)
        )

        try:
            response = self.client.query('SELECT version();', fmt='TabSeparated')
        except TimeoutError:
            self.echo.error("Error: Connection timeout.")
            return False
        except ConnectionError:
            self.echo.error("Error: Failed to connect.")
            return False
        except DBException as e:
            self.echo.error("Error:")
            self.echo.error(e.error)

            if self.stacktrace and e.stacktrace:
                self.echo.print("Stack trace:")
                self.echo.print(e.stacktrace)

            return False

        if not response.data.endswith('\n'):
            self.echo.error("Error: Request failed: `SELECT version();` query failed.")
            return False

        version = response.data.strip().split('.')
        self.server_version = (int(version[0]), int(version[1]), int(version[2]))

        self.echo.success(
            "Connected to ClickHouse server v{0}.{1}.{2}.\n".format(
                *self.server_version
            )
        )
        return True

    def load_config(self):
        self.config = read_config()

        self.multiline = self.config.getboolean('main', 'multiline')
        self.format = self.format or self.config.get('main', 'format')
        self.format_stdin = self.format_stdin or self.config.get('main', 'format_stdin')
        self.show_formatted_query = self.config.getboolean('main', 'show_formatted_query')
        self.highlight = self.config.getboolean('main', 'highlight')
        self.highlight_output = self.config.getboolean('main', 'highlight_output')

        self.conn_timeout = self.config.getfloat('http', 'conn_timeout')
        self.conn_timeout_retry = self.config.getint('http', 'conn_timeout_retry')
        self.conn_timeout_retry_delay = self.config.getfloat('http', 'conn_timeout_retry_delay')

        self.host = self.host or self.config.get('defaults', 'host') or '127.0.0.1'
        self.port = self.port or self.config.get('defaults', 'port') or 8123
        self.user = self.user or self.config.get('defaults', 'user') or 'default'
        self.password = self.password or self.config.get('defaults', 'password') or ''
        self.database = self.database or self.config.get('defaults', 'db') or 'default'

        config_settings = dict(self.config.items('settings'))
        arg_settings = self.settings
        config_settings.update(arg_settings)
        self.settings = config_settings

        self.echo.colors = self.highlight

    def run(self, query, data):
        self.load_config()

        if data or query is not None:
            self.format = self.format_stdin
            self.echo.verbose = False

        if self.echo.verbose:
            show_version()

        if not self.connect():
            return

        if self.client:
            self.client.settings = self.settings
            self.client.cli_settings = {
                'multiline': self.multiline,
                'format': self.format,
                'format_stdin': self.format_stdin,
                'show_formatted_query': self.show_formatted_query,
                'highlight': self.highlight,
                'highlight_output': self.highlight_output,
            }

        if data and query is None:
            # cat stuff.sql | clickhouse-cli
            # clickhouse-cli stuff.sql
            for subdata in data:
                self.handle_input(
                    subdata.read(),
                    verbose=False,
                    refresh_metadata=False
                )

            return

        if not data and query is not None:
            # clickhouse-cli -q 'SELECT 1'
            return self.handle_query(
                query,
                stream=True
            )

        if data and query is not None:
            # cat stuff.csv | clickhouse-cli -q 'INSERT INTO stuff'
            # clickhouse-cli -q 'INSERT INTO stuff' stuff.csv
            for subdata in data:
                compress = 'gzip' if os.path.splitext(subdata.name)[1] == '.gz' else False

                self.handle_query(
                    query,
                    data=subdata,
                    stream=True,
                    compress=compress
                )

            return

        layout = create_prompt_layout(
            lexer=PygmentsLexer(CHLexer) if self.highlight else None,
            get_prompt_tokens=get_prompt_tokens,
            get_continuation_tokens=get_continuation_tokens,
            multiline=self.multiline,
        )

        buffer = CLIBuffer(
            client=self.client,
            multiline=self.multiline,
            metadata=self.metadata,
        )

        application = Application(
            layout=layout,
            buffer=buffer,
            style=CHStyle if self.highlight else None,
            key_bindings_registry=KeyBinder.registry,
        )

        eventloop = create_eventloop()

        self.cli = CommandLineInterface(application=application, eventloop=eventloop)
        self.cli.application.buffer.completer.refresh_metadata()

        try:
            while True:
                try:
                    cli_input = self.cli.run(reset_current_buffer=True)
                    self.handle_input(cli_input.text)
                except KeyboardInterrupt:
                    # Attempt to terminate queries
                    for query_id in self.query_ids:
                        self.client.kill_query(query_id)

                    self.echo.error("\nQuery was terminated.")
                finally:
                    self.query_ids = []
        except EOFError:
            self.echo.success("Bye.")

    def handle_input(self, input_data, verbose=True, refresh_metadata=True):
        force_pager = False
        if input_data.endswith(r'\p' if isinstance(input_data, str) else rb'\p'):
            input_data = input_data[:-2]
            force_pager = True

        # FIXME: A dirty dirty hack to make multiple queries (per one paste) work.
        self.query_ids = []
        for query in sqlparse.split(input_data):
            query_id = str(uuid4())
            self.query_ids.append(query_id)
            self.handle_query(query, verbose=verbose, query_id=query_id, force_pager=force_pager)

        if refresh_metadata and input_data:
            self.cli.application.buffer.completer.refresh_metadata()

    def handle_query(self, query, data=None, stream=False, verbose=False, query_id=None, compress=False, **kwargs):
        if query.rstrip(';') == '':
            return

        elif query.lower() in EXIT_COMMANDS:
            raise EOFError

        elif query.lower() in (r'\?', 'help'):
            rows = [
                ['', ''],
                ["clickhouse-cli's custom commands:", ''],
                ['---------------------------------', ''],
                ['USE', "Change the current database."],
                ['SET', "Set an option for the current CLI session."],
                ['QUIT', "Exit clickhouse-cli."],
                ['HELP', "Show this help message."],
                ['', ''],
                ["PostgreSQL-like custom commands:", ''],
                ['--------------------------------', ''],
                [r'\l', "Show databases."],
                [r'\c', "Change the current database."],
                [r'\d, \dt', "Show tables in the current database."],
                [r'\d+', "Show table's schema."],
                [r'\ps', "Show current queries."],
                [r'\kill', "Kill query by its ID."],
                ['', ''],
                ["Query suffixes:", ''],
                ['---------------', ''],
                [r'\g, \G', "Use the Vertical format."],
                [r'\p', "Enable the pager."],
            ]

            for row in rows:
                self.echo.success('{:<8s}'.format(row[0]), nl=False)
                self.echo.info(row[1])
            return

        elif query in (r'\d', r'\dt'):
            query = 'SHOW TABLES'

        elif query.startswith(r'\d+ '):
            query = 'DESCRIBE TABLE ' + query[4:]

        elif query == r'\l':
            query = 'SHOW DATABASES'

        elif query.startswith(r'\c '):
            query = 'USE ' + query[3:]

        elif query.startswith(r'\ps'):
            query = (
                "SELECT query_id, user, address, elapsed, {}, memory_usage "
                "FROM system.processes WHERE query_id != '{}'"
            ).format('read_rows' if self.server_version[2] >= 54115 else 'rows_read', query_id)

        elif query.startswith(r'\kill '):
            self.client.kill_query(query[6:])
            return

        response = ''

        try:
            response = self.client.query(
                query,
                fmt=self.format,
                data=data,
                stream=stream,
                verbose=verbose,
                query_id=query_id,
                compress=compress,
            )
        except TimeoutError:
            self.echo.error("Error: Connection timeout.")
            return
        except ConnectionError:
            self.echo.error("Error: Failed to connect.")
            return
        except DBException as e:
            self.echo.error("\nQuery:")
            self.echo.error(query)
            self.echo.error("\n\nReceived exception from server:")
            self.echo.error(e.error)

            if self.stacktrace and e.stacktrace:
                self.echo.print("\nStack trace:")
                self.echo.print(e.stacktrace)

            self.echo.print('\nElapsed: {elapsed:.3f} sec.\n'.format(
                elapsed=e.response.elapsed.total_seconds()
            ))

            return

        self.echo.print()

        if stream:
            data = response.iter_lines() if hasattr(response, 'iter_lines') else response.data
            for line in data:
                print(line.decode('utf-8', 'ignore'))

        else:
            if response.data != '':
                print_func = print

                if self.config.getboolean('main', 'pager') or kwargs.pop('force_pager', False):
                    print_func = self.echo.pager

                should_highlight_output = (
                    verbose and
                    self.highlight and
                    self.highlight_output and
                    response.format in PRETTY_FORMATS
                )

                formatter = TerminalFormatter()

                if self.highlight:
                    formatter = TerminalTrueColorFormatter(style=CHPygmentsStyle)

                if should_highlight_output:
                    print_func(pygments.highlight(
                        response.data,
                        CHPrettyFormatLexer(),
                        formatter
                    ))
                else:
                    print_func(response.data)

        if response.message != '':
            self.echo.print(response.message)
            self.echo.print()

        self.echo.success('Ok. ', nl=False)

        if response.rows is not None:
            self.echo.print('{rows_count} row{rows_plural} in set.'.format(
                rows_count=response.rows,
                rows_plural='s' if response.rows != 1 else '',
            ), end=' ')

        if self.config.getboolean('main', 'timing') and response.time_elapsed is not None:
            self.echo.print('Elapsed: {elapsed:.3f} sec.'.format(
                elapsed=response.time_elapsed
            ), end='')

        self.echo.print('\n')


@click.command(context_settings=dict(
    ignore_unknown_options=True,
))
@click.option('--host', '-h', help="Server host")
@click.option('--port', '-p', type=click.INT, help="Server HTTP port")
@click.option('--user', '-u', help="User")
@click.option('--password', '-P', is_flag=True, help="Password")
@click.option('--database', '-d', help="Database")
@click.option('--settings', '-s', help="Query string to be sent with every query")
@click.option('--query', '-q', help="Query to execute")
@click.option('--format', '-f', help="Data format for the interactive mode")
@click.option('--format-stdin', '-F', help="Data format for stdin/file queries")
@click.option('--multiline', '-m', is_flag=True, help="Enable multiline shell")
@click.option('--stacktrace', is_flag=True, help="Print stacktraces received from the server.")
@click.option('--version', is_flag=True, help="Show the version and exit.")
@click.argument('files', nargs=-1, type=click.File('rb'))
def run_cli(host, port, user, password, database, settings, query, format,
            format_stdin, multiline, stacktrace, version, files):
    """
    A third-party client for the ClickHouse DBMS.
    """
    if version:
        return show_version()

    if password:
        password = click.prompt("Password", hide_input=True, show_default=False, type=str)

    data_input = ()

    # Read from STDIN if non-interactive mode
    stdin = click.get_binary_stream('stdin')
    if not stdin.isatty():
        data_input += (stdin,)

    # Read the given file
    if files:
        data_input += files

    # TODO: Rename the CLI's instance into something more feasible
    cli = CLI(
        host, port, user, password, database, settings,
        format, format_stdin, multiline, stacktrace
    )
    cli.run(query, data_input)


if __name__ == '__main__':
    run_cli()
