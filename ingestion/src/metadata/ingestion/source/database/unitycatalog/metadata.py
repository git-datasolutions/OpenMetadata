#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Databricks Unity Catalog Source source methods.
"""
import json
import traceback
from typing import Any, Iterable, List, Optional, Tuple, Union

from databricks.sdk.service.catalog import ColumnInfo
from databricks.sdk.service.catalog import TableConstraint as DBTableConstraint

from metadata.generated.schema.api.data.createDatabase import CreateDatabaseRequest
from metadata.generated.schema.api.data.createDatabaseSchema import (
    CreateDatabaseSchemaRequest,
)
from metadata.generated.schema.api.data.createQuery import CreateQueryRequest
from metadata.generated.schema.api.data.createStoredProcedure import (
    CreateStoredProcedureRequest,
)
from metadata.generated.schema.api.data.createTable import CreateTableRequest
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.entity.data.database import Database
from metadata.generated.schema.entity.data.databaseSchema import DatabaseSchema
from metadata.generated.schema.entity.data.table import (
    Column,
    ConstraintType,
    Table,
    TableConstraint,
    TableType,
)
from metadata.generated.schema.entity.services.connections.database.unityCatalogConnection import (
    UnityCatalogConnection,
)
from metadata.generated.schema.entity.services.ingestionPipelines.status import (
    StackTraceError,
)
from metadata.generated.schema.metadataIngestion.databaseServiceMetadataPipeline import (
    DatabaseServiceMetadataPipeline,
)
from metadata.generated.schema.metadataIngestion.workflow import (
    Source as WorkflowSource,
)
from metadata.ingestion.api.models import Either
from metadata.ingestion.api.steps import InvalidSourceException
from metadata.ingestion.lineage.sql_lineage import get_column_fqn
from metadata.ingestion.models.ometa_classification import OMetaTagAndClassification
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.database.column_type_parser import ColumnTypeParser
from metadata.ingestion.source.database.database_service import DatabaseServiceSource
from metadata.ingestion.source.database.multi_db_source import MultiDBSource
from metadata.ingestion.source.database.stored_procedures_mixin import QueryByProcedure
from metadata.ingestion.source.database.unitycatalog.connection import get_connection
from metadata.ingestion.source.database.unitycatalog.models import (
    ColumnJson,
    ElementType,
    ForeignConstrains,
    Type,
)
from metadata.ingestion.source.models import TableView
from metadata.utils import fqn
from metadata.utils.db_utils import get_view_lineage
from metadata.utils.filters import filter_by_database, filter_by_schema, filter_by_table
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()


class UnitycatalogSource(DatabaseServiceSource, MultiDBSource):
    """
    Implements the necessary methods to extract
    Database metadata from Databricks Source using
    the unity catalog source
    """

    def __init__(self, config: WorkflowSource, metadata: OpenMetadata):
        super().__init__()
        self.config = config
        self.source_config: DatabaseServiceMetadataPipeline = (
            self.config.sourceConfig.config
        )
        self.context.table_views = []
        self.metadata = metadata
        self.service_connection: UnityCatalogConnection = (
            self.config.serviceConnection.__root__.config
        )
        self.client = get_connection(self.service_connection)
        self.connection_obj = self.client
        self.table_constraints = []
        self.test_connection()

    def get_configured_database(self) -> Optional[str]:
        return self.service_connection.catalog

    def get_database_names_raw(self) -> Iterable[str]:
        for catalog in self.client.catalogs.list():
            yield catalog.name

    @classmethod
    def create(
        cls, config_dict, metadata: OpenMetadata, pipeline_name: Optional[str] = None
    ):
        config: WorkflowSource = WorkflowSource.parse_obj(config_dict)
        connection: UnityCatalogConnection = config.serviceConnection.__root__.config
        if not isinstance(connection, UnityCatalogConnection):
            raise InvalidSourceException(
                f"Expected UnityCatalogConnection, but got {connection}"
            )
        return cls(config, metadata)

    def get_database_names(self) -> Iterable[str]:
        """
        Default case with a single database.

        It might come informed - or not - from the source.

        Sources with multiple databases should overwrite this and
        apply the necessary filters.

        Catalog ID -> Database
        """
        if self.service_connection.catalog:
            yield self.service_connection.catalog
        else:
            for catalog_name in self.get_database_names_raw():
                try:
                    database_fqn = fqn.build(
                        self.metadata,
                        entity_type=Database,
                        service_name=self.context.database_service,
                        database_name=catalog_name,
                    )
                    if filter_by_database(
                        self.config.sourceConfig.config.databaseFilterPattern,
                        database_fqn
                        if self.config.sourceConfig.config.useFqnForFiltering
                        else catalog_name,
                    ):
                        self.status.filter(
                            database_fqn,
                            "Database (Catalog ID) Filtered Out",
                        )
                        continue
                    yield catalog_name
                except Exception as exc:
                    self.status.failed(
                        StackTraceError(
                            name=catalog_name,
                            error=f"Unexpected exception to get database name [{catalog_name}]: {exc}",
                            stackTrace=traceback.format_exc(),
                        )
                    )

    def yield_database(
        self, database_name: str
    ) -> Iterable[Either[CreateDatabaseRequest]]:
        """
        From topology.
        Prepare a database request and pass it to the sink
        """
        yield Either(
            right=CreateDatabaseRequest(
                name=database_name,
                service=self.context.database_service,
            )
        )

    def get_database_schema_names(self) -> Iterable[str]:
        """
        return schema names
        """
        catalog_name = self.context.database
        for schema in self.client.schemas.list(catalog_name=catalog_name):
            try:
                schema_fqn = fqn.build(
                    self.metadata,
                    entity_type=DatabaseSchema,
                    service_name=self.context.database_service,
                    database_name=self.context.database,
                    schema_name=schema.name,
                )
                if filter_by_schema(
                    self.config.sourceConfig.config.schemaFilterPattern,
                    schema_fqn
                    if self.config.sourceConfig.config.useFqnForFiltering
                    else schema.name,
                ):
                    self.status.filter(schema_fqn, "Schema Filtered Out")
                    continue
                yield schema.name
            except Exception as exc:
                self.status.failed(
                    StackTraceError(
                        name=schema.name,
                        error=f"Unexpected exception to get database schema [{schema.name}]: {exc}",
                        stackTrace=traceback.format_exc(),
                    )
                )

    def yield_database_schema(
        self, schema_name: str
    ) -> Iterable[Either[CreateDatabaseSchemaRequest]]:
        """
        From topology.
        Prepare a database schema request and pass it to the sink
        """
        yield Either(
            right=CreateDatabaseSchemaRequest(
                name=schema_name,
                database=fqn.build(
                    metadata=self.metadata,
                    entity_type=Database,
                    service_name=self.context.database_service,
                    database_name=self.context.database,
                ),
            )
        )

    def get_tables_name_and_type(self) -> Iterable[Tuple[str, str]]:
        """
        Handle table and views.

        Fetches them up using the context information and
        the inspector set when preparing the db.

        :return: tables or views, depending on config
        """
        schema_name = self.context.database_schema
        catalog_name = self.context.database
        for table in self.client.tables.list(
            catalog_name=catalog_name,
            schema_name=schema_name,
        ):
            try:
                table_name = table.name
                table_fqn = fqn.build(
                    self.metadata,
                    entity_type=Table,
                    service_name=self.context.database_service,
                    database_name=self.context.database,
                    schema_name=self.context.database_schema,
                    table_name=table_name,
                )
                if filter_by_table(
                    self.config.sourceConfig.config.tableFilterPattern,
                    table_fqn
                    if self.config.sourceConfig.config.useFqnForFiltering
                    else table_name,
                ):
                    self.status.filter(
                        table_fqn,
                        "Table Filtered Out",
                    )
                    continue
                table_type: TableType = TableType.Regular
                if table.table_type.value.lower() == TableType.View.value.lower():
                    table_type: TableType = TableType.View
                if table.table_type.value.lower() == TableType.External.value.lower():
                    table_type: TableType = TableType.External
                self.context.table_data = table
                yield table_name, table_type
            except Exception as exc:
                self.status.failed(
                    StackTraceError(
                        name=table.Name,
                        error=f"Unexpected exception to get table [{table.Name}]: {exc}",
                        stackTrace=traceback.format_exc(),
                    )
                )

    def yield_table(
        self, table_name_and_type: Tuple[str, str]
    ) -> Iterable[Either[CreateTableRequest]]:
        """
        From topology.
        Prepare a table request and pass it to the sink
        """
        table_name, table_type = table_name_and_type
        table = self.client.tables.get(self.context.table_data.full_name)
        schema_name = self.context.database_schema
        db_name = self.context.database
        table_constraints = None
        try:
            columns = self.get_columns(table.columns)
            (
                primary_constraints,
                foreign_constraints,
            ) = self.get_table_constraints(table.table_constraints)

            table_constraints = self.update_table_constraints(
                primary_constraints, foreign_constraints
            )

            table_request = CreateTableRequest(
                name=table_name,
                tableType=table_type,
                description=table.comment,
                columns=columns,
                tableConstraints=table_constraints,
                databaseSchema=fqn.build(
                    metadata=self.metadata,
                    entity_type=DatabaseSchema,
                    service_name=self.context.database_service,
                    database_name=self.context.database,
                    schema_name=schema_name,
                ),
            )
            yield Either(right=table_request)

            if table_type == TableType.View or table.view_definition:
                self.context.table_views.append(
                    TableView(
                        table_name=table_name,
                        schema_name=schema_name,
                        db_name=db_name,
                        view_definition=(
                            f'CREATE VIEW "{db_name}"."{schema_name}"'
                            f'."{table_name}" AS {table.view_definition}'
                        ),
                    )
                )

            self.register_record(table_request=table_request)
        except Exception as exc:
            yield Either(
                left=StackTraceError(
                    name=table_name,
                    error=f"Unexpected exception to yield table [{table_name}]: {exc}",
                    stackTrace=traceback.format_exc(),
                )
            )

    def get_table_constraints(
        self, constraints: List[DBTableConstraint]
    ) -> Tuple[List[TableConstraint], List[ForeignConstrains]]:
        """
        Function to handle table constraint for the current table and add it to context
        """

        primary_constraints = []
        foreign_constraints = []
        for constraint in constraints:
            if constraint.primary_key_constraint:
                primary_constraints.append(
                    TableConstraint(
                        constraintType=ConstraintType.PRIMARY_KEY,
                        columns=constraint.primary_key_constraint.child_columns,
                    )
                )
            if constraint.foreign_key_constraint:
                foreign_constraints.append(
                    ForeignConstrains(
                        child_columns=constraint.foreign_key_constraint.child_columns,
                        parent_columns=constraint.foreign_key_constraint.parent_columns,
                        parent_table=constraint.foreign_key_constraint.parent_table,
                    )
                )
        return primary_constraints, foreign_constraints

    def _get_foreign_constraints(self, foreign_columns) -> List[TableConstraint]:
        """
        Search the referred table for foreign constraints
        and get referred column fqn
        """

        table_constraints = []
        for column in foreign_columns:
            referred_column_fqns = []
            ref_table_fqn = column.parent_table
            table_fqn_list = fqn.split(ref_table_fqn)

            referred_table = fqn.search_table_from_es(
                metadata=self.metadata,
                table_name=table_fqn_list[2],
                schema_name=table_fqn_list[1],
                database_name=table_fqn_list[0],
                service_name=self.context.database_service,
            )
            if referred_table:
                for parent_column in column.parent_columns:
                    col_fqn = get_column_fqn(
                        table_entity=referred_table, column=parent_column
                    )
                    if col_fqn:
                        referred_column_fqns.append(col_fqn)
            else:
                continue

            table_constraints.append(
                TableConstraint(
                    constraintType=ConstraintType.FOREIGN_KEY,
                    columns=column.child_columns,
                    referredColumns=referred_column_fqns,
                )
            )

        return table_constraints

    def update_table_constraints(
        self, table_constraints, foreign_columns
    ) -> List[TableConstraint]:
        """
        From topology.
        process the table constraints of all tables
        """
        foreign_table_constraints = self._get_foreign_constraints(foreign_columns)
        if foreign_table_constraints:
            if table_constraints:
                table_constraints.extend(foreign_table_constraints)
            else:
                table_constraints = foreign_table_constraints
        return table_constraints

    def prepare(self):
        """Nothing to prepare"""

    def add_complex_datatype_descriptions(
        self, column: Column, column_json: ColumnJson
    ):
        """
        Method to add descriptions to complex datatypes
        """
        try:
            if column.children is None:
                if column_json.metadata:
                    column.description = column_json.metadata.comment
            else:
                for i, child in enumerate(column.children):
                    if column_json.metadata:
                        column.description = column_json.metadata.comment
                    if (
                        column_json.type
                        and isinstance(column_json.type, Type)
                        and column_json.type.fields
                    ):
                        self.add_complex_datatype_descriptions(
                            child, column_json.type.fields[i]
                        )
                    if (
                        column_json.type
                        and isinstance(column_json.type, Type)
                        and column_json.type.type.lower() == "array"
                        and isinstance(column_json.type.elementType, ElementType)
                    ):
                        self.add_complex_datatype_descriptions(
                            child,
                            column_json.type.elementType.fields[i],
                        )
        except Exception as exc:
            logger.debug(traceback.format_exc())
            logger.warning(
                f"Unable to add description to complex datatypes for column [{column.name}]: {exc}"
            )

    def get_columns(self, column_data: List[ColumnInfo]) -> Iterable[Column]:
        """
        process table regular columns info
        """

        for column in column_data:
            if column.type_text.lower().startswith("union"):
                column.type_text = column.Type.replace(" ", "")
            if column.type_text.lower() == "struct":
                column.type_text = "struct<>"

            parsed_string = ColumnTypeParser._parse_datatype_string(  # pylint: disable=protected-access
                column.type_text.lower()
            )
            parsed_string["name"] = column.name[:256]
            parsed_string["dataLength"] = parsed_string.get("dataLength", 1)
            parsed_string["description"] = column.comment
            parsed_column = Column(**parsed_string)
            self.add_complex_datatype_descriptions(
                column=parsed_column,
                column_json=ColumnJson.parse_obj(json.loads(column.type_json)),
            )
            yield parsed_column

    def yield_view_lineage(self) -> Iterable[Either[AddLineageRequest]]:
        logger.info("Processing Lineage for Views")
        for view in [
            v for v in self.context.table_views if v.view_definition is not None
        ]:
            yield from get_view_lineage(
                view=view,
                metadata=self.metadata,
                service_name=self.context.database_service,
                connection_type=self.service_connection.type.value,
            )

    def yield_tag(
        self, schema_name: str
    ) -> Iterable[Either[OMetaTagAndClassification]]:
        """No tags being processed"""

    def get_stored_procedures(self) -> Iterable[Any]:
        """Not implemented"""

    def yield_stored_procedure(
        self, stored_procedure: Any
    ) -> Iterable[Either[CreateStoredProcedureRequest]]:
        """Not implemented"""

    def get_stored_procedure_queries(self) -> Iterable[QueryByProcedure]:
        """Not Implemented"""

    def yield_procedure_lineage_and_queries(
        self,
    ) -> Iterable[Either[Union[AddLineageRequest, CreateQueryRequest]]]:
        """Not Implemented"""
        yield from []

    def close(self):
        """Nothing to close"""
