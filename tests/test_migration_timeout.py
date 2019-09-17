import time

import pytest

from django.apps import apps
from django.db import (
    IntegrityError,
    connection,
    connections,
    migrations,
    models,
)
from django.db.migrations import CreateModel
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import InterfaceError as DjangoInterfaceError
from django.db.utils import OperationalError as DjangoOperationalError
from psycopg2 import InterfaceError as Psycopg2InterfaceError
from psycopg2 import OperationalError as Psycopg2OperationalError

import psqlextra.indexes.conditional_unique_index

from psqlextra.backend.migrations.patched_migrations import MigrationWithTimeout
from tests.fake_model import define_fake_model
from tests.migrations import apply_migration, expectation_judge


@pytest.fixture(autouse=True)
def establish_connection():
    """Fixture used for tests which mess up the connection to the database."""
    yield
    connection.ensure_connection()


@pytest.fixture()
def repair_credentials():
    """Fixture used to change the role used for the connection."""
    yield
    connections["pw_test"].settings_dict["USER"] = "tester_role"


def apply_patched_migration_with_timeout(
    operations,
    state=None,
    backwards: bool = False,
    safe_interrupt: bool = True,
    timeout: float = None,
    cancel_method: MigrationWithTimeout.CancellationMethods = MigrationWithTimeout.CancellationMethods.SQL,
    connection_name: str = "default",
):
    """Executes the specified migration operations using the specified schema
    editor.

    Arguments:
        operations:
            The migration operations to execute.

        state:
            The state state to use during the
            migrations.

        backwards:
            Whether to apply the operations
            in reverse (backwards).

        timeout:
            Cancel the operation if it takes
            more than x seconds

        safe_interrupt:
            Cancel safely or kill the connection
            entirely

        cancel_method:
            Tells the migration class how to
            abort the currently running operation

        connection_name:
            Tell the migration executor on which
            database to perform the migration
    """

    state = state or migrations.state.ProjectState.from_apps(apps)

    migration = MigrationWithTimeout("migration", "tests")
    migration.safe_sql_interrupt = safe_interrupt
    migration.operations = operations
    migration.timeout = timeout
    migration.cancellation_method = cancel_method

    executor = MigrationExecutor(
        connection
        if connection_name == "default"
        else connections[connection_name]
    )

    if not backwards:
        executor.apply_migration(state, migration)
    else:
        executor.unapply_migration(state, migration)

    return migration


@pytest.mark.parametrize(
    "timeout, stalling_time, expect_interruption",
    [(0.25, 0.5, True), (None, 0.2, False), (0.5, 0.25, False)],
)
def test_migration_timeout_python_code(
    timeout, stalling_time, expect_interruption
):
    """Test migration timeout if running python code."""

    def stall(*unused):
        time.sleep(stalling_time)

    expectation_judge(
        expect_interruption,
        apply_patched_migration_with_timeout,
        [migrations.RunPython(stall)],
        exception_expected=KeyboardInterrupt,
        timeout=timeout,
        cancel_method=MigrationWithTimeout.CancellationMethods.PYTHON,
    )


@pytest.mark.parametrize(
    "timeout, stalling_time, expect_interruption",
    [(0.25, 0.5, True), (None, 0.2, False), (0.5, 0.25, False)],
)
def test_migration_timeout_add_index(
    timeout, stalling_time, expect_interruption
):
    """Test add index."""
    field_name = "model_id"
    objects_added = 100
    model_fields = {field_name: models.IntegerField}

    def prepare_fields():
        return {name: field() for name, field in model_fields.items()}

    model = define_fake_model(fields=prepare_fields())
    apply_migration([CreateModel(model.__name__, prepare_fields().items())])

    model.objects.bulk_create(
        [model(**{field_name: x}) for x in range(objects_added)]
    )

    assert model.objects.all().count() == objects_added

    operations = [
        migrations.RunSQL(f"select pg_sleep({stalling_time});"),
        migrations.AddIndex(
            model_name=model.__name__,
            index=psqlextra.indexes.conditional_unique_index.ConditionalUniqueIndex(
                condition=f'"{field_name}" IS NOT NULL',
                fields=[field_name],
                name=f"{field_name}_idx",
            ),
        ),
    ]

    expectation_judge(
        expect_interruption,
        apply_patched_migration_with_timeout,
        operations,
        exception_expected=DjangoOperationalError,
        timeout=timeout,
    )
    expectation_judge(
        not expect_interruption,
        model.objects.bulk_create,
        [model(**{field_name: 0})],
        exception_expected=IntegrityError,
        with_transaction_wrapper=True,
    )

    assert model.objects.all().count() == objects_added + expect_interruption


@pytest.mark.parametrize(
    "timeout, stalling_time, expect_interruption",
    [(0.25, 0.5, True), (0.2, 0.1, False)],
)
def test_migration_timeout_force_close_sql_connection(
    timeout, stalling_time, expect_interruption, establish_connection
):
    assert connection.is_usable()

    expectation_judge(
        expect_interruption,
        apply_patched_migration_with_timeout,
        [migrations.RunSQL(f"select pg_sleep({stalling_time});")],
        exception_expected=(
            DjangoOperationalError,
            Psycopg2InterfaceError,
            DjangoInterfaceError,
        ),
        timeout=timeout,
        cancel_method=MigrationWithTimeout.CancellationMethods.SQL,
        safe_interrupt=False,
    )

    assert connection.is_usable() != expect_interruption

    expectation_judge(
        expect_interruption,
        apply_patched_migration_with_timeout,
        [migrations.RunSQL(f"select pg_sleep({stalling_time});")],
        exception_expected=DjangoInterfaceError,
        timeout=timeout,
        cancel_method=MigrationWithTimeout.CancellationMethods.SQL,
    )


@pytest.mark.parametrize(
    "timeout, stalling_time, expect_interruption",
    [(0.25, 0.5, True), (None, 0.2, False), (0.5, 0.1, False)],
)
def test_migration_timeout_both_cancelling_methods_active(
    timeout, stalling_time, expect_interruption
):
    """Test migration timeout if running python code."""

    def stall(*unused):
        time.sleep(stalling_time)

    expectation_judge(
        expect_interruption,
        apply_patched_migration_with_timeout,
        [
            migrations.RunSQL(f"select pg_sleep({stalling_time});"),
            migrations.RunPython(stall),
        ],
        exception_expected=KeyboardInterrupt,
        timeout=timeout,
        cancel_method=MigrationWithTimeout.CancellationMethods.BOTH,
    )


@pytest.mark.parametrize(
    "timeout, stalling_time, expect_interruption",
    [(0.25, 0.5, True), (None, 0.2, False), (0.5, 0.1, False)],
)
def test_migration_timeout_on_database_with_username_and_password(
    timeout, stalling_time, expect_interruption
):
    """Test migration timeout with different database, one with username and
    password."""

    expectation_judge(
        expect_interruption,
        apply_patched_migration_with_timeout,
        [migrations.RunSQL(f"select pg_sleep({stalling_time});")],
        exception_expected=DjangoOperationalError,
        timeout=timeout,
        connection_name="pw_test",
    )


@pytest.mark.parametrize("timeout, stalling_time", [(0.25, 0.5), (None, 0.2)])
def test_migration_timeout_on_database_with_invalid_role(
    timeout, stalling_time, repair_credentials
):
    """Test migration timeout with different database, with inexisting role."""

    connections["pw_test"].settings_dict["USER"] = "obviously_wrong_role"

    expectation_judge(
        False,
        apply_patched_migration_with_timeout,
        [migrations.RunSQL(f"select pg_sleep({stalling_time});")],
        exception_expected=Psycopg2OperationalError,
        timeout=timeout,
        connection_name="pw_test",
    )
