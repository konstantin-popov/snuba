import os

CLUSTERS = [
    {
        "host": os.environ.get("CLICKHOUSE_HOST", "localhost"),
        "port": int(os.environ.get("CLICKHOUSE_PORT", 9000)),
        "user": os.environ.get("CLICKHOUSE_USER", "default"),
        "password": os.environ.get("CLICKHOUSE_PASSWORD", ""),
        "database": os.environ.get("CLICKHOUSE_DATABASE", "default"),
        "http_port": int(os.environ.get("CLICKHOUSE_HTTP_PORT", 8123)),
        "secure": os.environ.get("CLICKHOUSE_SECURE", "0") == "1",
        "ca_certs": os.environ.get("CLICKHOUSE_CA_CERTS"),
        "storage_sets": {
            "cdc",
            "discover",
            "events",
            "events_ro",
            "metrics",
            "migrations",
            "outcomes",
            "querylog",
            "sessions",
            "transactions",
            "transactions_ro",
            "transactions_v2",
            "errors_v2",
            "errors_v2_ro",
            "profiles",
        },
        "single_node": False,
        "cluster_name": "cluster_one_sh",
        "distributed_cluster_name": "cluster_one_sh",
    },
]
