from copy import deepcopy
from typing import Sequence

from snuba.clickhouse.columns import (
    Array,
    UUID,
    Column,
    DateTime,
    Float,
    IPv4,
    IPv6,
    LowCardinality,
    Materialized,
    Nested,
    Nullable,
    String,
    UInt,
    WithDefault,
)
from snuba.datasets.storages.tags_hash_map import TAGS_HASH_MAP_COLUMN
from snuba.clusters.storage_sets import StorageSetKey
from snuba.migrations import migration, operations, table_engines


UNKNOWN_SPAN_STATUS = 2

columns = [
    Column("project_id", UInt(64)),
    Column("transaction_id", UUID()),  # event_id of the transaction
    Column("span_id", UInt(64)),
    Column("parent_span_id", UInt(64)),  # Is this actually the transaction id ?
    Column("description", LowCardinality(String())),  # description in span
    Column("op", LowCardinality(String())),
    Column(
        "status", WithDefault(UInt(8), str(UNKNOWN_SPAN_STATUS)),
    ),  # This was transaction_status
    Column("start_ts", DateTime()),
    Column("start_ms", UInt(16)),
    Column("finish_ts", DateTime()),
    Column("finish_ms", UInt(16)),
    Column("duration", UInt(32)),
    # back to spans
    Column("tags", Nested([("key", String()), ("value", String())])),
    Column("retention_days", UInt(16)),
    Column("deleted", UInt(8)),
]


class Migration(migration.MultiStepMigration):
    blocking = False

    def forwards_local(self) -> Sequence[operations.Operation]:
        return [
            operations.CreateTable(
                storage_set=StorageSetKey.TRANSACTIONS,
                table_name="single_spans_local",
                columns=columns,
                engine=table_engines.ReplacingMergeTree(
                    storage_set=StorageSetKey.TRANSACTIONS,
                    version_column="deleted",
                    order_by="(project_id, toStartOfDay(finish_ts), cityHash64(parent_span_id), cityHash64(span_id))",
                    partition_by="(retention_days, toMonday(finish_ts))",
                    sample_by="cityHash64(span_id)",
                    ttl="finish_ts + toIntervalDay(retention_days)",
                    settings={"index_granularity": "8192"},
                ),
            ),
            operations.AddColumn(
                storage_set=StorageSetKey.TRANSACTIONS,
                table_name="single_spans_local",
                column=Column(
                    "_tags_hash_map",
                    Materialized(Array(UInt(64)), TAGS_HASH_MAP_COLUMN),
                ),
                after="tags.value",
            ),
        ]

    def backwards_local(self) -> Sequence[operations.Operation]:
        return [
            operations.DropTable(
                storage_set=StorageSetKey.TRANSACTIONS, table_name="single_spans_local",
            )
        ]

    def forwards_dist(self) -> Sequence[operations.Operation]:
        # We removed the materialized for the dist table DDL.
        def strip_materialized(columns: Sequence[Column]) -> None:
            for col in columns:
                if isinstance(col.type, Materialized):
                    col.type = col.type.inner_type

        dist_columns = deepcopy(columns)
        strip_materialized(dist_columns)

        return [
            operations.CreateTable(
                storage_set=StorageSetKey.TRANSACTIONS,
                table_name="single_spans_dist",
                columns=dist_columns,
                engine=table_engines.Distributed(
                    local_table_name="spans_local", sharding_key="cityHash64(span_id)",
                ),
            )
        ]

    def backwards_dist(self) -> Sequence[operations.Operation]:
        return [
            operations.DropTable(
                storage_set=StorageSetKey.TRANSACTIONS, table_name="single_spans_dist",
            )
        ]
